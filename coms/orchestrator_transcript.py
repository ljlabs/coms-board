"""Persist ACP v1 messages and tool patches as an ordered conversation."""
from __future__ import annotations

import json
import threading
import uuid

from . import db
from .api import now


_changes = threading.Condition()
_revision = 0


def publish_change():
    """Wake live readers only after a transcript transaction has committed."""
    global _revision
    with _changes:
        _revision += 1
        _changes.notify_all()


def wait_for_change(revision=None, timeout=1):
    with _changes:
        if revision == _revision:
            _changes.wait_for(lambda: revision != _revision, timeout=timeout)
        return _revision


def message_dict(row):
    out = dict(row)
    try:
        out['data'] = json.loads(out.pop('metadata_json', '{}'))
    except (TypeError, ValueError):
        out['data'] = {}
    if not isinstance(out['data'], dict): out['data'] = {}
    return out


def conversation_messages(conn, sid):
    """Return public transcript entries, keeping ACP thought chunks private."""
    result = []
    rows = conn.execute("SELECT * FROM orchestrator_messages WHERE session_id=? "
                        "AND kind NOT IN ('log','thinking') ORDER BY id", (sid,))
    for row in rows:
        entry = message_dict(row)
        result.append(entry)
    return result


def _insert(conn, sid, turn_id, role, kind, body='', *, data=None,
            state='completed', acp_message_id=None, tool_call_id=None):
    ts = now()
    return conn.execute(
        'INSERT INTO orchestrator_messages(session_id,turn_id,role,kind,body,metadata_json,state,'
        'acp_message_id,tool_call_id,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)',
        (sid,turn_id,role,kind,body,json.dumps(data or {},ensure_ascii=False),state,
         acp_message_id,tool_call_id,ts,ts)).lastrowid


def begin_turn(sid, text, backend, model, workspace, *, replace_user_id=None):
    conn = db.connect()
    turn_id = uuid.uuid4().hex
    ts = now()
    with conn:
        if not conn.execute('SELECT 1 FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone():
            raise ValueError('session not found')
        if replace_user_id is None:
            user_id = _insert(conn,sid,turn_id,'user','text',text)
        else:
            row = conn.execute("SELECT * FROM orchestrator_messages WHERE session_id=? AND id=? AND role='user' AND kind='text'",
                               (sid, replace_user_id)).fetchone()
            if not row: raise ValueError('Only your text messages in this chat can be edited.')
            conn.execute('DELETE FROM orchestrator_turns WHERE session_id=? AND (user_message_id>=? OR id=?)',
                         (sid, row['id'], row['turn_id']))
            conn.execute('DELETE FROM orchestrator_messages WHERE session_id=? AND id>?', (sid,row['id']))
            conn.execute("UPDATE orchestrator_messages SET body=?,turn_id=?,metadata_json='{}',acp_message_id=NULL,state='completed',updated_at=?,version=version+1 WHERE id=?",
                         (text,turn_id,ts,row['id']))
            conn.execute("UPDATE orchestrator_sessions SET acp_session_id='' WHERE id=?", (sid,))
            user_id = row['id']
        conn.execute('INSERT INTO orchestrator_turns(id,session_id,user_message_id,backend,model,workspace,started_at) '
                     'VALUES(?,?,?,?,?,?,?)',(turn_id,sid,user_id,backend,model,workspace,ts))
        conn.execute("UPDATE orchestrator_sessions SET title=CASE WHEN title='New chat' THEN ? ELSE title END,updated_at=? WHERE id=?",
                     (text[:60],ts,sid))
    publish_change()
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
        self.current_kind = None
        self.lock = threading.RLock()
        self.finished = turn['status'] != 'running'

    def _seal(self, conn, state='completed'):
        if self.current_text:
            conn.execute('UPDATE orchestrator_messages SET state=?,updated_at=?,version=version+1 WHERE id=?',
                         (state,now(),self.current_text))
        self.current_text = None
        self.current_message = None
        self.current_kind = None

    def _log(self, conn, event):
        _insert(conn,self.sid,self.turn_id,'system','log',json.dumps(event,ensure_ascii=False))

    def log(self, line):
        with self.lock:
            with db.connect() as conn:
                _insert(conn,self.sid,self.turn_id,'system','log',str(line))
            publish_change()

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
                if kind == 'agent_thought_chunk':
                    self._log(conn,event)
                elif kind == 'agent_message_chunk':
                    message_kind = 'text'
                    content = event.get('content')
                    if not isinstance(content,dict): return
                    body = content.get('text') if content.get('type') == 'text' else None
                    if body is not None and not isinstance(body,str): return
                    if body == '': return
                    ident = event.get('messageId')
                    if ident is not None and not isinstance(ident,str): ident = None
                    if self.current_text and (message_kind != self.current_kind or (ident is not None and ident != self.current_message)):
                        self._seal(conn)
                    if not self.current_text:
                        self.current_text = _insert(conn,self.sid,self.turn_id,'assistant',message_kind,body or '',
                                                    state='streaming',acp_message_id=ident,
                                                    data={} if body is not None else {'blocks':[content]})
                        self.current_message = ident
                        self.current_kind = message_kind
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
            publish_change()

    def permission(self, params, decision):
        with self.lock:
            if self.finished: return
            tool = params.get('toolCall')
            if not isinstance(tool,dict): return
            with db.connect() as conn:
                self._tool(conn,{**tool,'permission':decision})
            publish_change()

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
                public = conn.execute("SELECT 1 FROM orchestrator_messages WHERE turn_id=? AND kind IN ('text','tool_call') AND role IN ('assistant','tool') LIMIT 1",(self.turn_id,)).fetchone()
                if not public and not explanation:
                    explanation = 'The backend ended this turn without public output.'
                if explanation:
                    _insert(conn,self.sid,self.turn_id,'system','error',explanation,state=status)
                conn.execute('UPDATE orchestrator_turns SET status=?,stop_reason=?,error=?,finished_at=? WHERE id=?',
                             (status,stop_reason,error,now(),self.turn_id))
            self.finished = True
            publish_change()


def fork_conversation(conn, sid, message_id):
    """Copy an immutable prefix; ACP sessions and all local IDs are independent."""
    with conn:
        source = conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?', (sid,)).fetchone()
        if not source: raise ValueError('session not found')
        target = conn.execute('SELECT * FROM orchestrator_messages WHERE session_id=? AND id=?', (sid,message_id)).fetchone()
        if not target: raise ValueError('message not found in this chat')
        if not ((target['role']=='assistant' and target['kind']=='text') or
                (target['role']=='system' and target['kind']=='error')):
            raise ValueError('Fork at an agent response or a terminal response error.')
        if target['state'] in ('streaming','pending','in_progress') or conn.execute(
                "SELECT 1 FROM orchestrator_turns WHERE session_id=? AND status='running'", (sid,)).fetchone():
            raise ValueError('Wait for this chat to finish responding before forking it.')
        new_sid = uuid.uuid4().hex
        ts = now()
        conn.execute('INSERT INTO orchestrator_sessions(id,title,project_id,backend,workspace,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                     (new_sid,'Fork of: '+source['title'],source['project_id'],source['backend'],source['workspace'],ts,ts))
        rows = conn.execute('SELECT * FROM orchestrator_messages WHERE session_id=? AND id<=? ORDER BY id', (sid,message_id)).fetchall()
        turn_ids = {row['turn_id']:uuid.uuid4().hex for row in rows if row['turn_id']}
        message_ids = {}
        for row in rows:
            message_ids[row['id']] = conn.execute(
                'INSERT INTO orchestrator_messages(session_id,turn_id,role,kind,body,metadata_json,state,acp_message_id,tool_call_id,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (new_sid,turn_ids.get(row['turn_id']),row['role'],row['kind'],row['body'],row['metadata_json'],
                 row['state'],None,row['tool_call_id'],row['created_at'],row['updated_at'],row['version'])).lastrowid
        for old_id, new_id in turn_ids.items():
            turn = conn.execute('SELECT * FROM orchestrator_turns WHERE session_id=? AND id=?', (sid,old_id)).fetchone()
            if not turn: continue
            cut = conn.execute('SELECT 1 FROM orchestrator_messages WHERE session_id=? AND turn_id=? AND id>?', (sid,old_id,message_id)).fetchone()
            conn.execute('INSERT INTO orchestrator_turns(id,session_id,user_message_id,backend,model,workspace,status,stop_reason,error,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (new_id,new_sid,message_ids.get(turn['user_message_id']),turn['backend'],turn['model'],turn['workspace'],
                          'completed' if cut else turn['status'],'fork' if cut else turn['stop_reason'],'' if cut else turn['error'],
                          turn['started_at'],ts if cut else turn['finished_at']))
    publish_change()
    return dict(conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?', (new_sid,)).fetchone())


def history_before(sid, user_message_id):
    lines = []
    for row in db.connect().execute('SELECT * FROM orchestrator_messages WHERE session_id=? AND id<? ORDER BY id',(sid,user_message_id)):
        entry = message_dict(row)
        if entry['kind']=='text' and entry['role'] in ('user','assistant'):
            if entry['body']: lines.append(('User: ' if entry['role']=='user' else 'Orchestrator: ') + entry['body'])
            if entry['data'].get('blocks'): lines.append('Message content: ' + json.dumps(entry['data']['blocks'],ensure_ascii=False))
        elif entry['kind']=='tool_call':
            payload = json.dumps(entry['data'],ensure_ascii=False)
            lines.append('Tool: ' + payload)
        elif entry['kind']=='plan':
            lines.append('Plan: ' + json.dumps(entry['data'],ensure_ascii=False))
        elif entry['kind']=='error':
            lines.append('Turn status: ' + entry['body'])
    return '\n\n'.join(lines)


def recover_turns(conn):
    for row in conn.execute("SELECT id,session_id FROM orchestrator_turns WHERE status='running'").fetchall():
        Transcript(row['session_id'],row['id']).finish(
            error='Server stopped before this response finished. Send a new message to continue.',status='interrupted')
