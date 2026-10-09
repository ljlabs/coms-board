import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests import *  # noqa: F401,F403
from coms import api, db, mcp
from coms.acp import AcpConnection
from coms.runner import Runner, _agent_retry_due, _review_retry_due, backend_command, board_servers
from coms.workspace import APP_ROOT, workspace_path


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'board.db')})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.workspace = Path(self.tmp.name) / 'workspace with spaces'
        self.workspace.mkdir()
        self.command = shlex.join([sys.executable, str(Path(__file__).parent / 'fixtures' / 'fake_acp.py')])
        for k, v in {'workspace': str(self.workspace), 'active_backend': 'codex', 'codex_command': self.command}.items():
            self.conn.execute('INSERT INTO runner_settings(key,value) VALUES(?,?)', (k, v))
        self.conn.commit()
        self.runner = Runner()

    def tearDown(self):
        self.runner.stop()
        conn = getattr(db._local, 'conn', None)
        if conn: conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def ticket(self):
        return api.create_ticket(self.conn, 'Research', 'Publish findings to the wiki.', 'admin', ready_for_execution=True)

    def test_workspace_rejects_application_ancestors_children_and_symlinks(self):
        for path in [APP_ROOT, APP_ROOT.parent, APP_ROOT / 'data', Path('/'), Path('relative')]:
            with self.subTest(path=path), self.assertRaises(ValueError): workspace_path({'workspace': str(path)})
        alias = Path(self.tmp.name) / 'alias'
        try:
            alias.symlink_to(APP_ROOT, target_is_directory=True)
        except OSError as exc:
            if getattr(exc,'winerror',None) != 1314: raise
        else:
            with self.assertRaises(ValueError): workspace_path({'workspace': str(alias)})
        self.assertEqual(workspace_path({'workspace': str(self.workspace)}), str(self.workspace))
        with self.assertRaises(ValueError): workspace_path({'workspace': str(self.workspace / 'missing')})

    def test_quoted_commands_and_mcp_session_contract(self):
        self.assertEqual(backend_command('codex', {'codex_command': '"/some path/adapter" --flag "two words"'}),
                         ['/some path/adapter', '--flag', 'two words'])
        c = AcpConnection('unused', str(self.workspace), on_notification=lambda m: None,
                          on_request=lambda m, r: None, on_close=lambda e: None, mcp_servers=board_servers('test'))
        with patch.object(c, 'request', return_value={'sessionId': 's'}) as request:
            c.session_new()
            self.assertIsInstance(request.call_args.args[1]['mcpServers'], list)
            server = request.call_args.args[1]['mcpServers'][0]
            self.assertEqual(server['name'], 'coms-board')
            self.assertTrue(Path(server['command']).is_absolute())
            self.assertEqual(dict((e['name'], e['value']) for e in server['env'])['COMS_DB'], os.environ['COMS_DB'])

    def test_inbound_activity_extends_request_deadline(self):
        c=AcpConnection('unused',str(self.workspace),on_notification=lambda m:None,
                        on_request=lambda m,r:None,on_close=lambda e:None)
        def stream(frame):
            def respond():
                for _ in range(12):
                    time.sleep(.025)
                    c._last_rx=time.monotonic()
                c._resolve(frame['id'],{'ok':True},None)
            worker=threading.Thread(target=respond)
            worker.start()
            return worker
        workers=[]
        with patch.object(c,'_write',side_effect=lambda frame:workers.append(stream(frame))):
            try: self.assertEqual(c.request('test/activity',{},timeout=.15),{'ok':True})
            finally:
                for worker in workers: worker.join(timeout=1)

    def test_execution_publishes_via_real_mcp_and_reaches_review(self):
        ticket = self.ticket()
        self.runner.active[ticket['id']] = True
        self.runner._work(ticket)
        run = dict(self.conn.execute('SELECT * FROM agent_runs').fetchone())
        self.assertEqual(run['status'], 'completed', run['error'])
        delivered = api.get_ticket(self.conn, ticket['id'])
        self.assertEqual(delivered['status'], 'review')
        self.assertIn('delivered-research', delivered['result'])
        self.assertEqual(delivered['links'][0]['ref'], 'delivered-research')
        self.assertEqual(api.get_wiki(self.conn, 'delivered-research')['body'], 'Published through MCP.')
        self.assertEqual(run['agent_slug'], delivered['assignee'])
        self.assertEqual(api.get_wiki(self.conn, 'delivered-research')['author'], run['agent_slug'])
        self.assertEqual(api.get_agent(self.conn, run['agent_slug'])['role'], 'expert')
        self.assertEqual(self.conn.execute('SELECT state FROM agent_runtime WHERE run_id=?', (run['id'],)).fetchone()['state'], 'completed')
        self.assertNotIn(ticket['id'], self.runner.active)

    def test_private_response_cannot_complete_ticket(self):
        ticket = self.ticket()
        with patch.dict(os.environ, {'COMS_FAKE_MODE': 'no-delivery'}): self.runner._work(ticket)
        run = dict(self.conn.execute('SELECT * FROM agent_runs').fetchone())
        self.assertEqual(run['status'], 'failed')
        self.assertIn('ticket_result', run['error'])
        self.assertEqual(api.get_ticket(self.conn, ticket['id'])['status'], 'in_progress')
        self.assertFalse(self.runner.conns)

    def test_agent_retry_backoff_is_bounded_and_grows(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(_agent_retry_due(0, now.isoformat(), now=now))
        self.assertFalse(_agent_retry_due(3, '2000-01-01T00:00:00+00:00', now=now))
        self.assertTrue(_agent_retry_due(0, '2000-01-01T00:00:00+00:00', now=now))
        self.assertTrue(_agent_retry_due(1, '2000-01-01T00:00:00+00:00', now=now))

    def test_review_retry_backoff_remains_bounded(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(_review_retry_due(3, '2000-01-01T00:00:00+00:00', now=now))

    def test_orchestrator_restores_history_when_backend_cannot_load(self):
        session = self.runner.create_orchestrator_session()
        session.update(acp_session_id='old-session', workspace=str(self.workspace))
        self.runner._orchestrator_turn('Hello', session['id'], session)
        detail = self.runner.orchestrator_detail(session['id'])
        assistant = [m['body'] for m in detail['messages'] if m['role'] == 'assistant']
        self.assertEqual(assistant, ['Private response is not a board deliverable.'])
        self.assertEqual(detail['workspace'], str(self.workspace))
        self.assertFalse(detail['busy'])

    def test_model_discovery_exposes_backend_select_options(self):
        models=self.runner.discover_models('codex')
        self.assertEqual(models['backend'],'codex')
        self.assertEqual([m['value'] for m in models['models']],['gpt-6-luna','gpt-6.1-sol'])

    def test_orchestrator_tool_timeline_and_fallback_context(self):
        session = self.runner.create_orchestrator_session()
        prompts = Path(self.tmp.name) / 'prompts.jsonl'
        with patch.dict(os.environ, {'COMS_FAKE_MODE':'chat-tools','COMS_FAKE_PROMPT_FILE':str(prompts)}):
            self.runner._orchestrator_turn('List workspace', session['id'], session)
            detail = self.runner.orchestrator_detail(session['id'])
            self.assertEqual([m['role'] for m in detail['messages']], ['user','assistant','tool','assistant'])
            self.assertEqual([m['body'] for m in detail['messages'] if m['role']=='assistant'], ['I’ll list the workspace.','Found src.'])
            tool = detail['messages'][2]
            self.assertEqual(tool['state'], 'completed')
            self.assertEqual(tool['data']['rawInput']['cwd'], str(self.workspace))
            self.assertEqual(detail['turns'][0]['status'], 'completed')
            self.runner._orchestrator_turn('Next prompt',session['id'],dict(self.conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(session['id'],)).fetchone()))
        second_prompt = json.loads(prompts.read_text(encoding='utf-8').splitlines()[1])
        self.assertIn('I’ll list the workspace.', second_prompt)
        self.assertIn('Found src.', second_prompt)
        self.assertIn('"stdout": "src\\n"', second_prompt)
        self.assertEqual(second_prompt.count('User: Next prompt'), 1)
        self.assertNotIn('Wrong session', second_prompt)

    def test_orchestrator_error_is_visible_and_replay_does_not_duplicate(self):
        session = self.runner.create_orchestrator_session()
        with patch.dict(os.environ, {'COMS_FAKE_MODE':'chat-error'}):
            self.runner._orchestrator_turn('List workspace',session['id'],session)
        detail = self.runner.orchestrator_detail(session['id'])
        self.assertEqual(detail['messages'][-1]['kind'], 'error')
        self.assertIn('Command transport failed', detail['messages'][-1]['body'])
        self.assertEqual(detail['messages'][2]['state'], 'interrupted')
        self.runner._reset_orchestrator()
        with patch.dict(os.environ, {'COMS_FAKE_LOAD':'1'}):
            self.runner._reset_orchestrator()
            self.runner._orchestrator_turn('First', session['id'], {})
            saved=dict(self.conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(session['id'],)).fetchone())
            self.runner._orchestrator_turn('Second',session['id'],saved)
        self.assertNotIn('Replayed old response', '\n'.join(m['body'] for m in self.runner.orchestrator_detail(session['id'])['messages']))

    def test_permission_request_with_colliding_rpc_id_finishes_the_same_tool(self):
        session=self.runner.create_orchestrator_session()
        with patch.dict(os.environ,{'COMS_FAKE_MODE':'chat-permission'}):
            self.runner._orchestrator_turn('List workspace',session['id'],session)
        detail=self.runner.orchestrator_detail(session['id'])
        self.assertEqual([m['role'] for m in detail['messages']],['user','assistant','tool','assistant'])
        self.assertEqual(detail['messages'][2]['data']['permission'],'allow-once')
        self.assertEqual(detail['turns'][0]['status'],'completed')

    def test_streaming_reuses_the_reader_database_connection_until_shutdown(self):
        session=self.runner.create_orchestrator_session()
        with patch.dict(os.environ,{'COMS_FAKE_MODE':'chat-tools'}), patch.object(db.sqlite3,'connect',wraps=db.sqlite3.connect) as connect:
            self.runner._orchestrator_turn('List workspace',session['id'],session)
            self.assertLessEqual(connect.call_count,2,'Every chunk must not reinitialize SQLite schema/migrations')
        self.runner.stop()

    def test_orchestrator_keeps_one_codex_process_and_overrides_stale_chat_backend(self):
        self.conn.execute("INSERT INTO runner_settings(key,value) VALUES('model_codex','gpt-6.1-sol')")
        self.conn.execute("INSERT INTO orchestrator_sessions(id,title,backend,acp_session_id,workspace,created_at,updated_at) VALUES('old-chat','Old chat','opencode','old-open-code-session',?,'now','now')",(str(self.workspace),))
        self.conn.commit()
        marker=Path(self.tmp.name)/'acp-pids'
        with patch.dict(os.environ,{'COMS_FAKE_PID_FILE':str(marker),'COMS_FAKE_IDENTITY':'1','COMS_FAKE_BACKEND':'codex'}):
            self.runner._warm_orchestrator()
            live=self.runner.orchestrator_conn
            self.assertTrue(live and live.alive)
            self.runner._orchestrator_turn('First turn','old-chat',dict(self.conn.execute("SELECT * FROM orchestrator_sessions WHERE id='old-chat'").fetchone()))
            self.runner._orchestrator_turn('Second turn','old-chat',dict(self.conn.execute("SELECT * FROM orchestrator_sessions WHERE id='old-chat'").fetchone()))
            self.assertIs(self.runner.orchestrator_conn,live)
            self.assertTrue(live.alive)
            self.assertEqual(marker.read_text().splitlines(),[str(live.proc.pid)])
        detail=self.runner.orchestrator_detail('old-chat')
        self.assertEqual(detail['backend'],'codex')
        assistant=[m['body'] for m in detail['messages'] if m['role']=='assistant']
        self.assertEqual(assistant,['codex / gpt-6.1-sol','codex / gpt-6.1-sol'])

    def test_orchestrator_restarts_only_after_model_change_and_closes_on_stop(self):
        marker=Path(self.tmp.name)/'acp-pids'
        with patch.dict(os.environ,{'COMS_FAKE_PID_FILE':str(marker)}):
            self.runner._warm_orchestrator()
            first=self.runner.orchestrator_conn
            self.assertTrue(first and first.alive)
            self.assertEqual(len(marker.read_text().splitlines()),1)
            self.runner._orchestrator_connection('codex',str(self.workspace),self.runner.settings())
            self.assertIs(self.runner.orchestrator_conn,first)
            self.conn.execute("UPDATE runner_settings SET value='gpt-6.1-sol' WHERE key='model_codex'")
            self.conn.commit()
            self.runner._reset_orchestrator()
            second=self.runner.orchestrator_conn
            self.assertIsNot(second,first)
            self.assertTrue(second and second.alive)
            self.assertEqual(len(marker.read_text().splitlines()),2)
            self.runner.stop()
            self.assertFalse(second.alive)

    def test_review_tools_are_readonly(self):
        tools = mcp.dispatch({'id': 1, 'method': 'tools/list'}, readonly=True)['result']['tools']
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in tools))
        result = mcp.dispatch({'id': 2, 'method': 'tools/call', 'params': {'name': 'wiki_put',
                               'arguments': {'slug': 'bad', 'title': 'Bad', 'body': 'Bad'}}}, readonly=True)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(api.list_wiki(self.conn), [])

    def test_mcp_rejects_invalid_arguments_and_wrong_ticket(self):
        t1, t2 = self.ticket(), self.ticket()
        for args in [{'id': True, 'result': 'bad'}, {'id': t1['id'], 'result': ''}, {'id': t1['id'], 'result': 'ok', 'unexpected': 'bad'}]:
            with self.assertRaises(ValueError): mcp.call_tool('ticket_result', args)
        with patch.dict(os.environ, {'COMS_TICKET_ID': str(t1['id'])}):
            with self.assertRaises(ValueError): mcp.call_tool('ticket_result', {'id': t2['id'], 'result': 'Wrong destination'})

    def test_start_recovers_interrupted_runs(self):
        t = self.ticket()
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,started_at) VALUES(?,'codex','running','old')", (t['id'],))
        self.conn.execute("INSERT INTO ticket_reviews(ticket_id,status,created_at) VALUES(?,'running','old')", (t['id'],))
        self.conn.commit()
        self.runner.start(warm=False)
        self.runner.stop()
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs').fetchone()[0], 'failed')
        self.assertEqual(self.conn.execute('SELECT status FROM ticket_reviews').fetchone()[0], 'failed')

    def test_second_runner_cannot_recover_or_duplicate_live_runs(self):
        self.runner.start(warm=False)
        other=Runner()
        try:
            with self.assertRaisesRegex(RuntimeError, 'already owns'):
                other.start(warm=False)
        finally: other.stop()

    def test_changed_human_assignment_is_rechecked_before_execution(self):
        ticket=self.ticket()
        api.update_ticket(self.conn,ticket['id'],'admin',assignee='human-owner')
        self.runner._work(ticket)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0],0)
        self.assertEqual(api.get_ticket(self.conn,ticket['id'])['status'],'open')

    def test_automated_review_closes_only_after_result_is_recorded(self):
        ticket=self.ticket()
        self.runner._work(ticket)
        ticket=api.get_ticket(self.conn,ticket['id'])
        run_id=self.conn.execute('SELECT id FROM agent_runs').fetchone()[0]
        review=self.conn.execute("INSERT INTO ticket_reviews(ticket_id,source_run_id,status,ticket_version,created_at) VALUES(?,?,'queued',?,?)",
                                 (ticket['id'],run_id,ticket['updated_at'],api.now()))
        self.conn.commit()
        self.runner._review_ticket(ticket,review.lastrowid,run_id)
        state=self.conn.execute('SELECT status,reason FROM ticket_reviews').fetchone()
        self.assertEqual(state['status'],'closed',state['reason'])
        self.assertEqual(api.get_ticket(self.conn,ticket['id'])['status'],'done')


if __name__ == '__main__': unittest.main()
