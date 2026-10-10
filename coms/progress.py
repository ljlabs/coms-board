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


def snapshot(conn, ticket=None, agent=None):
    filters, args = [], []
    if ticket is not None:
        filters.append('(p.ticket_id=? OR t.parent_id=?)')
        args.extend([int(ticket), int(ticket)])
    if agent:
        filters.append('p.agent_slug=?')
        args.append(agent)
    where = 'WHERE ' + ' AND '.join(filters) if filters else ''
    rows = [dict(row) for row in conn.execute('SELECT p.*,t.title AS ticket_title,t.status AS ticket_status,a.name AS agent_name FROM agent_runtime p LEFT JOIN tickets t ON t.id=p.ticket_id LEFT JOIN agents a ON a.slug=p.agent_slug '
            + where + " ORDER BY CASE WHEN p.state='running' THEN 0 WHEN p.state='waiting' THEN 1 ELSE 2 END,p.updated_at DESC LIMIT 80", args)]
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
