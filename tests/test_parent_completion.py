"""Regression cases for parent completion and non-job work recovery."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, mcp, progress, skill_store, team
from coms.runner import Runner


class ParentCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'COMS_DB': str(self.root / 'board.db'),
                                          'COMS_AGENT': 'orchestrator'})
        self.env.start()
        self.skills = patch.object(skill_store, 'SKILLS_ROOT', self.root / 'skills')
        self.skills.start()
        self.workspace = patch('coms.runner.workspace_path', return_value=str(self.root))
        self.workspace.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        api.upsert_agent(self.conn, 'orchestrator', 'Lead', role='orchestrator')
        api.upsert_agent(self.conn, 'worker', 'Destination specialist', role='expert')
        self.conn.execute("INSERT INTO runner_settings VALUES('active_backend','codex')")
        self.conn.commit()
        self.runner = Runner()

    def tearDown(self):
        self.runner.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.workspace.stop()
        self.skills.stop()
        self.env.stop()
        self.tmp.cleanup()

    def parent(self, type_='research'):
        return api.create_ticket(self.conn, 'Three destinations', 'Delegate once, then synthesize.',
                                  'admin', type_=type_, assignee='orchestrator',
                                  ready_for_execution=True, collaboration_required=False)

    def child(self, parent, *, delivered=False):
        child = api.create_ticket(self.conn, 'Nice research', 'Known for, food, attractions.',
                                 'admin', parent_id=parent['id'], assignee='worker', ready_for_execution=True)
        if delivered:
            child = api.update_ticket(self.conn, child['id'], 'admin', status='done', result='Verified Nice findings')
        return child

    def waiting(self, parent, *, delivered=False):
        rid = self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,agent_slug,stage,started_at) VALUES(?,'codex','waiting','orchestrator','execute',?)", (parent['id'], api.now())).lastrowid
        if delivered:
            api.update_ticket(self.conn, parent['id'], 'admin', result='Recorded synthesis')
            self.conn.execute("INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,'system','board-delivery',?,?)", (parent['id'], rid, json.dumps({'tool': 'ticket_result'}), api.now()))
        self.conn.commit()
        return rid

    def test_non_job_waiting_parent_does_not_resume_before_child_delivery(self):
        parent = self.parent()
        self.child(parent)
        rid = self.waiting(parent)
        self.assertEqual(self.runner._resume_waiting(), [])
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs WHERE id=?', (rid,)).fetchone()[0], 'waiting')

    def test_non_job_parent_waits_without_starting_another_model_turn(self):
        parent = self.parent()
        self.child(parent)
        with patch.object(self.runner, '_conn') as connect:
            self.runner._work(parent)
        connect.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'waiting')

    def test_non_job_parent_resumes_in_synthesis_and_completes(self):
        parent = self.parent()
        self.child(parent, delivered=True)
        fake = Mock(session_id='synthesis')
        def deliver(*args, **kwargs):
            rid = self.conn.execute('SELECT max(id) FROM agent_runs').fetchone()[0]
            with patch.dict(os.environ, {'COMS_TICKET_ID': str(parent['id']), 'COMS_RUN_ID': str(rid)}):
                mcp.call_tool('ticket_result', {'id': parent['id'], 'result': 'Verified synthesis'})
        fake.prompt.side_effect = deliver
        with patch.object(self.runner, '_conn', return_value=fake):
            self.runner._work(parent)
        self.assertIn('SYNTHESIS', fake.prompt.call_args.args[0])
        run = self.conn.execute('SELECT * FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual((run['stage'], run['status']), ('synthesize', 'completed'))
        self.assertEqual(api.get_ticket(self.conn, parent['id'])['status'], 'review')

    def test_result_delivery_wins_over_initial_dispatch_wait_branch(self):
        parent = self.parent('job')
        fake = Mock(session_id='dispatch')
        def finish(*args, **kwargs):
            self.child(parent, delivered=True)
            rid = self.conn.execute('SELECT max(id) FROM agent_runs').fetchone()[0]
            with patch.dict(os.environ, {'COMS_TICKET_ID': str(parent['id']), 'COMS_RUN_ID': str(rid)}):
                mcp.call_tool('ticket_result', {'id': parent['id'], 'result': 'Already synthesized'})
        fake.prompt.side_effect = finish
        with patch.object(self.runner, '_conn', return_value=fake):
            self.runner._work(parent)
        self.assertEqual(api.get_ticket(self.conn, parent['id'])['status'], 'review')
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'completed')
        self.assertEqual(self.runner._resume_waiting(), [])

    def test_backend_error_after_delivery_does_not_retry_execution(self):
        parent = self.parent()
        fake = Mock(session_id='delivery')
        def finish(*args, **kwargs):
            rid = self.conn.execute('SELECT max(id) FROM agent_runs').fetchone()[0]
            with patch.dict(os.environ, {'COMS_TICKET_ID': str(parent['id']), 'COMS_RUN_ID': str(rid)}):
                mcp.call_tool('ticket_result', {'id': parent['id'], 'result': 'Already delivered'})
            raise RuntimeError('Backend disconnected after cancellation')
        fake.prompt.side_effect = finish
        with patch.object(self.runner, '_conn', return_value=fake):
            self.runner._work(parent)
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'completed')
        self.assertEqual(api.get_ticket(self.conn, parent['id'])['status'], 'review')

    def test_waiting_run_with_published_result_routes_to_review_without_replay(self):
        parent = self.parent()
        self.child(parent, delivered=True)
        self.waiting(parent, delivered=True)
        self.runner._resume_waiting()
        self.assertEqual(api.get_ticket(self.conn, parent['id'])['status'], 'review')
        with patch.object(self.runner, '_conn') as connect:
            self.runner._work(api.get_ticket(self.conn, parent['id']))
        connect.assert_not_called()

    def test_result_is_terminal_for_execution_and_requires_child_results(self):
        parent = self.parent()
        child = self.child(parent)
        with patch.dict(os.environ, {'COMS_TICKET_ID': str(parent['id'])}):
            with self.assertRaisesRegex(api.BadRequest, 'child'):
                mcp.call_tool('ticket_result', {'id': parent['id'], 'result': 'Premature'})
            api.update_ticket(self.conn, child['id'], 'admin', result='Actual evidence', status='done')
            mcp.call_tool('ticket_result', {'id': parent['id'], 'result': 'Final result'})
            self.assertEqual(api.get_ticket(self.conn, parent['id'])['status'], 'review')
            with self.assertRaisesRegex(api.BadRequest, 'deliver|review'):
                api.create_ticket(self.conn, 'New copy', '', 'orchestrator', source_ticket_id=parent['id'])

    def test_multiworker_mcp_request_becomes_a_collaborative_job(self):
        result = mcp.call_tool('ticket_create', {'title': 'Trip', 'type': 'research',
                                               'ready': True, 'recommended_workers': 3})
        ticket = json.loads(result['content'][0]['text'])
        self.assertEqual(ticket['type'], 'job')
        self.assertEqual(ticket['collaboration_required'], 1)

    def test_common_api_rejects_team_size_on_non_job_types(self):
        with self.assertRaisesRegex(api.BadRequest, 'job'):
            api.create_ticket(self.conn, 'Trip', '', 'admin', type_='research', recommended_workers=3)

    def test_chat_cannot_staff_a_non_job_parent(self):
        parent = self.parent()
        with self.assertRaisesRegex(ValueError, 'staff'):
            mcp.call_tool('ticket_create', {'title': 'Duplicate Nice', 'parent_id': parent['id'], 'ready': True})

    def test_assigned_lead_must_use_structured_delegation(self):
        parent = self.parent('job')
        with patch.dict(os.environ, {'COMS_TICKET_ID': str(parent['id'])}):
            with self.assertRaisesRegex(ValueError, 'team_delegate'):
                mcp.call_tool('ticket_create', {'title': 'Nice again', 'parent_id': parent['id'], 'ready': True})

    def test_lead_cannot_bypass_delegation_by_omitting_parent(self):
        parent = self.parent('job')
        with patch.dict(os.environ, {'COMS_TICKET_ID': str(parent['id'])}):
            with self.assertRaisesRegex(ValueError, 'team_delegate'):
                mcp.call_tool('ticket_create', {'title': 'Orphan copy', 'ready': True})

    def test_successful_delivery_ends_the_model_turn(self):
        parent = self.parent()
        rid = self.waiting(parent)
        recorder = progress.Recorder('run:' + str(rid), 'orchestrator', parent['id'], rid, 'codex')
        self.runner.recorders[rid] = recorder
        fake = Mock(session_id='delivery')
        with patch('coms.runner.backend_command', return_value=('fake', [])), \
                patch('coms.runner.AcpConnection', return_value=fake) as connection, \
                patch.object(self.runner, '_apply_model'):
            self.runner._conn(parent['id'], 'codex', str(self.root), rid)
            notify = connection.call_args.kwargs['on_notification']
            notify({'params': {'update': {'sessionUpdate': 'tool_call', 'toolCallId': 'result',
                                         'title': 'swarm_ticket_result', 'status': 'in_progress'}}})
            notify({'params': {'update': {'sessionUpdate': 'tool_call_update', 'toolCallId': 'result',
                                         'status': 'completed'}}})
            fake.cancel.assert_not_called()  # Tool status alone is not delivery proof.
            with patch.dict(os.environ, {'COMS_TICKET_ID': str(parent['id']), 'COMS_RUN_ID': str(rid)}):
                mcp.call_tool('ticket_result', {'id': parent['id'], 'result': 'Delivered result'})
            notify({'params': {'update': {'sessionUpdate': 'tool_call_update', 'toolCallId': 'result',
                                         'status': 'completed'}}})
            fake.cancel.assert_called_once()

    def test_held_parent_does_not_consume_child_final_review_attempts(self):
        parent = self.parent()
        child = self.child(parent)
        api.update_ticket(self.conn, child['id'], 'admin', status='review', result='Original findings')
        api.update_ticket(self.conn, parent['id'], 'admin', status='blocked', ready_for_execution=False)
        with patch.object(self.runner.stopping, 'wait', side_effect=[False, True]), \
                patch.object(self.runner, '_restart_orchestrator_if_needed'), \
                patch('coms.runner.threading.Thread') as thread:
            self.runner._loop()
        thread.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM ticket_reviews').fetchone()[0], 0)

    def test_held_reviews_do_not_hide_eligible_work_at_capacity_one(self):
        parent = self.parent()
        child = self.child(parent)
        api.update_ticket(self.conn, child['id'], 'admin', status='review', result='Held findings')
        api.update_ticket(self.conn, parent['id'], 'admin', status='blocked', ready_for_execution=False)
        eligible = self.parent()
        api.update_ticket(self.conn, eligible['id'], 'admin', status='review', result='Ready findings')
        self.conn.execute("INSERT OR REPLACE INTO runner_settings VALUES('parallel_agents','1')")
        self.conn.commit()
        with patch.object(self.runner.stopping, 'wait', side_effect=[False, True]), \
                patch.object(self.runner, '_restart_orchestrator_if_needed'), \
                patch('coms.runner.threading.Thread') as thread:
            self.runner._loop()
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args.kwargs['args'][0], 'review:' + str(eligible['id']))
        self.assertEqual(self.conn.execute('SELECT ticket_id FROM ticket_reviews').fetchone()[0], eligible['id'])

    def test_explicit_final_review_retry_is_actually_dispatched(self):
        ticket = self.parent()
        rid = self.waiting(ticket)
        self.conn.execute("UPDATE agent_runs SET status='completed' WHERE id=?", (rid,))
        self.conn.commit()
        ticket = api.update_ticket(self.conn, ticket['id'], 'admin', status='review', result='Delivered findings')
        review_id = self.conn.execute("INSERT INTO ticket_reviews(ticket_id,source_run_id,backend,status,ticket_version,attempt_count,created_at,finished_at) VALUES(?,?,'codex','failed',?,3,?,?)",
                                      (ticket['id'], rid, ticket['updated_at'], api.now(), api.now())).lastrowid
        self.conn.commit()
        api.retry_ticket(self.conn, ticket['id'], 'admin')
        with patch.object(self.runner.stopping, 'wait', side_effect=[False, True]), \
                patch.object(self.runner, '_restart_orchestrator_if_needed'), \
                patch('coms.runner.threading.Thread') as thread:
            self.runner._loop()
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args.kwargs['args'][0], 'review:' + str(ticket['id']))
        self.assertEqual(self.conn.execute('SELECT attempt_count FROM ticket_reviews WHERE id=?', (review_id,)).fetchone()[0], 1)

    def test_completed_scope_needs_an_explicit_bounded_followup(self):
        parent = self.parent('job')
        assigned = team.delegate(self.conn, parent['id'], 'orchestrator', 'Destination specialist',
                                 'Nice evidence', 'Nice research', 'Research Nice')
        api.update_ticket(self.conn, assigned['ticket']['id'], 'admin', status='done', result='Nice findings')
        with self.assertRaisesRegex(api.BadRequest, 'criterion|completed'):
            team.delegate(self.conn, parent['id'], 'orchestrator', 'Destination specialist',
                          'Nice evidence', 'Research Nice', 'Same research')
        followup = team.delegate(self.conn, parent['id'], 'orchestrator', 'Destination specialist',
                                 'Nice evidence', 'Confirm accessibility', 'Verify access',
                                 followup_reason='The required wheelchair access criterion is unverified')
        api.update_ticket(self.conn, followup['ticket']['id'], 'admin', status='done', result='Access checked')
        with self.assertRaisesRegex(api.BadRequest, 'budget'):
            team.delegate(self.conn, parent['id'], 'orchestrator', 'Destination specialist',
                          'Nice evidence', 'More Nice', 'More research', followup_reason='Another detail')


if __name__ == '__main__':
    unittest.main()
