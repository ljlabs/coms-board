"""Isolated browser fixture: real HTTP routes, seeded data, no live agents."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from http.server import ThreadingHTTPServer
from coms import api, db
from coms.server import Handler
from coms.runner import RUNNER

conn = db.connect()
t = api.create_ticket(conn, 'Browser regression ticket', '\n\n'.join('Paragraph ' + str(i) for i in range(45)), 'admin')
api.ask_question(conn, 'Browser question', 'Question body', 'other')
api.save_wiki(conn, 'browser-page', 'Browser page', 'Wiki body', 'admin')
workspace=Path(db.db_path()).parent/'workspace'
workspace.mkdir(exist_ok=True)
for key,value in {'active_backend':'codex','workspace':str(workspace),'model_codex':'gpt-6-luna',
                  'codex_command':sys.executable + ' ' + str(Path(__file__).with_name('fake_acp.py'))}.items():
    conn.execute("INSERT INTO runner_settings(key,value) VALUES(?,?)", (key,value))
conn.execute("INSERT INTO orchestrator_sessions(id,title,created_at,updated_at) VALUES('browser-chat','Browser chat',?,?)", (api.now(), api.now()))
for i in range(60):
    conn.execute("INSERT INTO agent_messages(ticket_id,role,kind,body,created_at) VALUES(?,'user','text',?,?)", (t['id'], 'Ticket prompt ' + str(i), api.now()))
    conn.execute("INSERT INTO orchestrator_messages(session_id,role,kind,body,created_at) VALUES('browser-chat',?,'text',?,?)", ('assistant' if i % 2 else 'user', 'Message ' + str(i) + '\n\n' + 'Long paragraph. ' * 30, api.now()))
conn.commit()
srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print(srv.server_port, flush=True)
try:
    srv.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    RUNNER.stop()
    srv.server_close()
