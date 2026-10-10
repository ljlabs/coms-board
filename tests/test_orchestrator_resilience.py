"""Focused regressions for runner and ACP recovery paths."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db
from coms.acp import AcpConnection
from coms.runner import Runner


class OrchestratorResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent)
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'board.db')})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.workspace = Path(self.tmp.name) / 'workspace'
        self.workspace.mkdir()
        self.workspace_policy = patch('coms.runner.workspace_path', return_value=str(self.workspace))
        self.workspace_policy.start()
        for key, value in {
            'workspace': str(self.workspace),
            'active_backend': 'codex',
            'codex_command': 'unused-acp-command',
        }.items():
            self.conn.execute('INSERT INTO runner_settings(key,value) VALUES(?,?)', (key, value))
        self.conn.commit()
        self.runner = Runner()

    def tearDown(self):
        self.runner.stop()
        conn = getattr(db._local, 'conn', None)
        if conn:
            conn.close()
        db._local.__dict__.clear()
        self.workspace_policy.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _job_with_waiting_lead(self, title, *, updated_at='2000-01-01T00:00:00+00:00',
                               assignee=None, ready=True):
        job = api.create_ticket(self.conn, title, '', 'admin', type_='job',
                                 ready_for_execution=ready)
        lead = api.resolve_ticket_agent(self.conn, job)
        if assignee:
            lead = api.upsert_agent(self.conn, assignee, assignee, role='human')
            api.update_ticket(self.conn, job['id'], 'admin', assignee=assignee)
        api.update_ticket(self.conn, job['id'], 'admin', status='in_progress')
        child = api.create_ticket(self.conn, title + ' child', '', 'admin',
                                  parent_id=job['id'], ready_for_execution=True)
        api.update_ticket(self.conn, child['id'], 'admin', status='in_progress')
        cursor = self.conn.execute(
            "INSERT INTO agent_runs(ticket_id,backend,status,started_at,finished_at,agent_slug,stage) "
            "VALUES(?,'codex','waiting',?,?,?,'heartbeat')",
            (job['id'], updated_at, updated_at, lead['slug']))
        run_id = cursor.lastrowid
        self.conn.execute(
            "INSERT INTO agent_runtime(key,agent_slug,ticket_id,run_id,backend,state,phase,summary,started_at,updated_at) "
            "VALUES(?,?,?,?,'codex','waiting','heartbeat','waiting',?,?)",
            ('run:' + str(run_id), lead['slug'], job['id'], run_id, updated_at, updated_at))
        self.conn.commit()
        return job, child, run_id

    def test_acp_close_terminates_live_process_even_if_reader_already_marked_closed(self):
        class Process:
            def __init__(self):
                self.stdin = Mock()
                self.stdout = Mock()
                self.stderr = Mock()
                self.terminated = False
                self.killed = False

            def poll(self):
                return 0 if self.terminated or self.killed else None

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return 0

        connection = AcpConnection('unused', str(self.workspace), on_notification=lambda _m: None,
                                    on_request=lambda _m, _r: None, on_close=lambda _e: None)
        process = Process()
        connection.proc = process
        connection._closed.set()  # Simulate stdout EOF arriving before process exit.
        connection.close()
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        process.stdin.close.assert_called()

    def test_team_heartbeat_wakes_only_stale_agent_leads_once(self):
        stale, _child, stale_run = self._job_with_waiting_lead('Stale lead')
        recent, _child, recent_run = self._job_with_waiting_lead(
            'Recent lead', updated_at=api.now())
        paused, _child, paused_run = self._job_with_waiting_lead(
            'Paused lead', ready=False)
        human, _child, human_run = self._job_with_waiting_lead(
            'Human lead', assignee='human-reviewer')

        self.runner._team_heartbeat()
        self.runner._team_heartbeat()

        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs WHERE id=?',
                                           (stale_run,)).fetchone()['status'], 'completed')
        self.assertEqual(self.conn.execute('SELECT phase FROM agent_runtime WHERE run_id=?',
                                           (stale_run,)).fetchone()['phase'], 'heartbeat')
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs WHERE id=?',
                                           (recent_run,)).fetchone()['status'], 'waiting')
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs WHERE id=?',
                                           (paused_run,)).fetchone()['status'], 'waiting')
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs WHERE id=?',
                                           (human_run,)).fetchone()['status'], 'waiting')
        wakes = self.conn.execute(
            "SELECT count(*) FROM activity WHERE kind='team.heartbeat' AND ref=?",
            ('ticket:' + str(stale['id']),)).fetchone()[0]
        self.assertEqual(wakes, 1)

    def test_stale_failed_lead_gets_monitor_after_normal_retries_are_exhausted(self):
        stale_at = '2000-01-01T00:00:00+00:00'

        def failed_job(title, updated_at):
            job, _child, run_id = self._job_with_waiting_lead(title, updated_at=updated_at)
            self.conn.execute('UPDATE tickets SET agent_retry_count=2 WHERE id=?', (job['id'],))
            self.conn.execute(
                "UPDATE agent_runs SET status='failed',stage='execute',error='lead ACP failed',finished_at=? WHERE id=?",
                (updated_at, run_id))
            self.conn.execute(
                "UPDATE agent_runtime SET state='failed',phase='execute',summary='lead ACP failed',updated_at=? WHERE run_id=?",
                (updated_at, run_id))
            self.conn.commit()
            return job, run_id

        stale, stale_run = failed_job('Exhausted stale lead', stale_at)
        recent, recent_run = failed_job('Exhausted recent lead', api.now())
        stale_row = api.get_ticket(self.conn, stale['id'])
        recent_row = api.get_ticket(self.conn, recent['id'])
        self.assertFalse(Runner._automatic_run_retry_ready(self.conn, stale_row))
        self.assertFalse(Runner._automatic_run_retry_ready(self.conn, recent_row))

        self.runner._team_heartbeat()
        self.runner._team_heartbeat()

        stale_run_row = self.conn.execute('SELECT status,error FROM agent_runs WHERE id=?',
                                          (stale_run,)).fetchone()
        self.assertEqual(stale_run_row['status'], 'completed')
        self.assertEqual(stale_run_row['error'], 'lead ACP failed')
        self.assertEqual(self.conn.execute('SELECT phase FROM agent_runtime WHERE run_id=?',
                                           (stale_run,)).fetchone()['phase'], 'heartbeat')
        self.assertTrue(Runner._automatic_run_retry_ready(self.conn, stale_row))
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs WHERE id=?',
                                           (recent_run,)).fetchone()['status'], 'failed')
        self.assertFalse(Runner._automatic_run_retry_ready(self.conn, recent_row))
        wakes = self.conn.execute(
            "SELECT count(*) FROM activity WHERE kind='team.heartbeat' AND ref=?",
            ('ticket:' + str(stale['id']),)).fetchone()[0]
        self.assertEqual(wakes, 1)
        recent_wakes = self.conn.execute(
            "SELECT count(*) FROM activity WHERE kind='team.heartbeat' AND ref=?",
            ('ticket:' + str(recent['id']),)).fetchone()[0]
        self.assertEqual(recent_wakes, 0)

    def test_due_team_monitor_schedules_outside_full_normal_worker_capacity(self):
        job, _child, _run_id = self._job_with_waiting_lead('Monitor with full worker pool')
        self.runner._team_heartbeat()
        self.runner.active.update({'worker-1': True, 'worker-2': True})
        started = []

        class DeferredThread:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            def start(self):
                started.append(self)

        with patch('coms.runner.threading.Thread', side_effect=DeferredThread):
            self.runner._schedule_team_heartbeats(self.conn)
            self.runner._schedule_team_heartbeats(self.conn)

        self.assertEqual(len(started), 1)
        self.assertIn(job['id'], self.runner.active)
        self.assertIn(job['id'], self.runner.heartbeat_tickets)
        self.assertEqual(self.runner._active_slots(), 2)

    def test_failed_lead_with_unresolved_children_is_retained_for_heartbeat(self):
        job = api.create_ticket(self.conn, 'Supervise workers', '', 'admin', type_='job',
                                 ready_for_execution=True, collaboration_required=False)
        lead = api.resolve_ticket_agent(self.conn, job)

        class DispatchThenFail:
            session_id = 'lead-session'

            def prompt(inner_self, _prompt, **_kwargs):
                api.create_ticket(self.conn, 'Partially dispatched child', '', lead['slug'],
                                  parent_id=job['id'], ready_for_execution=True)
                raise RuntimeError('ACP disconnected after delegation')

            close = Mock()

        fake = DispatchThenFail()

        with patch.object(self.runner, '_conn', return_value=fake):
            self.runner._work(job)

        run = self.conn.execute('SELECT * FROM agent_runs WHERE ticket_id=?', (job['id'],)).fetchone()
        self.assertEqual(run['status'], 'waiting')
        self.assertEqual(run['agent_slug'], lead['slug'])
        self.assertIn('ACP disconnected after delegation', run['error'])
        self.assertEqual(self.conn.execute('SELECT state FROM agent_runtime WHERE run_id=?',
                                           (run['id'],)).fetchone()['state'], 'waiting')

    def test_monitor_budget_timer_starts_before_acp_init_and_is_cancelled_on_failure(self):
        ticket = api.create_ticket(self.conn, 'Bounded monitor startup', '', 'admin',
                                   ready_for_execution=True)
        timer = Mock()
        fake = Mock()

        def fail_after_timer_started():
            timer.start.assert_called_once()
            raise RuntimeError('ACP initialization failed')

        fake.start.side_effect = fail_after_timer_started
        with patch('coms.runner.threading.Timer', return_value=timer) as timer_factory, \
             patch('coms.runner.AcpConnection', return_value=fake):
            with self.assertRaisesRegex(RuntimeError, 'ACP initialization failed'):
                self.runner._conn(ticket['id'], 'codex', str(self.workspace), monitor_only=True)

        timer_factory.assert_called_once_with(180, fake.close)
        self.assertTrue(timer.daemon)
        timer.start.assert_called_once()
        timer.cancel.assert_called_once()
        fake.close.assert_called_once()

    def _run_heartbeat_prompt(self, *, emit_team_read):
        job, _child, _prior_run = self._job_with_waiting_lead(
            'Heartbeat inspection', updated_at='2000-01-01T00:00:00+00:00')
        lead_slug = self.conn.execute('SELECT agent_slug FROM agent_runs WHERE id=?',
                                      (_prior_run,)).fetchone()['agent_slug']
        lead = api.get_agent(self.conn, lead_slug)
        self.runner._team_heartbeat()

        class FakeConnection:
            session_id = 'heartbeat-session'
            alive = True

            def __init__(self):
                self.prompt_args = None
                self.close = Mock()

            def prompt(inner_self, prompt, **kwargs):
                inner_self.prompt_args = (prompt, kwargs)
                if emit_team_read:
                    recorder = self.runner.recorders[max(self.runner.recorders)]
                    recorder.notification({
                        'sessionUpdate': 'tool_call', 'toolCallId': 'read',
                        'title': 'swarm-agent_team_read', 'kind': 'other',
                        'rawInput': {'job_id': job['id']}, 'status': 'in_progress',
                    })
                    recorder.notification({
                        'sessionUpdate': 'tool_call_update', 'toolCallId': 'read',
                        'title': 'swarm-agent_team_read', 'status': 'completed',
                        'rawInput': {'job_id': job['id']},
                    })
                return {'stopReason': 'end_turn'}

        fake = FakeConnection()
        with patch.object(self.runner, '_conn', return_value=fake) as connection:
            self.runner._work(job)
        run = self.conn.execute(
            'SELECT * FROM agent_runs WHERE ticket_id=? ORDER BY id DESC LIMIT 1',
            (job['id'],)).fetchone()
        self.assertEqual(run['stage'], 'heartbeat')
        self.assertEqual(run['agent_slug'], lead['slug'])
        self.assertEqual(connection.call_args.kwargs.get('monitor_only'), True)
        self.assertEqual(fake.prompt_args[1].get('timeout'), 180)
        self.assertEqual(fake.prompt_args[1].get('total_timeout'), 180)
        return run

    def test_team_heartbeat_requires_completed_scoped_team_read(self):
        missed = self._run_heartbeat_prompt(emit_team_read=False)
        self.assertEqual(missed['status'], 'waiting')
        self.assertIn('successful team_read', missed['error'])

        read = self._run_heartbeat_prompt(emit_team_read=True)
        self.assertEqual(read['status'], 'waiting')
        self.assertEqual(read['error'], '')

    def test_idle_orchestrator_restart_replaces_only_a_dead_connection(self):
        dead = Mock(alive=False)
        self.runner.orchestrator_conn = dead
        self.runner.orchestrator_conn_key = ('old',)
        self.runner.orchestrator_health_checked = 0
        warmed = threading.Event()
        with patch('coms.runner.time.monotonic', return_value=31), \
             patch.object(self.runner, '_warm_orchestrator', side_effect=warmed.set) as warm:
            self.runner._restart_orchestrator_if_needed()
            self.assertTrue(warmed.wait(2), 'idle restart did not schedule orchestrator warmup')
        dead.close.assert_called_once()
        self.assertIsNone(self.runner.orchestrator_conn)
        warm.assert_called_once()

        live = Mock(alive=True)
        self.runner.orchestrator_conn = live
        self.runner.orchestrator_health_checked = 0
        with patch('coms.runner.time.monotonic', return_value=62), \
             patch.object(self.runner, '_warm_orchestrator') as warm:
            self.runner._restart_orchestrator_if_needed()
        self.assertIs(self.runner.orchestrator_conn, live)
        live.close.assert_not_called()
        warm.assert_not_called()

        self.runner.orchestrator_busy = True
        self.runner.orchestrator_conn = dead
        self.runner.orchestrator_health_checked = 0
        with patch('coms.runner.time.monotonic', return_value=93), \
             patch.object(self.runner, '_warm_orchestrator') as warm:
            self.runner._restart_orchestrator_if_needed()
        self.assertIs(self.runner.orchestrator_conn, dead)
        dead.close.assert_called_once()
        warm.assert_not_called()

    def test_model_discovery_keeps_live_project_connection_while_chat_is_busy(self):
        project_connection = Mock(alive=True)
        project_connection.config_options = [{'id': 'model', 'options': []}]
        project_connection.models.return_value = ['project-model']
        project_connection.current_model.return_value = 'project-model'
        self.runner.orchestrator_conn = project_connection
        self.runner.orchestrator_conn_key = ('codex', ('project-command',), '/project/workspace', 'old-model')
        self.runner.orchestrator_busy = True
        self.runner.orchestrator_session = 'active-project-chat'

        with patch('coms.runner.AcpConnection') as acp_connection:
            result = self.runner.discover_models('codex')

        self.assertEqual(result['models'], ['project-model'])
        self.assertEqual(result['current'], 'project-model')
        self.assertIs(self.runner.orchestrator_conn, project_connection)
        project_connection.close.assert_not_called()
        acp_connection.assert_not_called()

    def test_stop_is_scoped_to_the_active_chat_and_records_cancellation(self):
        first = self.runner.create_orchestrator_session('First chat')
        second = self.runner.create_orchestrator_session('Second chat')
        entered = threading.Event()
        canceled = threading.Event()

        class BlockingConnection:
            alive = True
            session_id = 'acp-session'

            def supports(self, _capability):
                return False

            def session_new(self, **_kwargs):
                return {'sessionId': self.session_id}

            def prompt(self, _text, **_kwargs):
                entered.set()
                if not canceled.wait(3):
                    raise TimeoutError('cancel was not delivered')
                return {'stopReason': 'cancelled'}

            def cancel(self):
                canceled.set()

            def close(self):
                canceled.set()
                self.alive = False

        fake = BlockingConnection()
        worker_connection = Mock()
        self.runner.conns[9876] = worker_connection
        with patch.object(self.runner, '_orchestrator_connection', return_value=fake), \
             patch.object(self.runner, '_apply_model'):
            accepted = self.runner.orchestrate('Please respond', first['id'])
            try:
                self.assertTrue(entered.wait(2), 'orchestrator turn did not start')
                with self.assertRaises(ValueError):
                    self.runner.stop_orchestrator(second['id'])
                self.assertFalse(canceled.is_set())
                stopped = self.runner.stop_orchestrator(first['id'])
                self.assertEqual(stopped, {'stopping': True, 'turn_id': accepted['turn_id']})
            finally:
                # Keep a failed stop assertion from leaking a blocked worker/SQLite handle.
                canceled.set()
                deadline = time.monotonic() + 3
                while self.runner.orchestrator_busy and time.monotonic() < deadline:
                    time.sleep(.01)

        self.assertFalse(self.runner.orchestrator_busy)
        turn = self.conn.execute('SELECT status FROM orchestrator_turns WHERE id=?',
                                 (accepted['turn_id'],)).fetchone()
        self.assertEqual(turn['status'], 'cancelled')
        detail = self.runner.orchestrator_detail(first['id'])
        self.assertFalse(detail['busy'])
        self.assertEqual(detail['turns'][-1]['status'], 'cancelled')
        worker_connection.cancel.assert_not_called()
        worker_connection.close.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM orchestrator_messages '
                                           "WHERE turn_id=? AND kind='error'",
                                           (accepted['turn_id'],)).fetchone()[0], 1)

    def test_stop_force_closes_backend_that_ignores_cancel(self):
        session = self.runner.create_orchestrator_session('Unresponsive chat')
        entered = threading.Event()
        closed = threading.Event()

        class UnresponsiveConnection:
            alive = True
            session_id = 'unresponsive-session'

            def supports(self, _capability):
                return False

            def session_new(self, **_kwargs):
                return {'sessionId': self.session_id}

            def prompt(self, _text, **_kwargs):
                entered.set()
                if not closed.wait(3):
                    raise TimeoutError('forced close did not reach backend')
                raise OSError('ACP connection closed')

            def cancel(self):
                # Simulate an ACP backend that does not honor session/cancel.
                return None

            def close(self):
                self.alive = False
                closed.set()

        fake = UnresponsiveConnection()
        with patch.object(self.runner, '_orchestrator_connection', return_value=fake), \
             patch.object(self.runner, '_apply_model'), \
             patch('coms.runner.ORCHESTRATOR_STOP_GRACE_SECONDS', .02):
            accepted = self.runner.orchestrate('Stop even if backend hangs', session['id'])
            self.assertTrue(entered.wait(2), 'orchestrator turn did not start')
            stopped = self.runner.stop_orchestrator(session['id'])
            self.assertEqual(stopped, {'stopping': True, 'turn_id': accepted['turn_id']})
            self.assertTrue(closed.wait(2), 'stop did not force-close the unresponsive ACP')
            deadline = time.monotonic() + 3
            while self.runner.orchestrator_busy and time.monotonic() < deadline:
                time.sleep(.01)

        self.assertFalse(self.runner.orchestrator_busy)
        turn = self.conn.execute('SELECT status FROM orchestrator_turns WHERE id=?',
                                 (accepted['turn_id'],)).fetchone()
        self.assertEqual(turn['status'], 'cancelled')
        self.assertFalse(self.runner.orchestrator_detail(session['id'])['busy'])

    def test_stop_during_connection_initialization_closes_bound_connection(self):
        session = self.runner.create_orchestrator_session('Starting chat')
        connection_bound = threading.Event()
        continue_startup = threading.Event()
        closed = threading.Event()

        class StartingConnection:
            alive = True
            session_id = 'starting-session'

            def cancel(self):
                return None

            def close(self):
                self.alive = False
                closed.set()

        fake = StartingConnection()

        def initialize(_backend, _cwd, _settings, *, on_connect=None, **_kwargs):
            on_connect(fake)
            connection_bound.set()
            if not continue_startup.wait(3):
                raise TimeoutError('test did not release mocked ACP startup')
            return fake

        with patch.object(self.runner, '_orchestrator_connection', side_effect=initialize), \
             patch('coms.runner.ORCHESTRATOR_STOP_GRACE_SECONDS', .02):
            accepted = self.runner.orchestrate('Stop while ACP is starting', session['id'])
            self.assertTrue(connection_bound.wait(2), 'startup did not bind the ACP connection')
            stopped = self.runner.stop_orchestrator(session['id'])
            self.assertEqual(stopped, {'stopping': True, 'turn_id': accepted['turn_id']})
            self.assertTrue(closed.wait(2), 'bound startup connection was not force-closed')
            continue_startup.set()
            deadline = time.monotonic() + 3
            while self.runner.orchestrator_busy and time.monotonic() < deadline:
                time.sleep(.01)

        self.assertFalse(self.runner.orchestrator_busy)
        turn = self.conn.execute('SELECT status FROM orchestrator_turns WHERE id=?',
                                 (accepted['turn_id'],)).fetchone()
        self.assertEqual(turn['status'], 'cancelled')
        self.assertFalse(self.runner.orchestrator_detail(session['id'])['busy'])

    def test_old_stop_timer_cannot_close_a_subsequent_turn(self):
        session = self.runner.create_orchestrator_session('Two consecutive turns')
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        release_second = threading.Event()

        class TurnConnection:
            def __init__(self, entered, release, stop_reason):
                self.alive = True
                self.session_id = 'turn-session'
                self.entered = entered
                self.release = release
                self.stop_reason = stop_reason
                self.cancel = Mock()
                self.close = Mock()

            def supports(self, _capability):
                return False

            def session_new(self, **_kwargs):
                return {'sessionId': self.session_id}

            def prompt(self, _text, **_kwargs):
                self.entered.set()
                if not self.release.wait(3):
                    raise TimeoutError('turn was not released')
                return {'stopReason': self.stop_reason}

        first_connection = TurnConnection(first_entered, release_first, 'end_turn')
        second_connection = TurnConnection(second_entered, release_second, 'end_turn')
        with patch.object(self.runner, '_orchestrator_connection',
                          side_effect=[first_connection, second_connection]), \
             patch.object(self.runner, '_apply_model'), \
             patch('coms.runner.ORCHESTRATOR_STOP_GRACE_SECONDS', .3):
            first = self.runner.orchestrate('First response', session['id'])
            try:
                self.assertTrue(first_entered.wait(2), 'first turn did not start')
                first_stop = self.runner.stop_orchestrator(session['id'])
                repeated_stop = self.runner.stop_orchestrator(session['id'])
                self.assertEqual(first_stop, repeated_stop)
                self.assertEqual(first_stop['turn_id'], first['turn_id'])
                first_connection.cancel.assert_called_once()
                release_first.set()
                deadline = time.monotonic() + 2
                while self.runner.orchestrator_busy and time.monotonic() < deadline:
                    time.sleep(.01)

                second = self.runner.orchestrate('Second response', session['id'])
                self.assertNotEqual(second['turn_id'], first['turn_id'])
                self.assertTrue(second_entered.wait(2), 'second turn did not start')
                time.sleep(.4)  # Let the first turn's bounded-close timer expire.
                self.assertTrue(self.runner.orchestrator_busy)
                self.assertEqual(self.runner.orchestrator_stop_turn_id, second['turn_id'])
                first_connection.close.assert_not_called()
                second_connection.close.assert_not_called()
            finally:
                release_first.set()
                release_second.set()
                deadline = time.monotonic() + 3
                while self.runner.orchestrator_busy and time.monotonic() < deadline:
                    time.sleep(.01)

        first_status = self.conn.execute('SELECT status FROM orchestrator_turns WHERE id=?',
                                         (first['turn_id'],)).fetchone()['status']
        second_status = self.conn.execute('SELECT status FROM orchestrator_turns WHERE id=?',
                                          (second['turn_id'],)).fetchone()['status']
        self.assertEqual(first_status, 'cancelled')
        self.assertEqual(second_status, 'completed')


if __name__ == '__main__':
    unittest.main()
