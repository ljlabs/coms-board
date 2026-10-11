"""Durable team dialogue, recipient wakeups, peer reviews and retrieved evidence."""
import ipaddress
import hashlib
import json
import re
import socket
from pathlib import Path
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


def is_stopped(conn, tid):
    """A held ancestor also prevents work created through execution provenance."""
    return bool(conn.execute("""WITH RECURSIVE lineage(id) AS (
        SELECT id FROM tickets WHERE id=? UNION
        SELECT t.parent_id FROM tickets t JOIN lineage l ON t.id=l.id WHERE t.parent_id IS NOT NULL UNION
        SELECT t.source_ticket_id FROM tickets t JOIN lineage l ON t.id=l.id WHERE t.source_ticket_id IS NOT NULL)
        SELECT 1 FROM tickets t JOIN lineage l ON t.id=l.id
        WHERE t.ready_for_execution=0 AND t.status IN ('blocked','wontfix') LIMIT 1""", (tid,)).fetchone())


def require_running(conn, tid):
    if is_stopped(conn, tid):
        raise api.BadRequest(f'Ticket #{tid} belongs to a stopped team; resume it explicitly before executing')


def stop(conn, job_id, actor):
    """Hold a whole team atomically; killing a process alone is retryable."""
    job = api.get_ticket(conn, job_id)
    role = conn.execute('SELECT role FROM agents WHERE slug=?', (actor,)).fetchone()
    if job['type'] != 'job': raise api.BadRequest('Team stop requires a job ticket')
    if actor != api.ADMIN_ACTOR and actor != job['assignee'] and (not role or role['role'] != 'orchestrator'):
        raise api.BadRequest('Only the human operator, orchestrator, or assigned job lead can stop a team')
    from . import schedules
    if not conn.in_transaction: conn.execute('BEGIN IMMEDIATE')
    try:
        ids = [row[0] for row in conn.execute("""WITH RECURSIVE work(id) AS (
            SELECT id FROM tickets WHERE id=? UNION
            SELECT t.id FROM tickets t JOIN work w ON t.parent_id=w.id OR t.source_ticket_id=w.id)
            SELECT id FROM work ORDER BY id""", (job_id,))]
        marks = ','.join('?' for _ in ids)
        stamp, reason = api.now(), 'Team stopped by the operator; resume work explicitly'
        conn.execute(f"UPDATE tickets SET ready_for_execution=0,agent_retry_requested=0,status=CASE WHEN status IN ('done','wontfix') THEN status ELSE 'blocked' END,updated_at=? WHERE id IN ({marks})", [stamp] + ids)
        conn.execute(f"UPDATE agent_runs SET status='cancelled',finished_at=?,error=? WHERE ticket_id IN ({marks}) AND status IN ('queued','running','waiting')", [stamp, reason] + ids)
        conn.execute(f"UPDATE agent_runtime SET state='cancelled',phase='cancelled',summary=?,updated_at=?,finished_at=? WHERE ticket_id IN ({marks}) AND state IN ('running','waiting')", [reason, stamp, stamp] + ids)
        conn.execute(f"UPDATE team_deliveries SET status='cancelled',error=?,updated_at=? WHERE message_id IN (SELECT id FROM team_messages WHERE job_id IN ({marks})) AND status IN ('pending','responding','failed')", [reason, stamp] + ids)
        conn.execute(f"UPDATE peer_reviews SET status='cancelled',reason=?,updated_at=? WHERE job_id IN ({marks}) AND status IN ('requested','failed')", [reason, stamp] + ids)
        conn.execute(f"UPDATE ticket_reviews SET status='cancelled',reason=?,finished_at=? WHERE ticket_id IN ({marks}) AND status IN ('queued','running','failed')", [reason, stamp] + ids)
        for tid in ids:
            api._index_ticket(conn, dict(conn.execute('SELECT * FROM tickets WHERE id=?', (tid,)).fetchone()))
            schedules.mark_ticket(conn, tid, 'cancelled', reason)
        api.log_activity(conn, actor, 'team.stop', f'ticket:{job_id}', f'Stopped team and held {len(ids)} tickets')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'job_id': int(job_id), 'stopped': True, 'ticket_ids': ids}


def delegate(conn, job_id, actor, name, capabilities, title, body, *, type_='task'):
    require_running(conn, job_id)
    job = api.get_ticket(conn, job_id)
    if job['type'] != 'job' or job['assignee'] != actor:
        raise api.BadRequest('Only the assigned job lead can delegate teammates')
    if not name.strip() or not capabilities.strip() or not body.strip():
        raise api.BadRequest('Provide a distinct personal name, capabilities and an actionable scope')
    if type_ not in ('task', 'research'):
        raise api.BadRequest('Delegate a task or research assignment')
    agent = api.ensure_agent(conn, name, 'expert', capabilities, commit=False)
    if agent['slug'] == actor: raise api.BadRequest('Delegate to a specialist other than yourself')
    # Retrying a dispatched assignment must not create another identical worker.
    existing = conn.execute('SELECT id FROM tickets WHERE parent_id=? AND assignee=? AND title=?', (job_id, agent['slug'], title.strip())).fetchone()
    if existing:
        return {'agent': agent, 'ticket': api.get_ticket(conn, existing['id'])}
    urls = list(dict.fromkeys(re.findall(r'https://[^\s<>]+', job['body'])))
    assignment = (body.strip() + f'\n\nParent job #{job_id}: {job["title"]}. Perform only your assigned specialty; the lead handles staffing and synthesis.\n' +
                  ('Starting source URLs:\n' + '\n'.join(urls) + '\n' if urls else '') +
                  f'\nYour team room belongs to job #{job_id}. Use native tools to inspect the workspace and run requested tests. Record local file evidence with source_read_file, or retrieve relevant public HTTPS evidence with source_fetch. Cite the returned source URLs/IDs and file lines; distinguish evidence, inference and limitations. Read team_roster and ask one substantive question with team_send, then end this turn while waiting. Use existing answers on resumption. Publish and link your own evidence page and request peer review before ticket_result. Do not invent verification. Share useful methods with the lead; the orchestrator publishes shared skills. The lead owns the combined synthesis.')
    ticket = api.create_ticket(conn, title, assignment, actor, type_=type_, assignee=agent['slug'], parent_id=job_id, ready_for_execution=True, collaboration_required=True, workspace=job.get('workspace', ''))
    send(conn, job_id, actor, '*', f"{'Reused' if agent['reused'] else 'Registered'} {agent['name']} ({agent['slug']}): {capabilities}. Assigned ticket #{ticket['id']}: {title}.", kind='note')
    return {'agent': agent, 'ticket': ticket}


def room(conn, job_id, after_id=0):
    from . import progress
    api.get_ticket(conn, int(job_id))
    messages = [dict(row) for row in conn.execute('SELECT m.*,s.name AS sender_name,r.name AS recipient_name FROM team_messages m LEFT JOIN agents s ON s.slug=m.sender LEFT JOIN agents r ON r.slug=m.recipient WHERE m.job_id=? AND m.id>? ORDER BY m.id DESC LIMIT 200', (job_id, int(after_id)))]
    messages.reverse()
    for msg in messages:
        msg['deliveries'] = [dict(row) for row in conn.execute('SELECT * FROM team_deliveries WHERE message_id=?', (msg['id'],))]
    reviews = [dict(row) for row in conn.execute('SELECT p.*,w.updated_at AS current_version FROM peer_reviews p LEFT JOIN wiki_pages w ON w.slug=p.artifact_slug WHERE p.job_id=? ORDER BY p.id', (job_id,))]
    for review in reviews: review['current'] = review['current_version'] == review['artifact_version']
    sources = [dict(row) for row in conn.execute('SELECT id,reader,url,final_url,title,status,error,retrieved_at FROM research_sources WHERE job_id=? ORDER BY id', (job_id,))]
    children=[{key: ticket[key] for key in ('id','title','status','assignee','result','updated_at')}
              for ticket in api.list_tickets(conn,parent_id=job_id,include_closed=True)]
    recent_tools=[]
    for row in conn.execute("""SELECT m.ticket_id,m.run_id,m.body,m.updated_at FROM agent_messages m
            JOIN tickets t ON t.id=m.ticket_id WHERE t.parent_id=? AND m.kind='acp-tool_call'
            ORDER BY m.id DESC LIMIT 40""",(job_id,)):
        try: payload=json.loads(row['body'])
        except (ValueError,TypeError): continue
        if not isinstance(payload,dict): continue
        recent_tools.append({'ticket_id':row['ticket_id'],'run_id':row['run_id'],
                             'title':payload.get('title',''),'status':payload.get('status',''),
                             'updated_at':row['updated_at']})
    return {'job_id': int(job_id), 'members': roster(conn, job_id), 'messages': messages,
            'reviews': reviews, 'sources': sources, 'tickets':children,
            'progress':progress.snapshot(conn,ticket=job_id),'recent_tools':recent_tools}


def read_room(conn, job_id, after_id=0, *, message_id=None, offset=0, source_after_id=0):
    """Bound MCP/prompt context; the dashboard retains the full room contract."""
    if min(after_id, offset, source_after_id) < 0:
        raise api.BadRequest('Team read cursors must be nonnegative')
    if message_id is not None:
        row = conn.execute('SELECT * FROM team_messages WHERE job_id=? AND id=?', (job_id, message_id)).fetchone()
        if not row: raise api.NotFound('Team message not found in this job')
        message = {k: (value[:240] if isinstance(value, str) and k != 'body' else value) for k, value in dict(row).items()}
        message['body_length'] = len(message['body'])
        message['body'] = message['body'][offset:offset + 8000]
        while message['body'] and len(json.dumps(message, ensure_ascii=False).encode('utf-8')) > 24000:
            message['body'] = message['body'][:len(message['body']) // 2]
        message['next_offset'] = offset + len(message['body']) if offset + len(message['body']) < message['body_length'] else None
        return {'job_id': int(job_id), 'messages': [message]}
    data = room(conn, job_id, after_id)
    data['counts'] = {key: len(data[key]) for key in ('members', 'reviews', 'sources', 'tickets')}
    data['counts']['messages'] = conn.execute('SELECT count(*) FROM team_messages WHERE job_id=?', (job_id,)).fetchone()[0]
    data['messages'] = [{**m, 'body': m['body'][:800], 'body_length': len(m['body']), 'deliveries': [
            {k: (d.get(k, '')[:200] if isinstance(d.get(k), str) else d.get(k)) for k in ('recipient', 'status', 'attempt_count', 'error')} for d in m['deliveries'][:8]]}
            for m in data['messages'][-12:]]
    data['pending_requests'] = [dict(row) for row in conn.execute("SELECT m.id,m.sender,m.recipient,m.kind,substr(m.body,1,400) AS body,d.status,d.attempt_count FROM team_messages m JOIN team_deliveries d ON d.message_id=m.id WHERE m.job_id=? AND d.status IN ('pending','responding','failed') ORDER BY m.id DESC LIMIT 12", (job_id,))]
    data['members'] = [{k: str(a[k])[:240] for k in ('slug', 'name', 'role', 'description', 'ticket_ids')} for a in data['members'][:40]]
    data['tickets'] = [{**t, 'title': t['title'][:160], 'result': t['result'][:240]} for t in data['tickets'][:40]]
    data['reviews'] = [{k: (r[k][:240] if isinstance(r[k], str) else r[k]) for k in ('id', 'ticket_id', 'artifact_slug', 'author', 'reviewer', 'status', 'current', 'reason')} for r in data['reviews'][-12:]]
    data['sources'] = [{k: (value[:500] if isinstance(value, str) else value) for k, value in s.items()} for s in data['sources'] if s['id'] > source_after_id][:20]
    data['progress']['items'] = [{k: (item.get(k, '')[:240] if isinstance(item.get(k), str) else item.get(k)) for k in ('ticket_id', 'agent_slug', 'stage', 'state', 'phase', 'summary', 'updated_at', 'idle_seconds')} for item in data['progress']['items'][:16]]
    data['recent_tools'] = [{**t, 'title': t['title'][:160]} for t in data['recent_tools'][:12]]
    data['truncated'] = True
    # Limit encoded bytes, including non-ASCII text. Prefer keeping assignments
    # and pending requests visible; full messages/evidence have targeted reads.
    for bucket in (data['recent_tools'], data['progress']['items'], data['messages'], data['reviews'], data['sources'], data['members'], data['tickets'], data['pending_requests']):
        while len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > 31900 and len(bucket) > 1:
            bucket.pop(0 if bucket is data['messages'] else -1)
    data['next_source_after_id'] = data['sources'][-1]['id'] if data['sources'] else source_after_id
    return data


def send(conn, job_id, sender, to, body, *, kind='question', reply_to=None, commit=True):
    if kind in ('question', 'review_request'): require_running(conn, job_id)
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
    if not delivery or delivery['status'] in ('answered', 'cancelled'): raise api.BadRequest('This request already has a response or was cancelled')
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
        f'Please peer review [[{artifact_slug}]] for ticket #{tid}. Read the current page with wiki_get and the recorded file or web evidence with source_get. Use native tools or source_read_file to check current workspace files when relevant. Challenge unsupported claims, missing citations and misleading completion claims. Use team_review with accepted or changes_requested and concrete reasons. Explicitly reported source-access limitations are acceptable; invented verification is not.', kind='review_request', commit=False)
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
    data = read_room(conn, job['id'])
    # Keep prompts small enough for local models: full source bodies and long
    # teammate artifacts remain available through source_get/wiki_get on demand.
    data['messages'] = [{
        'id': m['id'], 'sender': m['sender'], 'sender_name': m['sender_name'],
        'recipient': m['recipient'], 'recipient_name': m['recipient_name'],
        'kind': m['kind'], 'reply_to': m['reply_to'],
        'preview': (m['body'][:360] + ('…' if len(m['body']) > 360 else '')),
        'deliveries': [{k: d.get(k) for k in ('recipient', 'status', 'attempt_count', 'error')} for d in m['deliveries']],
    } for m in data['messages'][-24:]]
    data['members'] = [{k: a[k] for k in ('slug', 'name', 'role', 'description', 'ticket_ids')} for a in data['members']]
    data['reviews'] = [{
        'id': r['id'], 'ticket_id': r['ticket_id'], 'artifact_slug': r['artifact_slug'],
        'author': r['author'], 'reviewer': r['reviewer'], 'status': r['status'],
        'current': r['current'], 'reason_preview': r['reason'][:360],
    } for r in data['reviews']]
    data['sources'] = [{k: s.get(k) for k in ('id', 'reader', 'url', 'final_url', 'title', 'status', 'error', 'retrieved_at')}
                       for s in data['sources']]
    data['tickets']=[{**ticket,'result':ticket['result'][:360]} for ticket in data['tickets']]
    data['progress']['items']=[{k:item.get(k) for k in ('ticket_id','agent_slug','stage','state','phase','summary','updated_at','idle_seconds')}
                               for item in data['progress']['items'][:24]]
    data['recent_tools']=data['recent_tools'][:12]
    return ('TEAM CONVERSATION\n' + json.dumps(data, ensure_ascii=False) +
        ('\nThe conversation, review and source entries above are compact indexes. Read full details with team_read, wiki_get and source_get before relying on them. Ask one substantive question if you have not received a teammate answer. Finish all wiki edits before requesting its current-revision review. After requesting review, do not edit that page until the reviewer replies; the runner ends your turn and resumes it automatically. If a reply or review is pending, finish WITHOUT ticket_result. Once a teammate has answered and the current page has an accepted review, call ticket_result and verify the tool succeeded. Revise rejected work before requesting a fresh review. Share useful methods with the orchestrator; only the human operator and orchestrator can publish them to the shared skills folder. Do not create filler chatter or claim completed review without a decision.\n' if instructions else ''))


def validate_delivery(conn, tid, actor):
    ticket = api.get_ticket(conn, tid)
    job = job_for_ticket(conn, tid)
    if not job or not job.get('collaboration_required'): return
    # Legacy or explicitly non-collaborative children are accepted by their
    # recorded result/status. New delegated tickets opt into this stricter
    # evidence/review contract in team.delegate.
    if ticket['type'] != 'job' and not ticket.get('collaboration_required'): return
    pages = [dict(row) for row in conn.execute("SELECT w.* FROM ticket_links l JOIN wiki_pages w ON w.slug=l.ref WHERE l.ticket_id=? AND l.kind='wiki' AND w.updated_by=? AND w.updated_at>=?", (tid, actor, job['created_at']))]
    if not pages: raise api.BadRequest('Delivery needs an actual wiki page written by you and attached with ticket_link; summaries alone do not count')
    reviewed = [p for p in pages if conn.execute("SELECT 1 FROM peer_reviews WHERE ticket_id=? AND artifact_slug=? AND artifact_version=? AND status='accepted'", (tid, p['slug'], p['updated_at'])).fetchone()]
    if not reviewed: raise api.BadRequest('Your linked page needs an accepted teammate review of its current revision')
    pages = reviewed
    if ticket['type'] == 'job':
        if not api.children_resolved(conn, tid): raise api.BadRequest('Finish teammate tickets and reviews before final synthesis delivery')
        for child in api.list_tickets(conn, parent_id=tid, include_closed=True):
            if child['status'] != 'wontfix' and child.get('collaboration_required'):
                validate_delivery(conn, child['id'], child['assignee'])
        latest = conn.execute('SELECT max(updated_at) FROM tickets WHERE parent_id=?', (tid,)).fetchone()[0]
        if latest and not any(p['updated_at'] >= latest for p in pages): raise api.BadRequest('Write a fresh synthesis after the teammate results and reviews')
        skills = conn.execute("SELECT 1 FROM activity WHERE kind='skill.create' AND created_at>? AND actor IN (SELECT sender FROM team_messages WHERE job_id=?) LIMIT 1", (job['created_at'], job['id'])).fetchone()
        if not skills: raise api.BadRequest('Publish at least one new shared procedure grounded in the methods this team actually used')
    else:
        answered = conn.execute("SELECT 1 FROM team_messages m JOIN team_deliveries d ON d.message_id=m.id WHERE m.job_id=? AND m.sender=? AND m.kind='question' AND d.status='answered' LIMIT 1", (job['id'], actor)).fetchone()
        if not answered: raise api.BadRequest('Ask another teammate a substantive question with team_send and receive a reply before delivery')
        sources = [dict(row) for row in conn.execute("SELECT * FROM research_sources WHERE job_id=? AND reader=? AND status='ok'", (job['id'], actor))]
        if not any(s['url'] in p['body'] or s['final_url'] in p['body'] for s in sources for p in pages):
            raise api.BadRequest('Cite at least one source recorded with source_read_file or source_fetch in your linked evidence page')


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


def read_file_source(conn, job_id, actor, path, start_line=1, end_line=None, *, ticket_id=None):
    """Persist actual workspace text using the existing auditable source schema."""
    from .workspace import workspace_path, DEFAULT_WORKSPACE
    member(conn, job_id, actor)
    ticket = api.get_ticket(conn, ticket_id or job_id)
    job = job_for_ticket(conn, ticket['id'])
    if not job or job['id'] != job_id:
        raise api.BadRequest('Read file evidence only within your assigned job')
    if ticket_id and ticket['assignee'] != actor:
        raise api.BadRequest('Read file evidence from your assigned ticket workspace')
    settings = dict(conn.execute('SELECT key,value FROM runner_settings'))
    root = Path(workspace_path({'workspace': ticket.get('workspace') or job.get('workspace') or settings.get('workspace', str(DEFAULT_WORKSPACE))})).resolve()
    candidate = Path(path)
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not candidate.is_relative_to(root):
        raise api.BadRequest('File evidence must stay within the assigned workspace')
    if start_line < 1 or (end_line is not None and (end_line < start_line or end_line - start_line >= 200)):
        raise api.BadRequest('Choose a valid range of at most 200 file lines')
    try:
        with candidate.open('rb') as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024: raise api.BadRequest('File evidence supports text files up to 2 MiB')
        text = raw.decode('utf-8-sig')
        if '\x00' in text: raise api.BadRequest('File evidence must be UTF-8 text')
    except (OSError, UnicodeError) as exc:
        raise api.BadRequest(f'Cannot read UTF-8 file evidence: {exc}') from exc
    lines = text.splitlines()
    end_line = end_line if end_line is not None else min(len(lines), start_line + 199)
    if start_line > len(lines) or end_line > len(lines): raise api.BadRequest('Requested lines are outside the file')
    excerpt = '\n'.join(f'{i + 1}: {lines[i]}' for i in range(start_line - 1, end_line))
    if len(excerpt.encode('utf-8')) > 24000: raise api.BadRequest('File excerpt is too large; request fewer lines')
    url = candidate.as_uri() + f'#L{start_line}-L{end_line}'
    body = f'File: {candidate}\nSHA256: {hashlib.sha256(raw).hexdigest()}\nLines: {start_line}-{end_line}\n\n{excerpt}'
    sid = conn.execute("INSERT INTO research_sources(job_id,reader,url,final_url,title,status,body,retrieved_at) VALUES(?,?,?,?,?,'ok',?,?)", (job_id, actor, url, url, candidate.name, body, api.now())).lastrowid
    api.log_activity(conn, actor, 'source.ok', f'ticket:{job_id}', url)
    conn.commit()
    return source_get(conn, sid)


def source_get(conn, source_id):
    row = conn.execute('SELECT * FROM research_sources WHERE id=?', (source_id,)).fetchone()
    if not row: raise api.NotFound('Retrieved source not found')
    return dict(row)
