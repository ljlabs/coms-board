"""Source attribution and activity across nested delegated work."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coms import api, db, progress, mcp, schedules
from coms.runner import Runner
from coms.orchestrator_transcript import begin_turn


class AgentActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent)
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'board.db'),
                                         'COMS_TICKET_ID': '', 'COMS_AGENT': 'orchestrator'})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        api.orchestrator_agent(self.conn)
        api.upsert_agent(self.conn, 'lead', 'Lead', role='expert')
        api.upsert_agent(self.conn, 'worker', 'Worker', role='expert')
        self.runner = Runner()
        self.session = self.runner.create_orchestrator_session('Source chat')
        self.other = self.runner.create_orchestrator_session('Unrelated chat')

    def tearDown(self):
        self.runner.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def ticket(self, title, **kwargs):
        return api.create_ticket(self.conn, title, '', kwargs.pop('requester', 'lead'),
                                 assignee=kwargs.pop('assignee', 'worker'), **kwargs)

    def record(self, ticket, key=None, state='running'):
        recorder = progress.Recorder(key or 'test:' + str(ticket['id']), ticket['assignee'], ticket['id'])
        if state != 'running': recorder.finish(state)
        return recorder

    def test_nested_work_keeps_only_its_source_chat_active(self):
        root = self.ticket('Root', assignee='lead', source_session_id=self.session['id'])
        child = self.ticket('Child', parent_id=root['id'])
        grandchild = self.ticket('Grandchild', parent_id=child['id'])
        self.record(root, state='completed')
        worker = self.record(grandchild)
        activity = progress.activity(self.conn)
        self.assertEqual(activity['tickets'][root['id']]['active_count'], 1)
        sessions = {row['id']: row for row in self.runner.orchestrator_sessions()}
        self.assertTrue(sessions[self.session['id']]['working'])
        self.assertFalse(sessions[self.other['id']]['working'])
        detail = self.runner.orchestrator_detail(self.session['id'])
        self.assertTrue(detail['working'])
        self.assertFalse(detail['busy'], 'Descendants must not reserve the chat ACP slot')
        snapshot = progress.snapshot(self.conn, ticket=root['id'])
        self.assertIn(grandchild['id'], [item['ticket_id'] for item in snapshot['items']])
        lead = next(item for item in snapshot['items'] if item['ticket_id'] == root['id'])
        self.assertEqual(lead['state'], 'waiting')
        worker.finish('waiting')
        self.assertTrue(self.runner.orchestrator_detail(self.session['id'])['working'])
        worker.finish()
        self.assertFalse(self.runner.orchestrator_detail(self.session['id'])['working'])
        self.assertEqual(progress.snapshot(self.conn)['waiting_count'], 0)
        self.assertEqual(next(item for item in progress.snapshot(self.conn)['items']
                              if item['ticket_id'] == root['id'])['state'], 'completed')

    def test_schedule_source_and_activity_propagate_to_grandchildren(self):
        with patch('coms.schedules.workspace_path', return_value=str(Path(self.tmp.name))):
            task = schedules.create_task(self.conn, {'name': 'Recurring report', 'prompt': 'Report',
            'schedule_type': 'interval', 'schedule_config': {'every': 1, 'unit': 'hours'},
            'timezone': 'UTC', 'agent_slug': 'lead', 'workspace': str(Path(self.tmp.name))}, 'admin')
        root_id = schedules.run_now(self.conn, task['id'], 'admin')['ticket_id']
        self.conn.execute("UPDATE tickets SET status='in_progress' WHERE id=?", (root_id,))
        self.conn.commit()
        child = self.ticket('Child', parent_id=root_id)
        grandchild = self.ticket('Grandchild', parent_id=child['id'])
        worker = self.record(grandchild)
        item = next(row for row in progress.snapshot(self.conn)['items'] if row['ticket_id'] == grandchild['id'])
        self.assertEqual(item['source']['slug'], 'lead')
        self.assertEqual(item['source']['name'], 'Lead')
        self.assertEqual(item['schedule']['id'], task['id'])
        self.assertTrue(schedules.list_tasks(self.conn)[0]['working'])
        self.assertTrue(schedules.get_run(self.conn, 1)['working'])
        worker.finish()
        self.assertFalse(schedules.list_tasks(self.conn)[0]['working'])

    def test_mcp_records_chat_and_worker_origins_without_parent(self):
        turn, _ = begin_turn(self.session['id'], 'Delegate', 'codex', 'model', '')
        result = mcp.call_tool('ticket_create', {'title': 'Dispatched', 'assignee': 'lead'})
        root = json.loads(result['content'][0]['text'])
        self.assertEqual(root['source_session_id'], self.session['id'])
        with patch.dict(os.environ, {'COMS_AGENT': 'lead', 'COMS_TICKET_ID': str(root['id'])}):
            child = json.loads(mcp.call_tool('ticket_create', {'title': 'Unparented child', 'assignee': 'worker'})['content'][0]['text'])
        self.assertEqual(child['source_ticket_id'], root['id'])
        self.record(child)
        self.conn.execute("UPDATE orchestrator_turns SET status='completed' WHERE id=?", (turn['id'],))
        self.conn.commit()
        self.assertTrue(self.runner.orchestrator_detail(self.session['id'])['working'])
        source = progress.snapshot(self.conn)['items'][0]
        self.assertEqual(source['orchestrator_session']['id'], self.session['id'])
        self.assertEqual(source['source']['slug'], 'lead')

    def test_legacy_chat_origin_uses_exact_successful_ticket_output(self):
        root = self.ticket('Old job', requester='orchestrator')
        child = self.ticket('Old child', parent_id=root['id'])
        data = {'title': 'swarm-agent.ticket_create', 'status': 'completed',
                'rawOutput': {'content': [{'type': 'text', 'text': json.dumps(root)}]}}
        self.conn.execute("INSERT INTO orchestrator_messages(session_id,role,kind,body,created_at,metadata_json,state) VALUES(?,'tool','tool_call','',?,?,'completed')",
                          (self.session['id'], api.now(), json.dumps(data)))
        self.conn.commit()
        self.record(child)
        self.assertTrue(self.runner.orchestrator_detail(self.session['id'])['working'])
        self.assertFalse(self.runner.orchestrator_detail(self.other['id'])['working'])

    def test_terminal_tickets_and_cycles_do_not_keep_parents_working(self):
        root = self.ticket('Root', source_session_id=self.session['id'])
        child = self.ticket('Child', parent_id=root['id'])
        self.record(child, state='waiting')
        self.conn.execute("UPDATE tickets SET status='wontfix' WHERE id=?", (child['id'],))
        self.conn.execute('UPDATE tickets SET source_ticket_id=? WHERE id=?', (child['id'], root['id']))
        self.conn.commit()
        self.assertFalse(self.runner.orchestrator_detail(self.session['id'])['working'])
        self.assertEqual(progress.snapshot(self.conn)['waiting_count'], 0)

    def test_legacy_content_outputs_preserve_explicit_origin_over_forks(self):
        root = self.ticket('Original work', source_session_id=self.session['id'])
        payload = {'name': 'ticket_create', 'content': [{'type': 'content', 'content':
            {'type': 'text', 'text': json.dumps(root)}}]}
        self.conn.execute("INSERT INTO orchestrator_messages(session_id,role,kind,body,created_at,metadata_json,state) VALUES(?,'tool','tool_call','',?,?,'completed')",
                          (self.other['id'], api.now(), json.dumps(payload)))
        self.conn.commit()
        self.record(root)
        self.assertTrue(self.runner.orchestrator_detail(self.session['id'])['working'])
        self.assertFalse(self.runner.orchestrator_detail(self.other['id'])['working'])

    def test_failed_or_missing_legacy_output_does_not_assign_a_chat(self):
        root = self.ticket('Unknown origin', requester='orchestrator')
        for payload in [{'title': 'ticket_create', 'rawOutput': {'isError': True, 'content':
                        [{'text': json.dumps(root)}]}}, {'title': 'ticket_create', 'rawOutput': 'invalid JSON'}]:
            self.conn.execute("INSERT INTO orchestrator_messages(session_id,role,kind,body,created_at,metadata_json,state) VALUES(?,'tool','tool_call','',?,?,'completed')",
                              (self.session['id'], api.now(), json.dumps(payload)))
        self.conn.commit()
        self.record(root)
        self.assertFalse(self.runner.orchestrator_detail(self.session['id'])['working'])

    def test_activity_is_not_limited_by_the_eighty_progress_rows(self):
        root = self.ticket('Root', source_session_id=self.session['id'])
        self.record(self.ticket('Child', parent_id=root['id']))
        for index in range(81): self.record(self.ticket('Unrelated ' + str(index)))
        self.assertEqual(len(progress.snapshot(self.conn)['items']), 80)
        self.assertEqual(self.runner.orchestrator_detail(self.session['id'])['activity']['active_count'], 1)

    def test_queued_children_and_direct_chat_turns_are_visible(self):
        root = self.ticket('Queued work', source_session_id=self.session['id'], ready_for_execution=True)
        self.assertTrue(self.runner.orchestrator_detail(self.session['id'])['working'])
        self.conn.execute("UPDATE tickets SET status='done' WHERE id=?", (root['id'],))
        self.conn.commit()
        self.assertFalse(self.runner.orchestrator_detail(self.session['id'])['working'])
        begin_turn(self.session['id'], 'Hello', 'codex', 'model', '')
        self.assertTrue(next(row for row in self.runner.orchestrator_sessions() if row['id'] == self.session['id'])['working'])

    def test_existing_schema_gets_source_columns(self):
        path = Path(self.tmp.name) / 'legacy.db'
        with sqlite3.connect(path) as legacy:
            legacy.executescript(db.SCHEMA)
        legacy.close()
        migrated = db.connect(path)
        columns = {row[1] for row in migrated.execute('PRAGMA table_info(tickets)')}
        migrated.close()
        self.assertIn('source_session_id', columns)
        self.assertIn('source_ticket_id', columns)
