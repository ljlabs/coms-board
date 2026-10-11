"""Small live snapshots and coalesced ACP output, without storing thought text."""
import json
import time
from datetime import datetime, timezone

from . import api, db


class Recorder:
    def __init__(self, key, agent_slug, ticket_id=None, run_id=None, backend='', *, message_kind='acp-response'):
        self.key, self.actor, self.tid, self.rid = key, agent_slug, ticket_id, run_id
        self.message_kind = message_kind
        self.state, self.phase, self.summary = 'running', 'starting', 'Starting agent session'
        self.preview, self.response_chars, self.thinking_updates, self.tool_calls = '', 0, 0, 0
        self.buffer, self.message_id, self.message_row, self.tools = '', None, None, {}
        self.last_flush = 0
        self.event_time = api.now()
        conn = db.connect()
        conn.execute('INSERT OR REPLACE INTO agent_runtime(key,agent_slug,ticket_id,run_id,backend,state,phase,summary,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (key, agent_slug, ticket_id, run_id, backend, self.state, self.phase, self.summary, self.event_time, self.event_time))
        api.log_activity(conn, self.actor, 'runner.start', self.ref, self.summary)
        conn.commit()

    @property
    def ref(self): return f'ticket:{self.tid}' if self.tid is not None else 'agent:' + self.actor

    def flush(self, force=False):
        if not force and time.monotonic() - self.last_flush < .5: return
        conn = db.connect()
        if self.buffer and self.tid is not None:
            if self.message_row is None:
                self.message_row = conn.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (self.tid, self.rid, 'assistant', self.message_kind, self.buffer, self.event_time, self.event_time)).lastrowid
            else:
                conn.execute('UPDATE agent_messages SET body=body||?,updated_at=? WHERE id=?', (self.buffer, self.event_time, self.message_row))
            self.buffer = ''
        conn.execute('UPDATE agent_runtime SET state=?,phase=?,summary=?,preview=?,response_chars=?,thinking_updates=?,tool_calls=?,updated_at=? WHERE key=?',
                     (self.state, self.phase, self.summary, self.preview[-2000:], self.response_chars, self.thinking_updates, self.tool_calls, self.event_time, self.key))
        api.touch_agent(conn, self.actor)
        conn.commit()
        self.last_flush = time.monotonic()

    def set(self, phase, summary, force=True):
        changed = self.phase != phase
        self.phase, self.summary, self.event_time = phase, summary, api.now()
        self.flush(force or changed)

    def notification(self, update):
        kind = update.get('sessionUpdate')
        text = (update.get('content') or {}).get('text', '') if isinstance(update.get('content'), dict) else ''
        if kind == 'agent_thought_chunk':
            self.thinking_updates += 1
            self.set('thinking', 'Model is thinking', force=False)
        elif kind == 'agent_message_chunk' and text:
            identity = update.get('messageId') or 'response'
            if identity != self.message_id:
                self.flush(True)
                self.message_id, self.message_row, self.preview = identity, None, ''
            self.buffer += text
            self.preview = (self.preview + text)[-2000:]
            self.response_chars += len(text)
            self.set('responding', 'Writing response', force=False)
        elif kind in ('tool_call', 'tool_call_update'):
            self.flush(True)
            self.message_id, self.message_row = None, None
            identity = update.get('toolCallId') or str(len(self.tools))
            record = self.tools.get(identity)
            payload = {**(record[1] if record else {}), **update}
            title = payload.get('title') or 'Agent tool'
            state = payload.get('status') or 'pending'
            conn = db.connect()
            if record:
                row = record[0]
                conn.execute('UPDATE agent_messages SET body=?,updated_at=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), api.now(), row))
            elif self.tid is not None:
                row = conn.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (self.tid, self.rid, 'tool', 'acp-tool_call', json.dumps(payload, ensure_ascii=False), api.now(), api.now())).lastrowid
                self.tool_calls += 1
                api.log_activity(conn, self.actor, 'runner.tool', self.ref, title)
            else:
                row = None
                self.tool_calls += int(record is None)
            self.tools[identity] = (row, payload)
            conn.commit()
            self.set('tool', f'{title} · {state}')
        else:
            self.event_time = api.now()
            self.flush()

    def finish(self, state='completed', summary='Agent finished'):
        self.state = state
        self.set(state, summary)
        conn = db.connect()
        conn.execute("UPDATE agent_runtime SET finished_at=? WHERE key=?", ('' if state == 'waiting' else api.now(), self.key))
        api.log_activity(conn, self.actor, 'runner.' + state, self.ref, summary)
        conn.commit()


def _returned_tickets(value, depth=0):
    """Read exact ticket creation results from older ACP tool records."""
    if depth > 8:
        return
    if isinstance(value, str):
        try: value = json.loads(value)
        except (ValueError, TypeError): return
    if isinstance(value, list):
        for child in value: yield from _returned_tickets(child, depth + 1)
    elif isinstance(value, dict) and not value.get('isError'):
        if isinstance(value.get('id'), int) and 'title' in value and 'assignee' in value:
            yield value['id']
        for key in ('content', 'text', 'result', 'output', 'ticket'):
            if key in value: yield from _returned_tickets(value[key], depth + 1)


def empty_activity():
    return {'active_count': 0, 'waiting_count': 0, 'queued_count': 0}


def activity_status(data, kind, key):
    counts = data[kind].get(key, empty_activity())
    return {'working': any(counts.values()), 'activity': counts}


def activity(conn):
    """Aggregate durable work across all descendants, independently of UI limits."""
    tickets = {row['id']: dict(row) for row in conn.execute('SELECT id,title,assignee,requester,status,parent_id,source_ticket_id,source_session_id,ready_for_execution FROM tickets')}
    agents = {row['slug']: dict(row) for row in conn.execute('SELECT slug,name,role FROM agents')}
    sessions = {row['id']: dict(row) for row in conn.execute('SELECT id,title FROM orchestrator_sessions')}
    schedule_runs = {row['ticket_id']: dict(row) for row in conn.execute(
        'SELECT r.ticket_id,s.id,s.name FROM scheduled_task_runs r JOIN scheduled_tasks s ON s.id=r.task_id')}
    chat_sources = {tid: t['source_session_id'] for tid, t in tickets.items() if t['source_session_id'] in sessions}
    # Old tickets have no explicit chat ID. Never assign all orchestrator work
    # to the latest chat: only successful, exact creation outputs establish it.
    for row in conn.execute("SELECT session_id,metadata_json FROM orchestrator_messages WHERE kind='tool_call' AND state='completed' AND (metadata_json LIKE '%ticket_create%' OR metadata_json LIKE '%team_delegate%') ORDER BY id"):
        if row['session_id'] not in sessions: continue
        try: payload = json.loads(row['metadata_json'])
        except (ValueError, TypeError): continue
        if not isinstance(payload, dict): continue
        name = str(payload.get('name') or payload.get('title') or '')
        if not any(tool in name for tool in ('ticket_create', 'team_delegate')): continue
        for tid in _returned_tickets(payload.get('rawOutput') or payload.get('content')):
            if tid in tickets: chat_sources.setdefault(tid, row['session_id'])
    lineage, contexts = {}, {}
    for tid, ticket in tickets.items():
        seen, pending, ordered = set(), [tid], []
        while pending:
            current = pending.pop(0)
            if current in seen or current not in tickets: continue
            seen.add(current); ordered.append(current)
            parent = tickets[current]
            pending.extend(pid for pid in (parent['source_ticket_id'], parent['parent_id']) if pid is not None)
        lineage[tid] = seen
        source_parent = tickets.get(ticket['source_ticket_id'] or ticket['parent_id'], {})
        slug = ticket['requester'] if ticket['requester'] in agents and agents[ticket['requester']]['role'] != 'human' else source_parent.get('assignee')
        source = {'kind': 'agent', 'slug': slug, 'name': agents.get(slug, {}).get('name', slug),
                  'ticket_id': source_parent.get('id')} if slug else None
        schedule = next((schedule_runs[node] for node in ordered if node in schedule_runs), None)
        sid = next((chat_sources[node] for node in ordered if node in chat_sources), None)
        contexts[tid] = {'source': source, 'schedule': schedule, 'orchestrator_session': sessions.get(sid)}
    runtime = [dict(row) for row in conn.execute('SELECT p.*,t.title AS ticket_title,t.status AS ticket_status,a.name AS agent_name FROM agent_runtime p LEFT JOIN tickets t ON t.id=p.ticket_id LEFT JOIN agents a ON a.slug=p.agent_slug ORDER BY p.updated_at DESC,p.started_at DESC,p.key DESC')]
    out = {'tickets': {}, 'sessions': {}, 'schedules': {}, '_lineage': lineage,
           '_contexts': contexts, '_runtime': runtime, '_sessions': sessions}

    def count(tid, field):
        for ancestor in lineage.get(tid, {tid}):
            counts = out['tickets'].setdefault(ancestor, empty_activity())
            counts[field] += 1
        context = contexts.get(tid, {})
        for kind, source in (('sessions', context.get('orchestrator_session')), ('schedules', context.get('schedule'))):
            if source:
                counts = out[kind].setdefault(source['id'], empty_activity())
                counts[field] += 1

    active_tickets = set()
    for row in runtime:
        tid = row['ticket_id']
        if tid not in tickets or tickets[tid]['status'] in ('done', 'wontfix'): continue
        if row['state'] in ('running', 'waiting'):
            count(tid, 'active_count' if row['state'] == 'running' else 'waiting_count')
            active_tickets.add(tid)
    for tid, ticket in tickets.items():
        if tid not in active_tickets and ticket['status'] == 'open' and ticket['ready_for_execution']:
            count(tid, 'queued_count')
    turns = [dict(row) for row in conn.execute('SELECT id,session_id,status FROM orchestrator_turns ORDER BY started_at,id')]
    out['_chat_sessions'] = {turn['id']: turn['session_id'] for turn in turns}
    for turn in turns:
        if turn['status'] == 'running':
            out['sessions'].setdefault(turn['session_id'], empty_activity())['active_count'] += 1
    return out


def snapshot(conn, ticket=None, agent=None, *, activity_data=None):
    data = activity_data if activity_data is not None else activity(conn)
    rows, latest_tickets, latest_chats = [], set(), set()
    for raw in data['_runtime']:
        item = dict(raw)
        tid = item['ticket_id']
        sid = data['_chat_sessions'].get(item['key'].split(':', 1)[-1]) if item['key'].startswith('chat:') else None
        latest = tid not in latest_tickets if tid is not None else sid not in latest_chats
        latest_tickets.add(tid); latest_chats.add(sid)
        context = data['_contexts'].get(tid, {'source': None, 'schedule': None, 'orchestrator_session': None})
        item.update(context)
        if sid: item['orchestrator_session'] = data['_sessions'].get(sid)
        if item['ticket_status'] in ('done', 'wontfix') and item['state'] == 'waiting':
            item.update(own_state='waiting', state='completed', phase='completed', summary='Ticket closed')
        counts = data['tickets'].get(tid, empty_activity()) if tid is not None else data['sessions'].get(sid, empty_activity())
        if latest and item['state'] not in ('running', 'waiting') and any(counts.values()) and item['ticket_status'] not in ('done', 'wontfix'):
            item.update(own_state=item['state'], state='waiting', phase='waiting', summary='Waiting for delegated work')
        if ticket is not None and int(ticket) not in data['_lineage'].get(tid, set()): continue
        if agent and item['agent_slug'] != agent: continue
        rows.append(item)
    rows.sort(key=lambda row: row['updated_at'], reverse=True)
    rows.sort(key=lambda row: {'running': 0, 'waiting': 1}.get(row['state'], 2))
    rows = rows[:80]
    now = datetime.now(timezone.utc)
    for item in rows:
        prefix = item['key'].split(':', 1)[0]
        item['stage'] = {'review': 'review', 'ticket-chat': 'conversation', 'chat': 'conversation'}.get(prefix, 'execute')
        if prefix == 'run' and item['run_id']:
            run = conn.execute('SELECT stage FROM agent_runs WHERE id=?', (item['run_id'],)).fetchone()
            if run: item['stage'] = run['stage']
        try: item['idle_seconds'] = max(0, int((now - datetime.fromisoformat(item['updated_at'].replace('Z', '+00:00'))).total_seconds()))
        except ValueError: item['idle_seconds'] = 0
    return {'generated_at': api.now(), 'items': rows,
            'active_count': sum(row['state'] == 'running' for row in rows),
            'waiting_count': sum(row['state'] == 'waiting' for row in rows)}
