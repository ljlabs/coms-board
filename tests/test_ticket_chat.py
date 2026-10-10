import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tests import *  # noqa: F401,F403
from coms import api, db, progress
from coms.runner import Runner
from coms.server import r_runner_ticket
import re


class TestTicketChat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'board.db'), 'COMS_FAKE_MODE': 'chat-tools'})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.workspace = Path(self.tmp.name) / 'workspace'
        self.workspace.mkdir()
        self.command = shlex.join([sys.executable, str(Path(__file__).parent / 'fixtures' / 'fake_acp.py')])
        for key, value in {'workspace': str(self.workspace), 'active_backend': 'codex', 'codex_command': self.command}.items():
            self.conn.execute('INSERT INTO runner_settings(key,value) VALUES(?,?)', (key, value))
        self.ticket = api.create_ticket(self.conn, 'Weather', 'Fetch the forecast.', 'admin', assignee='orchestrator')
        self.tid = self.ticket['id']
        self.rid = self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,session_id,status,stage,agent_slug) VALUES(?,'codex','old-session','completed','execute','orchestrator')", (self.tid,)).lastrowid
        self.conn.commit()
        self.runner = Runner()

    def tearDown(self):
        self.runner.stop()
        conn = getattr(db._local, 'conn', None)
        if conn:
            conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def message(self, role, kind, body, run=None):
        mid = self.conn.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,?,?,?,?)',
                                (self.tid, run, role, kind, body, api.now())).lastrowid
        self.conn.commit()
        return mid

    def detail(self, logs=False):
        return r_runner_ticket(self.conn, 'GET', re.match(r'(\d+)', str(self.tid)), {'include_logs': '1'} if logs else {}, {}, 'admin')

    def test_ticket_transcript_includes_responses_tools_review_and_errors(self):
        answer = self.message('assistant', 'acp-response', '**Forecast**\n\n- Sunny', self.rid)
        tool = self.message('tool', 'acp-tool_call', json.dumps({'toolCallId': 'weather', 'title': 'Fetch forecast', 'status': 'completed', 'rawOutput': {'temperature': 12.8}}), self.rid)
        review = self.message('assistant', 'review-decision', json.dumps({'decision': 'close', 'reason': 'Report delivered.'}), self.rid)
        user = self.message('user', 'text', 'How did it go?')
        reply = self.message('assistant', 'acp-response', 'It worked.')
        self.message('system', 'acp-log', 'raw transport diagnostic', self.rid)
        error = self.message('system', 'error', 'Backend disconnected')
        detail = self.detail()
        self.assertEqual([m['id'] for m in detail['messages']], [answer, tool, review, user, reply, error])
        self.assertEqual(detail['messages'][0]['kind'], 'text')
        self.assertIn('Execution', detail['messages'][0]['label'])
        self.assertEqual(detail['messages'][1]['kind'], 'tool_call')
        self.assertEqual(detail['messages'][1]['data']['rawOutput'], {'temperature': 12.8})
        self.assertIn('Review', detail['messages'][2]['label'])
        self.assertIn('Report delivered.', detail['messages'][2]['body'])
        self.assertIn('Conversation', detail['messages'][4]['label'])
        self.assertEqual(detail['messages'][5]['kind'], 'error')
        self.assertEqual(detail['logs'], [])
        self.assertEqual(detail['log_count'], 1)
        self.assertEqual(len(self.detail(True)['logs']), 1)
        self.assertFalse(detail['busy'])

    def test_progress_distinguishes_execution_review_and_followup(self):
        for key in ['run:' + str(self.rid), 'review:1', 'ticket-chat:test']:
            progress.Recorder(key, 'orchestrator', self.tid, self.rid, 'codex').finish()
        stages = {m['key'].split(':')[0]: m['stage'] for m in progress.snapshot(self.conn, ticket=self.tid)['items']}
        self.assertEqual(stages, {'run': 'execute', 'review': 'review', 'ticket-chat': 'conversation'})

    def test_followup_after_restart_restores_context_and_persists_reply(self):
        self.message('assistant', 'acp-response', 'Previous weather report', self.rid)
        # There is deliberately no in-memory ACP connection after a restart.
        self.assertTrue(self.runner.chat(self.tid, 'How did it go?')['accepted'])
        deadline = time.monotonic() + 10
        while self.tid in self.runner.active and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertNotIn(self.tid, self.runner.active)
        detail = self.detail()
        self.assertTrue(any(m['role'] == 'assistant' and m.get('run_id') is None for m in detail['messages']), detail)
        self.assertFalse(any(m['kind'] == 'error' for m in detail['messages']), detail)
        self.assertEqual(sum(m['role'] == 'user' for m in detail['messages']), 1)
        self.assertEqual(len(detail['runs']), 1, 'A conversation must not rerun the scheduled ticket')

    def test_followup_rejects_busy_review_capacity_and_missing_execution(self):
        for active, message in [({self.tid: True}, 'still working'),
                                ({'review:' + str(self.tid): True}, 'being reviewed'),
                                ({100: True, 101: True}, 'slots are in use')]:
            self.runner.active = active
            with self.assertRaisesRegex(ValueError, message):
                self.runner.chat(self.tid, 'Hello')
        self.runner.active.clear()
        self.conn.execute('DELETE FROM agent_runs')
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'No completed agent session'):
            self.runner.chat(self.tid, 'Hello')
        self.assertEqual(self.detail()['messages'], [])

    def test_session_load_does_not_duplicate_replayed_messages(self):
        replay = {'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'text': 'Old answer'}}}}
        connection = MagicMock()
        with patch('coms.runner.AcpConnection', return_value=connection) as factory:
            def load(*_args, **_kwargs):
                connection.on_notification(replay)
            connection.session_load.side_effect = load
            restored = self.runner._conn(self.tid, 'codex', str(self.workspace), resume_session='old-session')
        self.assertTrue(restored.ticket_restored)
        connection.session_load.assert_called_once_with('old-session', cwd=str(self.workspace))
        connection.session_new.assert_not_called()
        self.assertEqual(self.detail()['messages'], [])
        # Normal notifications must be restored once session loading finishes.
        self.assertIs(connection.on_notification, factory.call_args.kwargs['on_notification'])

    def test_restore_error_is_visible_and_releases_slot(self):
        self.conn.execute("UPDATE runner_settings SET value='missing-acp-executable' WHERE key='codex_command'")
        self.conn.commit()
        self.runner.chat(self.tid, 'Hello')
        deadline = time.monotonic() + 10
        while self.tid in self.runner.active and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertNotIn(self.tid, self.runner.active)
        self.assertTrue(any(m['kind'] == 'error' for m in self.detail()['messages']))
