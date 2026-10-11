"""Regressions for workspace workers, evidence and bounded team context."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, mcp, team, skill_store
from coms.runner import Runner, backend_environment


class WorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'work'
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {'COMS_DB': str(self.root / 'board.db')})
        self.env.start()
        self.skills = patch.object(skill_store, 'SKILLS_ROOT', self.root / 'skills')
        self.skills.start()
        self.policy = patch('coms.workspace.workspace_path', side_effect=lambda settings, **kw: settings['workspace'])
        self.policy.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        for key, value in {'workspace': str(self.workspace), 'active_backend': 'codex',
                           'codex_command': 'unused-acp-command'}.items():
            self.conn.execute('INSERT INTO runner_settings VALUES(?,?)', (key, value))
        self.conn.commit()
        self.runner = Runner()
        self.job = api.create_ticket(self.conn, 'Audit code', 'Inspect the configured workspace.',
                                     'admin', type_='job', collaboration_required=True)
        self.lead = api.resolve_ticket_agent(self.conn, self.job)['slug']
        self.worker = team.delegate(self.conn, self.job['id'], self.lead, 'Tess', 'Test audits',
                                    'Inspect tests', 'Read tests and report failures')['ticket']
        self.reviewer = team.delegate(self.conn, self.job['id'], self.lead, 'Dex', 'Code review',
                                      'Review findings', 'Verify the reported source')['ticket']

    def tearDown(self):
        self.runner.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.policy.stop()
        self.skills.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_local_provider_gets_search_and_required_native_tools_despite_legacy_flags(self):
        with patch('coms.runner._opencode_model_ids', return_value=['model']):
            env = backend_environment('opencode', {}, {'COMS_BOARD_ONLY': '1', 'OPENCODE_ENABLE_EXA': '0'})
        config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(env['OPENCODE_ENABLE_EXA'], '1')
        self.assertEqual(config['default_agent'], 'build')
        for name in ('read', 'grep', 'glob', 'bash', 'edit', 'webfetch', 'websearch'):
            with self.subTest(tool=name):
                self.assertEqual(config['permission'][name], 'allow')
                self.assertEqual(config['agent']['build']['permission'][name], 'allow')
        self.assertNotIn('*', config['permission'])
        self.assertNotIn('doom_loop', config['permission'])
        self.assertNotIn('tools', config)

    def test_all_connection_roles_keep_native_tool_configuration(self):
        for mode in ({}, {'dispatch_only': True}, {'monitor_only': True}, {'collaboration_message_id': 1}):
            with self.subTest(mode=mode), patch('coms.runner.AcpConnection', return_value=Mock(session_id='fake')) as backend, patch('coms.runner.threading.Timer'), patch('coms.runner._opencode_model_ids', return_value=['model']), patch.object(self.runner, '_apply_model'):
                self.runner._conn(self.worker['id'], 'opencode', str(self.workspace), **mode)
                env = backend.call_args.kwargs['env']
                self.assertNotIn('COMS_BOARD_ONLY', env)
                self.assertEqual(env['OPENCODE_ENABLE_EXA'], '1')
                self.assertEqual(json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']['websearch'], 'allow')

    def test_orchestrator_gets_the_same_required_tool_environment(self):
        settings = self.runner.settings()
        settings['opencode_command'] = 'unused-acp-command'
        with patch('coms.runner.AcpConnection', return_value=Mock(session_id='fake')) as backend, patch('coms.runner._opencode_model_ids', return_value=['model']), patch.object(self.runner, '_apply_model'):
            self.runner._orchestrator_connection('opencode', str(self.workspace), settings)
        env = backend.call_args.kwargs['env']
        self.assertEqual(env['OPENCODE_ENABLE_EXA'], '1')
        self.assertEqual(json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']['websearch'], 'allow')

    def test_other_backends_do_not_receive_opencode_tool_overrides(self):
        for backend in ('codex', 'claude'):
            with self.subTest(backend=backend), patch('coms.runner._opencode_model_ids') as discovery:
                self.assertEqual(backend_environment(backend, {}, {'COMS_AGENT': 'Tess'}), {'COMS_AGENT': 'Tess'})
                discovery.assert_not_called()

    def test_response_turn_uses_recipient_workspace_without_requiring_research_skill(self):
        reply_workspace = self.root / 'reply-workspace'
        reply_workspace.mkdir()
        api.update_ticket(self.conn, self.reviewer['id'], 'admin', workspace=str(reply_workspace))
        question = team.send(self.conn, self.job['id'], self.worker['assignee'], self.reviewer['assignee'], 'Which test should be checked?')
        fake = Mock(session_id='fake')
        fake.prompt.side_effect = lambda *a, **kw: team.reply(self.conn, question['id'], self.reviewer['assignee'], 'Check the failing HTTP test.')
        with patch('coms.runner.workspace_path', side_effect=lambda settings: settings['workspace']), patch.object(self.runner, '_conn', return_value=fake) as connection:
            self.runner._reply_team(question['id'], self.reviewer['assignee'])
        self.assertEqual(connection.call_args.args[2], str(reply_workspace))
        self.assertIn('Native tools', fake.prompt.call_args.args[0])
        self.assertEqual(self.conn.execute('SELECT status FROM team_deliveries WHERE message_id=?', (question['id'],)).fetchone()[0], 'answered')
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'completed')

    def test_delegation_defaults_to_task_and_inherits_workspace(self):
        api.update_ticket(self.conn, self.job['id'], 'admin', workspace=str(self.workspace))
        result = team.delegate(self.conn, self.job['id'], self.lead, 'Ivy', 'Docs', 'Read docs', 'Inspect README')
        self.assertEqual(result['ticket']['type'], 'task')
        self.assertEqual(result['ticket']['workspace'], str(self.workspace))
        self.assertIn('source_read_file', result['ticket']['body'])
        self.assertNotIn('Parent research job', result['ticket']['body'])

    def test_delegation_can_explicitly_request_web_research(self):
        with patch.dict(os.environ, {'COMS_AGENT': self.lead}):
            result = mcp.call_tool('team_delegate', {'job_id': self.job['id'], 'name': 'Ravi',
                'capabilities': 'Public docs', 'title': 'Read docs', 'body': 'Inspect public docs', 'type': 'research'})
        self.assertEqual(json.loads(result['content'][0]['text'])['ticket']['type'], 'research')

    def _run(self, ticket):
        rid = self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,agent_slug,stage) VALUES(?,'codex','running',?,'execute')",
                                (ticket['id'], ticket['assignee'])).lastrowid
        self.conn.commit()
        return rid

    def _read_file(self, ticket, rid, path, **kwargs):
        with patch.dict(os.environ, {'COMS_AGENT': ticket['assignee'], 'COMS_TICKET_ID': str(ticket['id']), 'COMS_RUN_ID': str(rid)}):
            result = mcp.call_tool('source_read_file', {'job_id': self.job['id'], 'path': str(path), **kwargs})
        return json.loads(result['content'][0]['text'])

    def test_workspace_evidence_is_recorded_with_lines_hash_and_review_access(self):
        path = self.workspace / 'module.py'
        path.write_text('first\nsecond\nthird\n', encoding='utf-8')
        rid = self._run(self.worker)
        source = self._read_file(self.worker, rid, 'module.py', start_line=2, end_line=3)
        self.assertEqual(source['status'], 'ok')
        self.assertIn('2: second', source['body'])
        self.assertIn('SHA256:', source['body'])
        self.assertNotIn('1: first', source['body'])
        self.assertEqual(source['url'], path.as_uri() + '#L2-L3')
        self.assertTrue(self.conn.execute('SELECT 1 FROM source_reads WHERE run_id=? AND source_id=?', (rid, source['id'])).fetchone())
        page = api.save_wiki(self.conn, 'tess-evidence', 'Finding', 'Inspected ' + source['url'], self.worker['assignee'])
        api.link_ticket(self.conn, self.worker['id'], 'wiki', page['slug'], self.worker['assignee'])
        question = team.send(self.conn, self.job['id'], self.worker['assignee'], self.reviewer['assignee'], 'Check the module?')
        team.reply(self.conn, question['id'], self.reviewer['assignee'], 'Checked the scope.')
        review = team.request_review(self.conn, self.job['id'], self.worker['id'], self.worker['assignee'], page['slug'], self.reviewer['assignee'])
        rrid = self._run(self.reviewer)
        with patch.dict(os.environ, {'COMS_AGENT': self.reviewer['assignee'], 'COMS_TICKET_ID': str(self.reviewer['id']), 'COMS_RUN_ID': str(rrid)}):
            mcp.call_tool('wiki_get', {'slug': page['slug']}, collaboration=True)
            with self.assertRaises(api.BadRequest):
                mcp.call_tool('team_review', {'review_id': review['id'], 'decision': 'accepted', 'body': 'Verified.'}, collaboration=True)
            mcp.call_tool('source_get', {'source_id': source['id']}, collaboration=True)
            mcp.call_tool('team_review', {'review_id': review['id'], 'decision': 'accepted', 'body': 'Verified lines 2 and 3.'}, collaboration=True)
        team.validate_delivery(self.conn, self.worker['id'], self.worker['assignee'])
        api.save_wiki(self.conn, page['slug'], 'Finding', 'Changed after review ' + source['url'], self.worker['assignee'])
        with self.assertRaises(api.BadRequest):
            team.validate_delivery(self.conn, self.worker['id'], self.worker['assignee'])

    def test_file_evidence_rejects_escape_binary_and_invalid_line_ranges(self):
        outside = self.root / 'outside.py'
        outside.write_text('private', encoding='utf-8')
        (self.workspace / 'binary').write_bytes(b'\xff\x00')
        (self.workspace / 'module.py').write_text('one\ntwo\n', encoding='utf-8')
        rid = self._run(self.worker)
        for path, kwargs in [('../outside.py', {}), (outside, {}), ('binary', {}),
                             ('module.py', {'start_line': 0}), ('module.py', {'start_line': 3}),
                             ('module.py', {'start_line': 2, 'end_line': 1}),
                             ('module.py', {'end_line': 999})]:
            with self.subTest(path=path, kwargs=kwargs), self.assertRaises((api.BadRequest, ValueError)):
                self._read_file(self.worker, rid, path, **kwargs)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM research_sources').fetchone()[0], 0)

    def test_file_evidence_rejects_cross_job_access(self):
        other = api.create_ticket(self.conn, 'Other job', '', 'admin', type_='job')
        with patch.dict(os.environ, {'COMS_AGENT': self.worker['assignee'], 'COMS_TICKET_ID': str(self.worker['id'])}):
            with self.assertRaises((ValueError, api.BadRequest)):
                mcp.call_tool('source_read_file', {'job_id': other['id'], 'path': 'module.py'})

    def test_file_evidence_cannot_follow_symlink_outside_workspace(self):
        outside = self.root / 'outside.txt'
        outside.write_text('private', encoding='utf-8')
        link = self.workspace / 'link.txt'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks unavailable on this platform')
        with self.assertRaises(api.BadRequest):
            self._read_file(self.worker, self._run(self.worker), link)

    def test_team_read_is_bounded_and_can_retrieve_a_specific_message(self):
        for i in range(100):
            team.send(self.conn, self.job['id'], self.lead, '*', ('Long note %d ' % i) + 'x' * 4000, kind='note')
        question = team.send(self.conn, self.job['id'], self.worker['assignee'], self.reviewer['assignee'], 'An older pending request')
        self.conn.executemany("INSERT INTO research_sources(job_id,reader,url,title,status,retrieved_at) VALUES(?,?,?,?,'failed','now')",
            [(self.job['id'], self.worker['assignee'], 'https://example.com/' + str(i), 'Example',) for i in range(500)])
        self.conn.commit()
        payload = json.loads(mcp.call_tool('team_read', {'job_id': self.job['id']})['content'][0]['text'])
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode('utf-8')), 32000)
        self.assertEqual(payload['counts']['sources'], 500)
        self.assertTrue(any(m['id'] == question['id'] for m in payload['pending_requests']))
        targeted = json.loads(mcp.call_tool('team_read', {'job_id': self.job['id'], 'message_id': question['id']})['content'][0]['text'])
        self.assertEqual(targeted['messages'][0]['body'], 'An older pending request')
        second = json.loads(mcp.call_tool('team_read', {'job_id': self.job['id'], 'source_after_id': payload['sources'][-1]['id']})['content'][0]['text'])
        self.assertGreater(second['sources'][0]['id'], payload['sources'][-1]['id'])

    def test_long_unicode_message_is_readable_in_bounded_pages(self):
        body = '\U0001f30e' * 14000
        message = team.send(self.conn, self.job['id'], self.lead, '*', body, kind='note')
        recovered, offset = '', 0
        while offset is not None:
            payload = json.loads(mcp.call_tool('team_read', {'job_id': self.job['id'], 'message_id': message['id'], 'offset': offset})['content'][0]['text'])
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode('utf-8')), 32000)
            recovered += payload['messages'][0]['body']
            offset = payload['messages'][0]['next_offset']
        self.assertEqual(recovered, body)

    def test_timed_out_worker_is_failed_and_releases_its_connection(self):
        fake = Mock(session_id='fake')
        fake.prompt.side_effect = RuntimeError('hard timeout waiting for session/prompt')
        with patch('coms.runner.workspace_path', return_value=str(self.workspace)), patch.object(self.runner, '_conn', return_value=fake):
            self.runner._work(self.worker)
        run = self.conn.execute('SELECT status,error FROM agent_runs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(run['status'], 'failed')
        self.assertIn('hard timeout', run['error'])
        fake.close.assert_called_once()
        self.assertNotIn(self.worker['id'], self.runner.active)
        self.assertNotIn(self.worker['id'], self.runner.conns)

    def test_worker_prompt_has_explicit_hard_deadline_and_workspace_evidence(self):
        fake = Mock(session_id='fake')
        with patch('coms.runner.workspace_path', return_value=str(self.workspace)), patch.object(self.runner, '_conn', return_value=fake):
            self.runner._work(self.worker)
        prompt = fake.prompt.call_args.args[0]
        self.assertIn('source_read_file', prompt)
        self.assertIn('native tools', prompt)
        self.assertEqual(fake.prompt.call_args.kwargs['total_timeout'], 900)


if __name__ == '__main__':
    unittest.main()
