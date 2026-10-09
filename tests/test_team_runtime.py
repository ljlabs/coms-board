import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, mcp, team
from coms.runner import Runner, _team_retry_status
from coms.acp import AcpConnection, AcpError
from coms import progress
from coms.server import r_runner_debug


class TestTeamRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'board.db')})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.workspace = Path(self.tmp.name) / 'work'
        self.workspace.mkdir()
        self.conn.execute("INSERT INTO runner_settings VALUES('workspace',?)", (str(self.workspace),))
        self.conn.commit()
        self.runner = Runner()

    def tearDown(self):
        self.runner.stop()
        conn = getattr(db._local, 'conn', None)
        if conn: conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def test_jobs_reuse_a_registered_orchestrator(self):
        first = api.create_ticket(self.conn, 'First job', '', 'admin', type_='job')
        second = api.create_ticket(self.conn, 'Second job', '', 'admin', type_='job')
        lead = api.resolve_ticket_agent(self.conn, first)
        again = api.resolve_ticket_agent(self.conn, second)
        self.assertEqual(lead['role'], 'orchestrator')
        self.assertEqual(lead['slug'], again['slug'])
        self.assertEqual(len(api.list_agents(self.conn)), 1)
        self.assertEqual(api.get_ticket(self.conn, first['id'])['assignee'], lead['slug'])

    def test_agent_ensure_reuses_an_existing_specialist(self):
        api.upsert_agent(self.conn, 'risk-expert', 'Risk expert', role='expert', description='Risk controls and backtesting')
        result = mcp.call_tool('agent_ensure', {'name': 'Risk expert', 'description': 'Risk controls and backtesting'})
        self.assertIn('risk-expert', result['content'][0]['text'])
        self.assertEqual(len(api.list_agents(self.conn)), 1)

    def test_reserved_orchestrator_identity_has_the_orchestrator_role(self):
        api.upsert_agent(self.conn, 'orchestrator', 'Orchestrator', role='lead')
        self.assertEqual(api.orchestrator_agent(self.conn)['role'], 'orchestrator')
        self.assertEqual(len(api.list_agents(self.conn)), 1)

    def test_delegated_ticket_has_a_registered_identity(self):
        lead = api.create_ticket(self.conn, 'Research job', '', 'admin', type_='job')
        with patch.dict(os.environ, {'COMS_AGENT': 'orchestrator', 'COMS_TICKET_ID': str(lead['id'])}):
            mcp.call_tool('ticket_create', {'title': 'Research market mechanics', 'parent_id': lead['id'], 'ready': True})
        child = api.list_tickets(self.conn, parent_id=lead['id'])[0]
        self.assertTrue(child['assignee'])
        self.assertEqual(api.get_agent(self.conn, child['assignee'])['role'], 'expert')

    def test_team_delegate_marks_new_child_for_collaborative_delivery(self):
        parent = api.create_ticket(self.conn, 'Research job', '', 'admin', type_='job')
        lead = api.resolve_ticket_agent(self.conn, parent)
        with patch.dict(os.environ, {'COMS_AGENT': lead['slug']}):
            result = team.delegate(self.conn, parent['id'], lead['slug'], 'Evidence Specialist',
                                   'Source-backed crypto market research', 'Collect primary evidence',
                                   'Research exchange mechanics')
        self.assertTrue(result['ticket']['collaboration_required'])

    def test_legacy_non_collaborative_child_is_not_retroactively_rejected(self):
        parent = api.create_ticket(self.conn, 'Research job', '', 'admin', type_='job', collaboration_required=True)
        child = api.create_ticket(self.conn, 'Legacy worker', '', 'admin', parent_id=parent['id'],
                                  collaboration_required=False)
        child = api.update_ticket(self.conn, child['id'], 'admin', status='done',
                                  result='Previously reviewed result')
        # Root delivery validates the parent artifact and completed child states;
        # it must not impose today's evidence workflow on a legacy ticket.
        team.validate_delivery(self.conn, child['id'], child['assignee'])

    def test_skills_are_shared_and_updates_require_current_version(self):
        with patch.dict(os.environ, {'COMS_AGENT': 'first'}):
            mcp.call_tool('skill_put', {'slug': 'sourcing', 'title': 'Sourcing', 'body': 'Read primary sources.'})
        with patch.dict(os.environ, {'COMS_AGENT': 'second'}):
            self.assertIn('sourcing', mcp.call_tool('skill_list', {})['content'][0]['text'])
            self.assertIn('Read primary sources.', mcp.call_tool('skill_get', {'slug': 'sourcing'})['content'][0]['text'])
            mcp.call_tool('skill_put', {'slug': 'sourcing', 'title': 'Sourcing', 'body': 'Record dates too.', 'version': 1})
            with self.assertRaises(api.BadRequest):
                mcp.call_tool('skill_put', {'slug': 'sourcing', 'title': 'Sourcing', 'body': 'Stale write', 'version': 1})
        self.assertEqual(api.get_skill(self.conn, 'sourcing')['version'], 2)

    def test_progress_coalesces_responses_and_keeps_a_thinking_heartbeat(self):
        ticket = api.create_ticket(self.conn, 'Work', '', 'admin')
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,started_at) VALUES(?,'opencode','running',?)", (ticket['id'], api.now()))
        rid = self.conn.execute('SELECT id FROM agent_runs').fetchone()[0]
        self.conn.commit()
        recorder = progress.Recorder('run:' + str(rid), 'researcher', ticket['id'], rid, 'opencode')
        for _ in range(2105):
            recorder.notification({'sessionUpdate': 'agent_message_chunk', 'messageId': 'reply', 'content': {'text': 'x'}})
        recorder.notification({'sessionUpdate': 'agent_thought_chunk', 'content': {'text': 'private reasoning'}})
        messages = self.conn.execute('SELECT body FROM agent_messages WHERE run_id=?', (rid,)).fetchall()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['body'], 'x' * 2105)
        state = progress.snapshot(self.conn)['items'][0]
        self.assertEqual(state['phase'], 'thinking')
        self.assertEqual(state['response_chars'], 2105)
        self.assertEqual(state['thinking_updates'], 1)
        self.assertTrue(state['updated_at'])
        self.assertNotIn('private reasoning', str(state))

    def test_debug_returns_the_newest_messages_in_chronological_order(self):
        ticket = api.create_ticket(self.conn, 'Work', '', 'admin')
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status) VALUES(?,'opencode','completed')", (ticket['id'],))
        rid = self.conn.execute('SELECT id FROM agent_runs').fetchone()[0]
        self.conn.executemany("INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,'system','acp-log',?,'now')", [(ticket['id'], rid, str(i)) for i in range(2105)])
        self.conn.commit()
        result = r_runner_debug(self.conn, 'GET', None, {'ticket': str(ticket['id'])}, {}, 'admin')
        bodies = [m['body'] for m in result['runs'][0]['messages']]
        self.assertEqual(bodies[-1], '2104')
        self.assertEqual(bodies[0], '105')
        self.assertEqual(result['runs'][0]['message_count'], 2105)

    def test_parent_waits_without_delivery_and_resumes_for_synthesis(self):
        parent = api.create_ticket(self.conn, 'Research team', 'Coordinate workers.', 'admin', type_='job', ready_for_execution=True, collaboration_required=False)
        fake = Mock(session_id='s')
        created = []
        def delegate(*args, **kwargs):
            created.append(api.create_ticket(self.conn, 'Evidence worker', 'Find sources', 'orchestrator', parent_id=parent['id'], ready_for_execution=True))
        fake.prompt.side_effect = delegate
        with patch.object(self.runner, '_conn', return_value=fake):
            self.runner._work(parent)
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs').fetchone()[0], 'waiting')
        self.assertEqual(api.get_ticket(self.conn, parent['id'])['status'], 'in_progress')
        self.assertEqual(self.runner._resume_waiting(), [])
        api.update_ticket(self.conn, created[0]['id'], 'worker', result='Verified evidence links', status='review')
        self.assertEqual(self.runner._resume_waiting(), [parent['id']])
        def synthesize(*args, **kwargs):
            rid = self.conn.execute('SELECT MAX(id) FROM agent_runs').fetchone()[0]
            with patch.dict(os.environ, {'COMS_TICKET_ID': str(parent['id']), 'COMS_RUN_ID': str(rid)}):
                mcp.call_tool('ticket_result', {'id': parent['id'], 'result': 'Synthesis based on verified workers.'})
        fake.prompt.side_effect = synthesize
        with patch.object(self.runner, '_conn', return_value=fake):
            self.runner._work(api.get_ticket(self.conn, parent['id']))
        prompt = fake.prompt.call_args.args[0]
        self.assertIn('SYNTHESIS', prompt)
        self.assertIn('Verified evidence links', prompt)
        self.assertEqual(api.get_ticket(self.conn, parent['id'])['status'], 'review')

    def test_waiting_parent_resumes_after_children_are_done(self):
        parent = api.create_ticket(self.conn, 'Research team', '', 'admin', type_='job', ready_for_execution=True)
        child = api.create_ticket(self.conn, 'Evidence worker', '', 'orchestrator', parent_id=parent['id'], ready_for_execution=True)
        api.update_ticket(self.conn, child['id'], 'worker', result='Reviewed findings', status='done')
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,started_at,agent_slug,stage) VALUES(?,'opencode','waiting',?,'orchestrator','synthesize')", (parent['id'], api.now()))
        self.conn.commit()
        self.assertEqual(self.runner._resume_waiting(), [parent['id']])
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs WHERE ticket_id=?', (parent['id'],)).fetchone()['status'], 'completed')

    def test_manual_retry_is_durable_and_eligible_for_agent_tickets(self):
        ticket = api.create_ticket(self.conn, 'Retry me', '', 'admin', assignee='risk-expert', ready_for_execution=True)
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,finished_at,error,agent_slug,stage) VALUES(?,'opencode','failed',?,'Missing ticket_result','risk-expert','execute')", (ticket['id'], api.now()))
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,finished_at,error,agent_slug,stage) VALUES(?,'opencode','failed',?,'Teammate response failed','risk-expert','collaborate')", (ticket['id'], api.now()))
        self.conn.commit()
        retried = api.retry_ticket(self.conn, ticket['id'], 'admin')
        self.assertEqual(retried['retry_kind'], 'agent run')
        self.assertEqual(api.get_ticket(self.conn, ticket['id'])['agent_retry_requested'], 1)
        state = api.ticket_retry_state(self.conn, ticket['id'])
        self.assertFalse(state['eligible'])
        self.assertIn('queued', state['reason'])
        self.assertEqual(state['kind'], 'agent run')

    def test_team_delivery_retry_attempts_are_bounded(self):
        parent = api.create_ticket(self.conn, 'Research team', '', 'admin', type_='job', ready_for_execution=True)
        api.upsert_agent(self.conn, 'reviewer', 'Reviewer', role='expert')
        message = self.conn.execute("INSERT INTO team_messages(job_id,sender,recipient,kind,body,created_at) VALUES(?,'orchestrator','reviewer','question','Question','now')", (parent['id'],)).lastrowid
        self.conn.execute("INSERT INTO team_deliveries(message_id,recipient,status,attempt_count,updated_at) VALUES(?, 'reviewer','pending',2,'2000-01-01T00:00:00+00:00')", (message,))
        self.conn.commit()
        with patch.object(self.runner, '_worker') as worker:
            self.runner._schedule_team_replies(self.conn, 1)
        worker.assert_called_once()
        self.assertEqual(_team_retry_status(1), 'pending')
        self.assertEqual(_team_retry_status(3), 'failed')

    def test_parent_cannot_be_reviewed_while_workers_are_unfinished(self):
        parent = api.create_ticket(self.conn, 'Job', '', 'admin', type_='job')
        api.create_ticket(self.conn, 'Pending worker', '', 'orchestrator', parent_id=parent['id'])
        self.assertFalse(api.children_delivered(self.conn, parent['id']))
        parent = api.update_ticket(self.conn, parent['id'], 'admin', status='review', result='Premature result')
        rid = self.conn.execute("INSERT INTO ticket_reviews(ticket_id,status,created_at) VALUES(?,'queued',?)", (parent['id'], api.now())).lastrowid
        self.conn.commit()
        with patch('coms.runner.AcpConnection') as backend:
            self.runner._review_ticket(parent, rid, None)
            backend.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM ticket_reviews').fetchone()[0], 'failed')

    def test_suggested_team_size_can_be_set_and_cleared(self):
        ticket = api.create_ticket(self.conn, 'Team', '', 'admin', type_='job', recommended_workers=3)
        self.assertEqual(ticket['recommended_workers'], 3)
        self.assertIsNone(api.update_ticket(self.conn, ticket['id'], 'admin', recommended_workers=None)['recommended_workers'])
        for invalid in (True, 0, 16, 'many'):
            with self.assertRaises(api.BadRequest):
                api.update_ticket(self.conn, ticket['id'], 'admin', recommended_workers=invalid)

    def test_parent_final_review_waits_for_worker_reviews(self):
        parent = api.create_ticket(self.conn, 'Job', '', 'admin', type_='job')
        child = api.create_ticket(self.conn, 'Worker', '', 'orchestrator', parent_id=parent['id'])
        api.update_ticket(self.conn, child['id'], 'admin', status='review', result='Evidence awaiting review')
        self.assertTrue(api.children_delivered(self.conn, parent['id']))
        self.assertFalse(api.children_resolved(self.conn, parent['id']))
        parent = api.update_ticket(self.conn, parent['id'], 'admin', status='review', result='Synthesis')
        rid = self.conn.execute("INSERT INTO ticket_reviews(ticket_id,status,created_at) VALUES(?,'queued',?)", (parent['id'], api.now())).lastrowid
        self.conn.commit()
        with patch('coms.runner.AcpConnection') as backend:
            self.runner._review_ticket(parent, rid, None)
            backend.assert_not_called()
        api.update_ticket(self.conn, child['id'], 'admin', status='done')
        self.assertTrue(api.children_resolved(self.conn, parent['id']))

    def test_existing_unfinished_team_waits_without_starting_the_model(self):
        parent = api.create_ticket(self.conn, 'Team', '', 'admin', type_='job', ready_for_execution=True)
        api.create_ticket(self.conn, 'Worker', '', 'admin', parent_id=parent['id'], ready_for_execution=True)
        with patch.object(self.runner, '_conn') as backend:
            self.runner._work(parent)
            backend.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs').fetchone()[0], 'waiting')
        self.assertEqual(progress.snapshot(self.conn)['items'][0]['state'], 'waiting')

    def test_tool_updates_merge_and_failure_is_visible(self):
        ticket = api.create_ticket(self.conn, 'Work', '', 'admin')
        recorder = progress.Recorder('test-tools', 'expert', ticket['id'])
        recorder.notification({'sessionUpdate': 'tool_call', 'toolCallId': 'one', 'title': 'Read source', 'status': 'in_progress'})
        recorder.notification({'sessionUpdate': 'tool_call_update', 'toolCallId': 'one', 'status': 'completed', 'rawOutput': {'text': 'Evidence'}})
        recorder.finish('failed', 'Backend disconnected')
        messages = self.conn.execute("SELECT body FROM agent_messages WHERE kind='acp-tool_call'").fetchall()
        self.assertEqual(len(messages), 1)
        self.assertIn('Evidence', messages[0]['body'])
        state = progress.snapshot(self.conn)['items'][0]
        self.assertEqual(state['tool_calls'], 1)
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['summary'], 'Backend disconnected')

    def test_review_hard_deadline_limits_a_continuous_stream(self):
        connection = AcpConnection('unused', str(self.workspace), on_notification=lambda _: None,
                                   on_request=lambda *args: None, on_close=lambda _: None)
        stop = threading.Event()
        def activity():
            while not stop.wait(.01): connection._last_rx = time.monotonic()
        thread = threading.Thread(target=activity)
        thread.start()
        started = time.monotonic()
        try:
            with patch.object(connection, '_write'), self.assertRaises(AcpError):
                connection.request('review', {}, timeout=.1, total_timeout=.2)
        finally:
            stop.set()
            thread.join()
        self.assertLess(time.monotonic() - started, 1)


if __name__ == '__main__':
    unittest.main()
