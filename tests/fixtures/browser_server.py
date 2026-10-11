"""Isolated browser fixture: real HTTP routes, seeded data, no live agents."""
import sys
import shlex
import threading
import json
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from http.server import ThreadingHTTPServer
from coms import api, db, skill_store
from coms.server import Handler
from coms.runner import RUNNER
from coms.orchestrator_transcript import Transcript, begin_turn

skill_store.SKILLS_ROOT = Path(db.db_path()).parent / 'skills'
shutil.copytree(Path(__file__).resolve().parents[2] / 'skills', skill_store.SKILLS_ROOT,
                ignore=shutil.ignore_patterns('.*'))
conn = db.connect()
t = api.create_ticket(conn, 'Browser regression ticket', '\n\n'.join('Paragraph ' + str(i) for i in range(45)), 'admin')
api.ask_question(conn, 'Browser question', 'Question body', 'other')
api.save_wiki(conn, 'browser-page', 'Browser page', 'Wiki body', 'admin')
workspace=Path(db.db_path()).parent/'workspace'
workspace.mkdir(exist_ok=True)
for key,value in {'active_backend':'codex','workspace':str(workspace),'model_codex':'gpt-6-luna',
                  'codex_command':shlex.join([sys.executable,str(Path(__file__).with_name('fake_acp.py'))])}.items():
    conn.execute("INSERT INTO runner_settings(key,value) VALUES(?,?)", (key,value))
conn.execute("INSERT INTO orchestrator_sessions(id,title,created_at,updated_at) VALUES('browser-chat','Browser chat',?,?)", (api.now(), api.now()))
for i in range(60):
    conn.execute("INSERT INTO agent_messages(ticket_id,role,kind,body,created_at) VALUES(?,'user','text',?,?)", (t['id'], 'Ticket prompt ' + str(i), api.now()))
    conn.execute("INSERT INTO orchestrator_messages(session_id,role,kind,body,created_at) VALUES('browser-chat',?,'text',?,?)", ('assistant' if i % 2 else 'user', 'Message ' + str(i) + '\n\n' + 'Long paragraph. ' * 30, api.now()))
rid = conn.execute("INSERT INTO agent_runs(ticket_id,backend,session_id,status,stage,agent_slug) VALUES(?,'codex','old-session','completed','execute','orchestrator')", (t['id'],)).lastrowid
for role,kind,body in [
    ('assistant','acp-response','**Weather report**\n\n- Temperature: 12.8 °C'),
    ('tool','acp-tool_call',json.dumps({'toolCallId':'weather','title':'Fetch weather','status':'completed','rawInput':{'location':'London'},'rawOutput':{'temperature':12.8}})),
    ('assistant','review-decision',json.dumps({'decision':'close','reason':'Report delivered.'})),
]:
    conn.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,?,?,?,?)', (t['id'],rid,role,kind,body,api.now()))
conn.commit()
conn.execute("INSERT INTO orchestrator_sessions(id,title,created_at,updated_at) VALUES('browser-tools','Tool conversation',?,?)", (api.now(),api.now()))
conn.commit()
turn, _ = begin_turn('browser-tools','List files','codex','gpt-6-luna',str(workspace))
transcript = Transcript('browser-tools',turn['id'])
transcript.apply({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'I’ll list the workspace.'}})
transcript.apply({'sessionUpdate':'tool_call','toolCallId':'list','title':'List workspace','kind':'execute','rawInput':{'command':'ls','cwd':str(workspace)}})
transcript.apply({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'The listing is ready.'}})
transcript.apply({'sessionUpdate':'tool_call_update','toolCallId':'list','status':'completed','rawOutput':{'stdout':'src\n','exitCode':0}})
transcript.finish('end_turn')
failed, _ = begin_turn('browser-tools','Try again','codex','gpt-6-luna',str(workspace))
Transcript('browser-tools',failed['id']).finish(error='Backend disconnected')
conn.execute("INSERT INTO orchestrator_sessions(id,title,created_at,updated_at) VALUES('browser-codex','Codex adapter trace',?,?)", (api.now(),api.now()))
conn.commit()
codex_turn,_=begin_turn('browser-codex','List the workspace','codex','gpt-6-luna',str(workspace))
codex_transcript=Transcript('browser-codex',codex_turn['id'])
for event in json.loads(Path(__file__).with_name('codex_acp_workspace.json').read_text(encoding='utf-8'))['events']:
    codex_transcript.apply(event)
codex_transcript.finish('end_turn')
# Real HTTP send/reload regressions use a deterministic ACP subprocess.
edit_ticket = api.create_ticket(conn, 'Chat edit fixture', 'Test message regeneration.', 'admin', assignee='orchestrator')
conn.execute("INSERT INTO agent_runs(ticket_id,backend,session_id,status,stage,agent_slug) VALUES(?,'codex','edit-fixture','completed','execute','orchestrator')", (edit_ticket['id'],))
conn.execute("INSERT INTO agent_messages(ticket_id,role,kind,body,created_at) VALUES(?,'assistant','acp-response','Original execution result',?)", (edit_ticket['id'],api.now()))
conn.commit()
import os
os.environ['COMS_FAKE_MODE'] = 'chat-tools'
class FixtureHandler(Handler):
    def do_POST(self):
        if self.path == '/__test_shutdown' and self._trusted_request():
            self._json(200, {'stopping':True})
            threading.Thread(target=self.server.shutdown,daemon=True).start()
        else:
            super().do_POST()


srv = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
print(srv.server_port, flush=True)
try:
    srv.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    RUNNER.stop()
    srv.server_close()
    srv.server_close()
