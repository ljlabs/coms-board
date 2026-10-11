"""Stopping a team is durable queue state, including running/startup races."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, mcp, progress, server, team
from coms.runner import Runner


class TeamStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent)
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'board.db'),
                              'COMS_AGENT': 'orchestrator', 'COMS_TICKET_ID': '', 'COMS_RUN_ID': ''})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.workspace = str(Path(self.tmp.name))
        self.runner = Runner()
        self.lead = api.orchestrator_agent(self.conn)['slug']
        self.job = api.create_ticket(self.conn, 'Audit', '', 'admin', type_='job',
                                    assignee=self.lead, ready_for_execution=True)
        self.child = team.delegate(self.conn, self.job['id'], self.lead,
                                   'Tess', 'Tests', 'Inspect tests', 'Check the test cases')['ticket']
        self.peer = team.delegate(self.conn, self.job['id'], self.lead,
                                  'Dex', 'Review', 'Review tests', 'Check the findings')['ticket']
        self.other = api.create_ticket(self.conn, 'Unrelated work', '', 'admin',
                                      assignee='unrelated', ready_for_execution=True)
        for k, v in {'workspace': self.workspace, 'active_backend': 'codex',
                     'codex_command': 'unused'}.items():
            self.conn.execute('INSERT INTO runner_settings VALUES(?,?)', (k, v))
        self.conn.commit()

    def tearDown(self):
        self.runner.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def run_record(self, ticket, status='waiting', stage='execute'):
        rid = self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,agent_slug,stage,started_at) VALUES(?,'codex',?,?,?,?)",
                                (ticket['id'], status, ticket['assignee'], stage, api.now())).lastrowid
        self.conn.commit()
        rec = progress.Recorder('run:' + str(rid), ticket['assignee'], ticket['id'], rid)
        if status == 'waiting': rec.finish('waiting')
        return rid, rec

    def test_stop_holds_nested_and_source_work_preserving_results_and_unrelated_work(self):
        grandchild = api.create_ticket(self.conn, 'Nested', '', 'admin', parent_id=self.child['id'], ready_for_execution=True)
        spawned = api.create_ticket(self.conn, 'Without parent', '', 'admin', source_ticket_id=grandchild['id'], ready_for_execution=True)
        api.update_ticket(self.conn, self.peer['id'], 'admin', status='done', result='Verified result')
        result = team.stop(self.conn, self.job['id'], 'admin')
        self.assertEqual(set(result['ticket_ids']), {self.job['id'], self.child['id'], self.peer['id'], grandchild['id'], spawned['id']})
        for tid in result['ticket_ids']:
            ticket = api.get_ticket(self.conn, tid)
            self.assertEqual(ticket['ready_for_execution'], 0)
            self.assertEqual(ticket['status'], 'done' if tid == self.peer['id'] else 'blocked')
        self.assertEqual(api.get_ticket(self.conn, self.peer['id'])['result'], 'Verified result')
        self.assertEqual(api.get_ticket(self.conn, self.other['id'])['status'], 'open')
        self.assertEqual(api.get_ticket(self.conn, self.other['id'])['ready_for_execution'], 1)

    def test_stop_cancels_runs_deliveries_and_reviews_and_is_idempotent(self):
        rid, rec = self.run_record(self.child)
        self.run_record(self.peer, 'running', 'collaborate')
        question = team.send(self.conn, self.job['id'], self.child['assignee'], self.peer['assignee'], 'Check this?')
        self.conn.execute("INSERT INTO peer_reviews(job_id,ticket_id,artifact_slug,artifact_version,author,reviewer,message_id,created_at,updated_at) VALUES(?,?,'page','v',?,?,?,? ,?)",
                          (self.job['id'], self.child['id'], self.child['assignee'], self.peer['assignee'], question['id'], api.now(), api.now()))
        self.conn.commit()
        first = team.stop(self.conn, self.job['id'], self.lead)
        second = team.stop(self.conn, self.job['id'], self.lead)
        self.assertEqual(first['ticket_ids'], second['ticket_ids'])
        self.assertEqual({r[0] for r in self.conn.execute('SELECT status FROM agent_runs')}, {'cancelled'})
        self.assertEqual(self.conn.execute('SELECT status FROM team_deliveries WHERE message_id=?', (question['id'],)).fetchone()[0], 'cancelled')
        self.assertEqual(self.conn.execute('SELECT status FROM peer_reviews').fetchone()[0], 'cancelled')
        rec.set('tool', 'Late tool notification')
        rec.finish('waiting', 'Late cleanup')
        self.assertEqual(self.conn.execute('SELECT state FROM agent_runtime WHERE run_id=?', (rid,)).fetchone()[0], 'cancelled')
        self.assertNotIn(self.job['id'], progress.activity(self.conn)['tickets'])
        self.assertEqual(self.runner._resume_waiting(), [])
        self.runner._team_heartbeat()
        with patch('coms.runner.threading.Thread') as worker:
            self.runner._schedule_team_replies(self.conn, 16)
            self.runner._schedule_team_heartbeats(self.conn)
            worker.assert_not_called()

    def test_stop_checks_authority_and_job_type(self):
        for actor in (self.child['assignee'], 'stranger'):
            with self.assertRaises(api.BadRequest): team.stop(self.conn, self.job['id'], actor)
        with self.assertRaises(api.BadRequest): team.stop(self.conn, self.child['id'], 'admin')
        self.assertEqual(api.get_ticket(self.conn, self.job['id'])['ready_for_execution'], 1)

    def test_stop_traversal_handles_cycles(self):
        self.conn.execute('UPDATE tickets SET source_ticket_id=? WHERE id=?', (self.child['id'], self.job['id']))
        self.conn.commit()
        self.assertEqual(len(team.stop(self.conn, self.job['id'], 'admin')['ticket_ids']), 3)

    def test_mcp_stop_then_rejects_new_work_from_held_worker(self):
        result = mcp.call_tool('team_stop', {'job_id': self.job['id']})
        self.assertEqual(json.loads(result['content'][0]['text'])['job_id'], self.job['id'])
        with patch.dict(os.environ, {'COMS_AGENT': self.child['assignee'], 'COMS_TICKET_ID': str(self.child['id'])}):
            with self.assertRaisesRegex(api.BadRequest, 'stopped'):
                mcp.call_tool('ticket_create', {'title': 'Should never spawn', 'ready': True})
            with self.assertRaisesRegex(api.BadRequest, 'stopped'):
                mcp.call_tool('wiki_put', {'slug': 'late', 'title': 'Late', 'body': 'After stop'})
            mcp.call_tool('ticket_get', {'id': self.child['id']})
        with self.assertRaisesRegex(api.BadRequest, 'stopped'):
            team.delegate(self.conn, self.job['id'], self.lead, 'Ivy', 'Docs', 'More work', 'Inspect docs')
        self.assertFalse(self.conn.in_transaction)

    def test_parent_and_source_creation_cannot_bypass_stop(self):
        team.stop(self.conn, self.job['id'], 'admin')
        for field in ('parent_id', 'source_ticket_id'):
            with self.subTest(field=field), self.assertRaisesRegex(api.BadRequest, 'stopped'):
                api.create_ticket(self.conn, 'Late child', '', self.lead, ready_for_execution=True, **{field: self.child['id']})
        self.assertEqual(len(api.list_tickets(self.conn, include_closed=True)), 4)

    def test_runner_cancels_only_held_connections_including_collaboration(self):
        held, other, reply = Mock(), Mock(), Mock()
        self.runner.conns.update({self.child['id']: held, self.other['id']: other})
        self.runner.run_conns[91] = (self.peer['id'], reply)
        team.stop(self.conn, self.job['id'], 'admin')
        self.runner._cancel_stopped_work()
        held.close.assert_called_once()
        reply.close.assert_called_once()
        other.close.assert_not_called()
        self.assertIn(self.other['id'], self.runner.conns)

    def test_connection_stop_during_startup_does_not_send_prompt(self):
        fake = Mock(session_id='test')
        def start():
            self.assertIn(91, self.runner.run_conns)
            team.stop(self.conn, self.job['id'], 'admin')
        fake.start.side_effect = start
        with patch('coms.runner.AcpConnection', return_value=fake), patch.object(self.runner, '_apply_model'):
            with self.assertRaisesRegex(api.BadRequest, 'stopped'):
                self.runner._conn(self.child['id'], 'codex', self.workspace, 91)
        fake.close.assert_called()
        fake.prompt.assert_not_called()

    def test_worker_stop_during_prompt_remains_cancelled_on_success_and_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                for ticket in (self.job, self.child, self.peer):
                    api.update_ticket(self.conn, ticket['id'], 'admin', status='open', ready_for_execution=True)
                fake = Mock(session_id='test')
                def stop(*args, **kwargs):
                    team.stop(self.conn, self.job['id'], 'admin')
                    if failure: raise RuntimeError('ACP EOF while stopping')
                fake.prompt.side_effect = stop
                with patch.object(self.runner, '_conn', return_value=fake), patch('coms.runner.workspace_path', return_value=self.workspace):
                    self.runner._work(api.get_ticket(self.conn, self.child['id']))
                run = self.conn.execute('SELECT status FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(run[0], 'cancelled')
                self.assertEqual(api.get_ticket(self.conn, self.child['id'])['status'], 'blocked')
                fake.close.assert_called()

    def test_stale_reply_reservation_does_not_restart_a_stopped_team(self):
        question = team.send(self.conn, self.job['id'], self.child['assignee'], self.peer['assignee'], 'Check?')
        team.stop(self.conn, self.job['id'], 'admin')
        # Mimic a stale pre-stop queue snapshot.
        self.conn.execute("UPDATE team_deliveries SET status='pending' WHERE message_id=?", (question['id'],))
        self.conn.commit()
        with patch.object(self.runner, '_conn') as connection:
            self.runner._reply_team(question['id'], self.peer['assignee'])
            connection.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0], 0)

    def test_reply_cleanup_cannot_requeue_cancelled_delivery(self):
        question = team.send(self.conn, self.job['id'], self.child['assignee'], self.peer['assignee'], 'Check?')
        fake = Mock(session_id='test')
        def stop(*args, **kwargs):
            team.stop(self.conn, self.job['id'], 'admin')
            raise RuntimeError('ACP EOF')
        fake.prompt.side_effect = stop
        with patch.object(self.runner, '_conn', return_value=fake), patch('coms.runner.workspace_path', return_value=self.workspace):
            self.runner._reply_team(question['id'], self.peer['assignee'])
        self.assertEqual(self.conn.execute('SELECT status FROM team_deliveries WHERE message_id=?', (question['id'],)).fetchone()[0], 'cancelled')
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'cancelled')

    def test_stop_between_cleanup_check_and_wait_transition_is_not_overwritten(self):
        job = api.create_ticket(self.conn, 'Dispatch race', '', 'admin', type_='job', assignee=self.lead, ready_for_execution=True)
        fake = Mock(session_id='test')
        fake.prompt.side_effect = lambda *a, **kw: team.delegate(self.conn, job['id'], self.lead, 'Ivy', 'Docs', 'Read docs', 'Inspect README')
        original = self.runner._finish_stopped_run
        def race(tid, rid, recorder):
            result = original(tid, rid, recorder)
            if not result: team.stop(self.conn, job['id'], 'admin')
            return result
        with patch.object(self.runner, '_conn', return_value=fake), patch.object(self.runner, '_finish_stopped_run', side_effect=race), patch('coms.runner.workspace_path', return_value=self.workspace):
            self.runner._work(api.get_ticket(self.conn, job['id']))
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'cancelled')

    def test_final_review_cleanup_preserves_cancelled_state(self):
        api.update_ticket(self.conn, self.child['id'], 'admin', status='review', result='Ready for review')
        current = api.get_ticket(self.conn, self.child['id'])
        review_id = self.conn.execute("INSERT INTO ticket_reviews(ticket_id,backend,status,ticket_version,created_at) VALUES(?,'codex','queued',?,?)",
                                      (self.child['id'], current['updated_at'], api.now())).lastrowid
        self.conn.commit()
        fake = Mock(session_id='test')
        def stop(*args, **kwargs):
            team.stop(self.conn, self.job['id'], 'admin')
            raise RuntimeError('ACP EOF')
        fake.prompt.side_effect = stop
        with patch('coms.runner.AcpConnection', return_value=fake), patch('coms.runner.workspace_path', return_value=self.workspace), patch.object(self.runner, '_apply_model'), patch('coms.team.validate_delivery'):
            self.runner._review_ticket(current, review_id, 0)
        self.assertEqual(self.conn.execute('SELECT status FROM ticket_reviews WHERE id=?', (review_id,)).fetchone()[0], 'cancelled')
        self.assertEqual(api.get_ticket(self.conn, self.child['id'])['status'], 'blocked')

    def test_stopped_ancestor_prevents_resuming_just_one_child(self):
        team.stop(self.conn, self.job['id'], 'admin')
        api.update_ticket(self.conn, self.child['id'], 'admin', ready_for_execution=True, status='open')
        with patch.object(self.runner, '_conn') as connection:
            self.runner._work(api.get_ticket(self.conn, self.child['id']))
            connection.assert_not_called()

    def test_http_team_stop_dispatches_durable_operation(self):
        route = next((pattern, handler) for pattern, handler in server.ROUTES if re.fullmatch(pattern, f"/api/teams/{self.job['id']}/stop"))
        result = route[1](self.conn, 'POST', re.fullmatch(route[0], f"/api/teams/{self.job['id']}/stop"), {}, {}, 'admin')
        self.assertEqual(result['job_id'], self.job['id'])
        self.assertEqual(api.get_ticket(self.conn, self.child['id'])['status'], 'blocked')

    def test_restart_does_not_revive_cancelled_team(self):
        self.run_record(self.child)
        team.stop(self.conn, self.job['id'], 'admin')
        with patch('coms.runner.workspace_path', return_value=self.workspace), patch('coms.runner.threading.Thread'):
            self.runner.start(warm=False)
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs').fetchone()[0], 'cancelled')
        self.assertEqual(self.runner._resume_waiting(), [])


if __name__ == '__main__':
    unittest.main()
