import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import *  # noqa: F401,F403
from coms import api, db, mcp
from coms.acp import AcpConnection
from coms.runner import Runner, backend_command, board_servers
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
        alias.symlink_to(APP_ROOT, target_is_directory=True)
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
        self.assertNotIn(ticket['id'], self.runner.active)

    def test_private_response_cannot_complete_ticket(self):
        ticket = self.ticket()
        with patch.dict(os.environ, {'COMS_FAKE_MODE': 'no-delivery'}): self.runner._work(ticket)
        run = dict(self.conn.execute('SELECT * FROM agent_runs').fetchone())
        self.assertEqual(run['status'], 'failed')
        self.assertIn('ticket_result', run['error'])
        self.assertEqual(api.get_ticket(self.conn, ticket['id'])['status'], 'in_progress')
        self.assertFalse(self.runner.conns)

    def test_orchestrator_restores_history_when_backend_cannot_load(self):
        session = self.runner.create_orchestrator_session()
        session.update(acp_session_id='old-session', workspace=str(self.workspace))
        self.runner._orchestrator_turn('Hello', session['id'], session)
        detail = self.runner.orchestrator_detail(session['id'])
        assistant = [m['body'] for m in detail['messages'] if m['role'] == 'assistant']
        self.assertEqual(assistant, ['Private response is not a board deliverable.'])
        self.assertEqual(detail['workspace'], str(self.workspace))
        self.assertFalse(detail['busy'])

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
        self.runner.start()
        self.runner.stop()
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs').fetchone()[0], 'failed')
        self.assertEqual(self.conn.execute('SELECT status FROM ticket_reviews').fetchone()[0], 'failed')

    def test_second_runner_cannot_recover_or_duplicate_live_runs(self):
        self.runner.start()
        other=Runner()
        try:
            with self.assertRaisesRegex(RuntimeError, 'already owns'):
                other.start()
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
