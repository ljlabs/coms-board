"""Persist ACP v1 messages and tool patches as an ordered conversation."""
from __future__ import annotations

import json
import threading
import uuid

from . import db
from .api import now


def message_dict(row):
    out = dict(row)
    try:
        out['data'] = json.loads(out.pop('metadata_json', '{}'))
    except (TypeError, ValueError):
        out['data'] = {}
    if not isinstance(out['data'], dict): out['data'] = {}
    return out


def _insert(conn, sid, turn_id, role, kind, body='', *, data=None,
            state='completed', acp_message_id=None, tool_call_id=None):
    ts = now()
    return conn.execute(
        'INSERT INTO orchestrator_messages(session_id,turn_id,role,kind,body,metadata_json,state,'
        'acp_message_id,tool_call_id,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)',
        (sid,turn_id,role,kind,body,json.dumps(data or {},ensure_ascii=False),state,
         acp_message_id,tool_call_id,ts,ts)).lastrowid


def begin_turn(sid, text, backend, model, workspace):
    conn = db.connect()
    turn_id = uuid.uuid4().hex
    ts = now()
    with conn:
        if not conn.execute('SELECT 1 FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone():
            raise ValueError('session not found')
        user_id = _insert(conn,sid,turn_id,'user','text',text)
        conn.execute('INSERT INTO orchestrator_turns(id,session_id,user_message_id,backend,model,workspace,started_at) '
                     'VALUES(?,?,?,?,?,?,?)',(turn_id,sid,user_id,backend,model,workspace,ts))
        conn.execute("UPDATE orchestrator_sessions SET title=CASE WHEN title='New chat' THEN ? ELSE title END,updated_at=? WHERE id=?",
                     (text[:60],ts,sid))
    return (dict(conn.execute('SELECT * FROM orchestrator_turns WHERE id=?',(turn_id,)).fetchone()),
            message_dict(conn.execute('SELECT * FROM orchestrator_messages WHERE id=?',(user_id,)).fetchone()))


class Transcript:
    """One turn; notifications, requests, and completion share this lock."""

    def __init__(self, sid, turn_id):
        self.sid = sid
        self.turn_id = turn_id
        turn = db.connect().execute('SELECT * FROM orchestrator_turns WHERE id=? AND session_id=?',(turn_id,sid)).fetchone()
        if not turn: raise ValueError('turn not found')
        self.workspace = turn['workspace']
        self.user_id = turn['user_message_id']
        self.current_text = None
        self.current_message = None
        self.lock = threading.RLock()
        self.finished = turn['status'] != 'running'

    def _seal(self, conn, state='completed'):
        if self.current_text:
            conn.execute('UPDATE orchestrator_messages SET state=?,updated_at=?,version=version+1 WHERE id=?',
                         (state,now(),self.current_text))
        self.current_text = None
        self.current_message = None

    def _log(self, conn, event):
        _insert(conn,self.sid,self.turn_id,'system','log',json.dumps(event,ensure_ascii=False))

    def log(self, line):
        with self.lock:
            with db.connect() as conn:
                _insert(conn,self.sid,self.turn_id,'system','log',str(line))

    def _tool(self, conn, event):
        ident = event.get('toolCallId')
        if not isinstance(ident,str) or not ident:
            self._log(conn,event)
            return
        row = conn.execute('SELECT * FROM orchestrator_messages WHERE session_id=? AND turn_id=? AND tool_call_id=?',
                           (self.sid,self.turn_id,ident)).fetchone()
        if row:
            data = message_dict(row)['data']
            state = row['state']
        else:
            self._seal(conn)
            data = {'workspace':self.workspace}
            state = 'pending'
        for key in ('name','title','kind','status','rawInput','rawOutput','content','locations','_meta','permission'):
            # ACP v1: null and omitted fields leave a prior value untouched.
            # An explicitly empty array is a replacement, not an append.
            if key in event and event[key] is not None:
                data[key] = event[key]
        if isinstance(data.get('status'),str): state = data['status']
        data['status'] = state
        payload = json.dumps(data,ensure_ascii=False)
        if row:
            if payload != row['metadata_json'] or state != row['state']:
                conn.execute('UPDATE orchestrator_messages SET metadata_json=?,state=?,updated_at=?,version=version+1 WHERE id=?',
                             (payload,state,now(),row['id']))
        else:
            _insert(conn,self.sid,self.turn_id,'tool','tool_call',data=data,state=state,tool_call_id=ident)

    def apply(self, event):
        if not isinstance(event,dict): return
        with self.lock:
            if self.finished: return
            with db.connect() as conn:
                kind = event.get('sessionUpdate')
                if kind == 'agent_message_chunk':
                    content = event.get('content')
                    if not isinstance(content,dict): return
                    body = content.get('text') if content.get('type') == 'text' else None
                    if body is not None and not isinstance(body,str): return
                    if body == '': return
                    ident = event.get('messageId')
                    if ident is not None and not isinstance(ident,str): ident = None
                    if self.current_text and ident is not None and ident != self.current_message:
                        self._seal(conn)
                    if not self.current_text:
                        self.current_text = _insert(conn,self.sid,self.turn_id,'assistant','text',body or '',
                                                    state='streaming',acp_message_id=ident,
                                                    data={} if body is not None else {'blocks':[content]})
                        self.current_message = ident
                    elif body is not None:
                        row=conn.execute('SELECT * FROM orchestrator_messages WHERE id=?',(self.current_text,)).fetchone()
                        data=message_dict(row)['data']
                        if 'blocks' in data:
                            blocks=data['blocks']
                            if blocks and blocks[-1].get('type')=='text': blocks[-1]['text']+=body
                            else: blocks.append({'type':'text','text':body})
                        conn.execute('UPDATE orchestrator_messages SET body=body||?,metadata_json=?,updated_at=?,version=version+1 WHERE id=?',
                                     (body,json.dumps(data,ensure_ascii=False),now(),self.current_text))
                    else:
                        row = conn.execute('SELECT * FROM orchestrator_messages WHERE id=?',(self.current_text,)).fetchone()
                        data = message_dict(row)['data']
                        if 'blocks' not in data:
                            data['blocks']=[{'type':'text','text':row['body']}] if row['body'] else []
                        data['blocks'].append(content)
                        conn.execute('UPDATE orchestrator_messages SET metadata_json=?,updated_at=?,version=version+1 WHERE id=?',
                                     (json.dumps(data,ensure_ascii=False),now(),self.current_text))
                elif kind in ('tool_call','tool_call_update'):
                    self._tool(conn,event)
                elif kind == 'user_message_chunk':
                    ident = event.get('messageId')
                    if isinstance(ident,str):
                        conn.execute('UPDATE orchestrator_messages SET acp_message_id=? WHERE id=?',(ident,self.user_id))
                elif kind == 'plan':
                    row = conn.execute("SELECT id FROM orchestrator_messages WHERE turn_id=? AND kind='plan'",(self.turn_id,)).fetchone()
                    data = {'entries':event.get('entries') or []}
                    if row:
                        conn.execute('UPDATE orchestrator_messages SET metadata_json=?,updated_at=?,version=version+1 WHERE id=?',
                                     (json.dumps(data,ensure_ascii=False),now(),row['id']))
                    else:
                        self._seal(conn)
                        _insert(conn,self.sid,self.turn_id,'system','plan',data=data)
                else:
                    self._log(conn,event)

    def permission(self, params, decision):
        with self.lock:
            if self.finished: return
            tool = params.get('toolCall')
            if not isinstance(tool,dict): return
            with db.connect() as conn:
                self._tool(conn,{**tool,'permission':decision})

    def finish(self, stop_reason='', *, error='', status=None):
        with self.lock:
            if self.finished: return
            states = {'end_turn':'completed','cancelled':'cancelled','refusal':'refused',
                      'max_tokens':'limited','max_turn_requests':'limited'}
            status = status or ('failed' if error else states.get(stop_reason,'failed'))
            explanation = error or {
                'cancelled':'The response was cancelled.',
                'refusal':'The backend declined this request.',
                'max_tokens':'The response stopped at the token limit.',
                'max_turn_requests':'The response stopped at the request limit.',
            }.get(stop_reason,'')
            with db.connect() as conn:
                self._seal(conn,'completed' if status=='completed' else status)
                tools = conn.execute("SELECT * FROM orchestrator_messages WHERE turn_id=? AND kind='tool_call'",(self.turn_id,)).fetchall()
                unresolved = False
                for row in tools:
                    if row['state'] not in ('completed','failed','cancelled','interrupted','unknown'):
                        unresolved = True
                        data = message_dict(row)['data']
                        data['status'] = 'cancelled' if status=='cancelled' else 'interrupted'
                        conn.execute('UPDATE orchestrator_messages SET state=?,metadata_json=?,updated_at=?,version=version+1 WHERE id=?',
                                     (data['status'],json.dumps(data,ensure_ascii=False),now(),row['id']))
                if unresolved and status=='completed':
                    status = 'interrupted'
                    explanation = 'The backend ended this turn without reporting a final tool result.'
                if status=='failed' and not explanation:
                    explanation = 'The backend stopped unexpectedly' + (f' ({stop_reason}).' if stop_reason else '.')
                public = conn.execute("SELECT 1 FROM orchestrator_messages WHERE turn_id=? AND role IN ('assistant','tool') LIMIT 1",(self.turn_id,)).fetchone()
                if not public and not explanation:
                    explanation = 'The backend ended this turn without public output.'
                if explanation:
                    _insert(conn,self.sid,self.turn_id,'system','error',explanation,state=status)
                conn.execute('UPDATE orchestrator_turns SET status=?,stop_reason=?,error=?,finished_at=? WHERE id=?',
                             (status,stop_reason,error,now(),self.turn_id))
            self.finished = True


def history_before(sid, user_message_id):
    lines = []
    for row in db.connect().execute('SELECT * FROM orchestrator_messages WHERE session_id=? AND id<? ORDER BY id',(sid,user_message_id)):
        entry = message_dict(row)
        if entry['kind']=='text' and entry['role'] in ('user','assistant'):
            if entry['body']: lines.append(('User: ' if entry['role']=='user' else 'Orchestrator: ') + entry['body'])
        elif entry['kind']=='tool_call':
            payload = json.dumps(entry['data'],ensure_ascii=False)
            if len(payload)>4000: payload = payload[:4000] + ' [tool output truncated]'
            lines.append('Tool: ' + payload)
        elif entry['kind']=='error':
            lines.append('Turn status: ' + entry['body'])
    text = '\n\n'.join(lines)
    return text if len(text)<=64000 else '[Earlier conversation truncated]\n' + text[-64000:]


def recover_turns(conn):
    for row in conn.execute("SELECT id,session_id FROM orchestrator_turns WHERE status='running'").fetchall():
        Transcript(row['session_id'],row['id']).finish(
            error='Server stopped before this response finished. Send a new message to continue.',status='interrupted')
