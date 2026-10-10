"""Monitor-only MCP surface and scoped team heartbeat coverage."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coms import api, db, mcp, progress, team
from coms.runner import board_servers


class TestMonitorTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent)
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'monitor.db')})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.job = api.create_ticket(self.conn, 'Heartbeat job', 'Coordinate child research.', 'admin', type_='job')
        self.lead = api.resolve_ticket_agent(self.conn, self.job)
        delegated = team.delegate(self.conn, self.job['id'], self.lead['slug'],
                                  'Evidence specialist', 'Primary-source research',
                                  'Check exchange mechanics', 'Find two primary sources')
        self.child = delegated['ticket']
        self.child_agent = delegated['agent']
        self.other_job = api.create_ticket(self.conn, 'Other job', '', 'admin', type_='job')
        api.resolve_ticket_agent(self.conn, self.other_job)

    def tearDown(self):
        conn = getattr(db._local, 'conn', None)
        if conn:
            conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def monitor_env(self):
        return patch.dict(os.environ, {
            'COMS_AGENT': self.lead['slug'],
            'COMS_TICKET_ID': str(self.job['id']),
        })

    def test_tools_list_exposes_team_monitor_tools_but_not_authoring_or_delivery(self):
        response = mcp.dispatch({'id': 1, 'method': 'tools/list'}, monitor_only=True)
        names = {item['name'] for item in response['result']['tools']}
        self.assertTrue({'team_read', 'team_send', 'team_note', 'team_reply', 'wiki_get', 'source_get'} <= names)
        self.assertFalse({'team_delegate', 'ticket_create', 'ticket_result', 'wiki_put'} & names)

    def test_monitor_forbidden_calls_are_rejected_by_dispatch_and_direct_call(self):
        forbidden = {
            'team_delegate': {'job_id': self.job['id'], 'name': 'Another specialist',
                              'capabilities': 'research', 'title': 'More work', 'body': 'Do more'},
            'ticket_create': {'title': 'Unapproved work'},
            'ticket_result': {'id': self.job['id'], 'result': 'Premature result'},
            'wiki_put': {'slug': 'monitor-page', 'title': 'Monitor page', 'body': 'Not allowed'},
        }
        with self.monitor_env():
            for name, arguments in forbidden.items():
                with self.subTest(tool=name):
                    response = mcp.dispatch({'id': 2, 'method': 'tools/call',
                                             'params': {'name': name, 'arguments': arguments}},
                                            monitor_only=True)['result']
                    self.assertTrue(response['isError'])
                    self.assertIn('tool unavailable', response['content'][0]['text'])
                    with self.assertRaisesRegex(ValueError, 'tool unavailable'):
                        mcp.call_tool(name, arguments, monitor_only=True)

    def test_monitor_tools_reject_a_different_job_scope(self):
        with self.monitor_env():
            with self.assertRaisesRegex(ValueError, 'assigned to this heartbeat'):
                mcp.call_tool('team_read', {'job_id': self.other_job['id']}, monitor_only=True)
            with self.assertRaisesRegex(ValueError, 'assigned to this heartbeat'):
                mcp.call_tool('team_note', {'job_id': self.other_job['id'], 'body': 'Wrong job'}, monitor_only=True)

    def test_monitor_can_reply_to_assigned_child_question_but_not_another_jobs_question(self):
        assigned_question = team.send(self.conn, self.job['id'], self.child_agent['slug'],
                                      self.lead['slug'], 'Which exchange should I prioritize?', kind='question')
        other_lead = api.get_ticket(self.conn, self.other_job['id'])['assignee']
        other_child = team.delegate(self.conn, self.other_job['id'], other_lead,
                                    'Other specialist', 'Separate job research',
                                    'Other assignment', 'Research unrelated evidence')['agent']
        foreign_question = team.send(self.conn, self.other_job['id'], other_child['slug'],
                                     self.lead['slug'], 'Can you review this unrelated request?', kind='question')

        with self.monitor_env():
            reply = mcp.call_tool('team_reply', {'message_id': assigned_question['id'],
                                                 'body': 'Prioritize the documented API path.'},
                                  monitor_only=True)
            response = json.loads(reply['content'][0]['text'])
            self.assertEqual(response['reply_to'], assigned_question['id'])
            delivery = self.conn.execute(
                'SELECT status,response_id FROM team_deliveries WHERE message_id=? AND recipient=?',
                (assigned_question['id'], self.lead['slug']),
            ).fetchone()
            self.assertEqual(delivery['status'], 'answered')
            self.assertEqual(delivery['response_id'], response['id'])

            with self.assertRaisesRegex(ValueError, 'assigned to this heartbeat'):
                mcp.call_tool('team_reply', {'message_id': foreign_question['id'],
                                             'body': 'I should not answer across jobs.'},
                              monitor_only=True)

    def test_assigned_team_read_returns_child_status_live_progress_and_recent_tools(self):
        child_run = self.conn.execute(
            "INSERT INTO agent_runs(ticket_id,backend,status,started_at,agent_slug,stage) "
            "VALUES(?,'codex','running',?,'','execute')",
            (self.child['id'], api.now()),
        ).lastrowid
        self.conn.execute('UPDATE agent_runs SET agent_slug=? WHERE id=?', (self.child_agent['slug'], child_run))
        self.conn.commit()
        recorder = progress.Recorder('run:' + str(child_run), self.child_agent['slug'],
                                     self.child['id'], child_run, 'codex')
        recorder.notification({'sessionUpdate': 'tool_call', 'toolCallId': 'monitor-read',
                               'title': 'swarm-agent_team_read', 'status': 'completed'})
        recorder.set('researching', 'Comparing primary sources')

        with self.monitor_env():
            result = mcp.call_tool('team_read', {'job_id': self.job['id']}, monitor_only=True)
        room = json.loads(result['content'][0]['text'])

        child = next(ticket for ticket in room['tickets'] if ticket['id'] == self.child['id'])
        self.assertEqual(child['title'], 'Check exchange mechanics')
        self.assertEqual(child['status'], self.child['status'])
        live = next(item for item in room['progress']['items'] if item['ticket_id'] == self.child['id'])
        self.assertEqual(live['summary'], 'Comparing primary sources')
        tool = next(item for item in room['recent_tools'] if item['ticket_id'] == self.child['id'])
        self.assertEqual(tool['run_id'], child_run)
        self.assertEqual(tool['title'], 'swarm-agent_team_read')
        self.assertEqual(tool['status'], 'completed')

    def test_board_servers_adds_monitor_flag_and_assignment_environment(self):
        server = board_servers(self.lead['slug'], ticket_id=self.job['id'], run_id=73, monitor_only=True)[0]
        self.assertIn('--monitor', server['args'])
        environment = {entry['name']: entry['value'] for entry in server['env']}
        self.assertEqual(environment['COMS_AGENT'], self.lead['slug'])
        self.assertEqual(environment['COMS_TICKET_ID'], str(self.job['id']))
        self.assertEqual(environment['COMS_RUN_ID'], '73')


if __name__ == '__main__':
    unittest.main()
