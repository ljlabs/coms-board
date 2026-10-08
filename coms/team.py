"""Durable team dialogue, recipient wakeups, peer reviews and retrieved evidence."""
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from . import api


def job_for_ticket(conn, tid):
    ticket = api.get_ticket(conn, int(tid))
    seen = set()
    while ticket['type'] != 'job' and ticket.get('parent_id'):
        if ticket['id'] in seen: return None
        seen.add(ticket['id'])
        ticket = api.get_ticket(conn, ticket['parent_id'])
    return ticket if ticket['type'] == 'job' else None


def roster(conn, job_id):
    job = api.get_ticket(conn, int(job_id))
    if job['type'] != 'job': raise api.BadRequest('Team conversations belong to a job ticket')
    rows = conn.execute('WITH RECURSIVE work AS (SELECT id,assignee FROM tickets WHERE id=? UNION ALL SELECT t.id,t.assignee FROM tickets t JOIN work w ON t.parent_id=w.id) SELECT a.*,group_concat(w.id) AS ticket_ids FROM work w JOIN agents a ON a.slug=w.assignee GROUP BY a.id ORDER BY a.role,a.name', (job_id,))
    return [dict(row) for row in rows]


def member(conn, job_id, actor):
    if actor != api.ADMIN_ACTOR and actor not in [a['slug'] for a in roster(conn, job_id)]:
        raise api.BadRequest('Agent is not a member of this job team')


def delegate(conn, job_id, actor, name, capabilities, title, body):
    job = api.get_ticket(conn, job_id)
    if job['type'] != 'job' or job['assignee'] != actor:
        raise api.BadRequest('Only the assigned job lead can delegate teammates')
    if not name.strip() or not capabilities.strip() or not body.strip():
        raise api.BadRequest('Provide a distinct personal name, capabilities and an actionable scope')
    agent = api.ensure_agent(conn, name, 'expert', capabilities, commit=False)
    if agent['slug'] == actor: raise api.BadRequest('Delegate to a specialist other than yourself')
    # Retrying a dispatched assignment must not create another identical worker.
    existing = conn.execute('SELECT id FROM tickets WHERE parent_id=? AND assignee=? AND title=?', (job_id, agent['slug'], title.strip())).fetchone()
    if existing:
        return {'agent': agent, 'ticket': api.get_ticket(conn, existing['id'])}
    urls = list(dict.fromkeys(re.findall(r'https://[^\s<>]+', job['body'])))
    assignment = (body.strip() + f'\n\nParent research job #{job_id}: {job["title"]}. Perform only your assigned specialty; the lead handles staffing and synthesis.\nStarting source URLs:\n' + '\n'.join(urls) +
                  f'\n\nYour team room belongs to job #{job_id}. Read team_roster, ask a substantive question with team_send, then end this turn while waiting. Read the answer on resumption. Retrieve actual evidence with source_fetch. Publish and link your own evidence page and request peer review before ticket_result. Cite source IDs, URLs and retrieval dates; distinguish evidence, inference and limitations. Do not invent verification or backtests. Publish a shared method only after using it successfully. The lead owns the combined synthesis.')
    ticket = api.create_ticket(conn, title, assignment, actor, type_='research', assignee=agent['slug'], parent_id=job_id, ready_for_execution=True)
    send(conn, job_id, actor, '*', f"{'Reused' if agent['reused'] else 'Registered'} {agent['name']} ({agent['slug']}): {capabilities}. Assigned ticket #{ticket['id']}: {title}.", kind='note')
    return {'agent': agent, 'ticket': ticket}


def room(conn, job_id, after_id=0):
    api.get_ticket(conn, int(job_id))
    messages = [dict(row) for row in conn.execute('SELECT m.*,s.name AS sender_name,r.name AS recipient_name FROM team_messages m LEFT JOIN agents s ON s.slug=m.sender LEFT JOIN agents r ON r.slug=m.recipient WHERE m.job_id=? AND m.id>? ORDER BY m.id DESC LIMIT 200', (job_id, int(after_id)))]
    messages.reverse()
    for msg in messages:
        msg['deliveries'] = [dict(row) for row in conn.execute('SELECT * FROM team_deliveries WHERE message_id=?', (msg['id'],))]
    reviews = [dict(row) for row in conn.execute('SELECT p.*,w.updated_at AS current_version FROM peer_reviews p LEFT JOIN wiki_pages w ON w.slug=p.artifact_slug WHERE p.job_id=? ORDER BY p.id', (job_id,))]
    for review in reviews: review['current'] = review['current_version'] == review['artifact_version']
    sources = [dict(row) for row in conn.execute('SELECT id,reader,url,final_url,title,status,error,retrieved_at FROM research_sources WHERE job_id=? ORDER BY id', (job_id,))]
    return {'job_id': int(job_id), 'members': roster(conn, job_id), 'messages': messages, 'reviews': reviews, 'sources': sources}


def send(conn, job_id, sender, to, body, *, kind='question', reply_to=None, commit=True):
    member(conn, job_id, sender)
    if not isinstance(body, str) or not body.strip() or len(body) > 16000:
        raise api.BadRequest('A team message must contain 1–16000 characters')
    members = {a['slug']: a for a in roster(conn, job_id)}
    if kind in ('question', 'review_request') and (to == '*' or to == sender):
        raise api.BadRequest('Choose one other teammate as the recipient')
    if to != '*' and to not in members: raise api.BadRequest('Recipient is not a member of this job')
    if to in members and members[to]['role'] == 'human' and kind in ('question', 'review_request'):
        raise api.BadRequest('Automatic responses require an execution agent')
    stamp = api.now()
    mid = conn.execute('INSERT INTO team_messages(job_id,sender,recipient,kind,body,reply_to,created_at) VALUES(?,?,?,?,?,?,?)', (job_id, sender, to, kind, body.strip(), reply_to, stamp)).lastrowid
    if kind in ('question', 'review_request'):
        conn.execute('INSERT INTO team_deliveries(message_id,recipient,updated_at) VALUES(?,?,?)', (mid, to, stamp))
    api.log_activity(conn, sender, 'team.' + kind, f'ticket:{job_id}', f'{sender} → {to}: {body[:180]}')
    api.touch_agent(conn, sender)
    if commit: conn.commit()
    return dict(conn.execute('SELECT * FROM team_messages WHERE id=?', (mid,)).fetchone())


def reply(conn, message_id, actor, body, *, commit=True):
    original = conn.execute('SELECT * FROM team_messages WHERE id=?', (message_id,)).fetchone()
    if not original: raise api.NotFound('Team message not found')
    if original['recipient'] != actor: raise api.BadRequest('Only the addressed teammate can reply')
    if original['kind'] == 'review_request': raise api.BadRequest('Use team_review to decide a peer review')
    delivery = conn.execute('SELECT status,response_id FROM team_deliveries WHERE message_id=? AND recipient=?', (message_id, actor)).fetchone()
    if not delivery or delivery['status'] == 'answered': raise api.BadRequest('This request already has a response')
    response = send(conn, original['job_id'], actor, original['sender'], body, kind='reply', reply_to=message_id, commit=False)
    conn.execute("UPDATE team_deliveries SET status='answered',response_id=?,error='',updated_at=? WHERE message_id=? AND recipient=?", (response['id'], api.now(), message_id, actor))
    if commit: conn.commit()
    return response


def request_review(conn, job_id, tid, actor, artifact_slug, reviewer):
    member(conn, job_id, actor)
    ticket = api.get_ticket(conn, tid)
    job = job_for_ticket(conn, tid)
    if not job or job['id'] != int(job_id) or ticket['assignee'] != actor:
        raise api.BadRequest('Review requests must refer to your own ticket in this job')
    artifact = api.get_wiki(conn, artifact_slug)
    if artifact['updated_by'] != actor: raise api.BadRequest('Only the artifact author can request its review')
    prior = conn.execute("SELECT * FROM peer_reviews WHERE ticket_id=? AND artifact_slug=? AND artifact_version=? AND reviewer=? AND status IN ('requested','accepted') ORDER BY id DESC LIMIT 1", (tid, artifact_slug, artifact['updated_at'], reviewer)).fetchone()
    if prior: return dict(prior)
    message = send(conn, job_id, actor, reviewer,
        f'Please peer review [[{artifact_slug}]] for ticket #{tid}. Read the current page with wiki_get and the actual research_sources with source_get. Challenge unsupported claims, missing citations and misleading completion claims. Use team_review with accepted or changes_requested and concrete reasons. Explicitly reported source-access limitations are acceptable; invented verification is not.', kind='review_request', commit=False)
    stamp = api.now()
    rid = conn.execute('INSERT INTO peer_reviews(job_id,ticket_id,artifact_slug,artifact_version,author,reviewer,message_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', (job_id, tid, artifact_slug, artifact['updated_at'], actor, reviewer, message['id'], stamp, stamp)).lastrowid
    conn.commit()
    return dict(conn.execute('SELECT * FROM peer_reviews WHERE id=?', (rid,)).fetchone())


def review(conn, review_id, actor, decision, body, run_id):
    if decision not in ('accepted', 'changes_requested'): raise api.BadRequest('Review decision must be accepted or changes_requested')
    if not body.strip(): raise api.BadRequest('Peer reviews need concrete findings')
    item = conn.execute('SELECT * FROM peer_reviews WHERE id=?', (review_id,)).fetchone()
    if not item: raise api.NotFound('Peer review not found')
    if item['reviewer'] != actor or item['status'] != 'requested': raise api.BadRequest('Only the assigned reviewer can decide a pending review')
    artifact = api.get_wiki(conn, item['artifact_slug'])
    if artifact['updated_at'] != item['artifact_version']: raise api.BadRequest('Artifact changed; its author must request a fresh review')
    seen = conn.execute('SELECT 1 FROM artifact_reads WHERE agent_slug=? AND run_id=? AND artifact_slug=? AND artifact_version=?', (actor, run_id, item['artifact_slug'], item['artifact_version'])).fetchone()
    if not seen: raise api.BadRequest('Read the current artifact with wiki_get before deciding its review')
    if decision == 'accepted':
        evidence = [dict(row) for row in conn.execute("SELECT s.* FROM source_reads r JOIN research_sources s ON s.id=r.source_id WHERE r.agent_slug=? AND r.run_id=? AND s.job_id=? AND s.status='ok'", (actor, run_id, item['job_id']))]
        if not any(s['url'] in artifact['body'] or s['final_url'] in artifact['body'] for s in evidence):
            raise api.BadRequest('Read a successfully retrieved source cited by the artifact with source_get before accepting the review')
    response = send(conn, item['job_id'], actor, item['author'], decision + ': ' + body, kind='peer_review', reply_to=item['message_id'], commit=False)
    conn.execute('UPDATE peer_reviews SET status=?,reason=?,updated_at=? WHERE id=?', (decision, body, api.now(), review_id))
    conn.execute("UPDATE team_deliveries SET status='answered',response_id=?,error='',updated_at=? WHERE message_id=? AND recipient=?", (response['id'], api.now(), item['message_id'], actor))
    conn.commit()
    return dict(conn.execute('SELECT * FROM peer_reviews WHERE id=?', (review_id,)).fetchone())


def pending(conn, tid, actor):
    job = job_for_ticket(conn, tid)
    if not job: return False
    return bool(conn.execute("SELECT 1 FROM team_messages m JOIN team_deliveries d ON d.message_id=m.id WHERE m.job_id=? AND m.sender=? AND d.status IN ('pending','responding') LIMIT 1", (job['id'], actor)).fetchone())


def context(conn, tid, actor, *, instructions=True):
    job = job_for_ticket(conn, tid)
    if not job or not job.get('collaboration_required'): return ''
    data = room(conn, job['id'])
    data['messages'] = data['messages'][-30:]
    data['members'] = [{k: a[k] for k in ('slug', 'name', 'role', 'description', 'ticket_ids')} for a in data['members']]
    return ('TEAM CONVERSATION\n' + json.dumps(data, ensure_ascii=False) +
        ('\nUse team_roster and team_read. If you have not received a teammate answer yet, ask one substantive question with team_send to another specialist; the runner wakes them to respond. Use existing answers instead of repeating completed exchanges. Publish your own linked evidence page, then request its peer review with team_request_review. If waiting for replies or a review, finish your turn WITHOUT ticket_result; the runner will resume you. Address changes_requested before requesting a fresh review. Capture a method actually used successfully as a shared skill with supporting source IDs. Do not create filler chatter or claim completed review without a decision.\n' if instructions else ''))


def validate_delivery(conn, tid, actor):
    ticket = api.get_ticket(conn, tid)
    job = job_for_ticket(conn, tid)
    if not job or not job.get('collaboration_required'): return
    pages = [dict(row) for row in conn.execute("SELECT w.* FROM ticket_links l JOIN wiki_pages w ON w.slug=l.ref WHERE l.ticket_id=? AND l.kind='wiki' AND w.updated_by=? AND w.updated_at>=?", (tid, actor, job['created_at']))]
    if not pages: raise api.BadRequest('Delivery needs an actual wiki page written by you and attached with ticket_link; summaries alone do not count')
    reviewed = [p for p in pages if conn.execute("SELECT 1 FROM peer_reviews WHERE ticket_id=? AND artifact_slug=? AND artifact_version=? AND status='accepted'", (tid, p['slug'], p['updated_at'])).fetchone()]
    if not reviewed: raise api.BadRequest('Your linked page needs an accepted teammate review of its current revision')
    pages = reviewed
    if ticket['type'] == 'job':
        if not api.children_resolved(conn, tid): raise api.BadRequest('Finish teammate tickets and reviews before final synthesis delivery')
        for child in api.list_tickets(conn, parent_id=tid, include_closed=True):
            if child['status'] != 'wontfix': validate_delivery(conn, child['id'], child['assignee'])
        latest = conn.execute('SELECT max(updated_at) FROM tickets WHERE parent_id=?', (tid,)).fetchone()[0]
        if latest and not any(p['updated_at'] >= latest for p in pages): raise api.BadRequest('Write a fresh synthesis after the teammate results and reviews')
        skills = conn.execute("SELECT 1 FROM shared_skills WHERE slug NOT IN ('team-orchestration','source-backed-research') AND created_at>? AND created_by IN (SELECT sender FROM team_messages WHERE job_id=?) LIMIT 1", (job['created_at'], job['id'])).fetchone()
        if not skills: raise api.BadRequest('Publish at least one new shared procedure grounded in the methods this team actually used')
    else:
        answered = conn.execute("SELECT 1 FROM team_messages m JOIN team_deliveries d ON d.message_id=m.id WHERE m.job_id=? AND m.sender=? AND m.kind='question' AND d.status='answered' LIMIT 1", (job['id'], actor)).fetchone()
        if not answered: raise api.BadRequest('Ask another teammate a substantive question with team_send and receive a reply before delivery')
        sources = [dict(row) for row in conn.execute("SELECT * FROM research_sources WHERE job_id=? AND reader=? AND status='ok'", (job['id'], actor))]
        if not any(s['url'] in p['body'] or s['final_url'] in p['body'] for s in sources for p in pages):
            raise api.BadRequest('Cite at least one source you actually retrieved with source_fetch in your linked evidence page')


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.title = []; self.hidden = 0; self.in_title = False
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'): self.hidden += 1
        if tag == 'title': self.in_title = True
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'): self.hidden = max(0, self.hidden - 1)
        if tag == 'title': self.in_title = False
    def handle_data(self, data):
        if self.in_title: self.title.append(data)
        if not self.hidden and data.strip(): self.parts.append(data.strip())


def _public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise api.BadRequest('Research sources must use a public HTTPS URL')
    for address in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(address[4][0]).is_global: raise api.BadRequest('Local and private source URLs are unavailable')


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_source(conn, job_id, actor, url):
    member(conn, job_id, actor)
    title, text, final_url, status, error = '', '', '', 'failed', ''
    try:
        _public_url(url)
        with build_opener(_Redirect()).open(Request(url, headers={'User-Agent': 'SwarmAgent Research/1.0'}), timeout=20) as response:
            final_url = response.url
            kind = response.headers.get_content_type()
            if kind not in ('text/html', 'text/plain', 'application/json'): raise api.BadRequest('Use an HTML or text source for auditable retrieval')
            raw = response.read(1024 * 1024).decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
            if kind == 'text/html':
                parsed = _Text(); parsed.feed(raw); title = ' '.join(parsed.title); text = '\n'.join(parsed.parts)
            else: text = raw
            text = text[:24000]
            if len(text.strip()) < 100: raise api.BadRequest('Source returned too little content to verify')
            status = 'ok'
    except (HTTPError, URLError, OSError, api.BadRequest) as exc:
        error = str(exc)[:1000]
    sid = conn.execute('INSERT INTO research_sources(job_id,reader,url,final_url,title,status,body,error,retrieved_at) VALUES(?,?,?,?,?,?,?,?,?)', (job_id, actor, url, final_url, title, status, text, error, api.now())).lastrowid
    api.log_activity(conn, actor, 'source.' + status, f'ticket:{job_id}', url + (': ' + error if error else ''))
    conn.commit()
    return dict(conn.execute('SELECT * FROM research_sources WHERE id=?', (sid,)).fetchone())


def source_get(conn, source_id):
    row = conn.execute('SELECT * FROM research_sources WHERE id=?', (source_id,)).fetchone()
    if not row: raise api.NotFound('Retrieved source not found')
    return dict(row)
