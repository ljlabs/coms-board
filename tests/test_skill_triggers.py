"""Skill triggers are file-backed and load only from the current request."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, mcp, skill_store, team
from coms.runner import Runner
from coms.server import r_skills


class TestSkillTriggers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=skill_store.APP_ROOT)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for context in [patch.object(skill_store, 'SKILLS_ROOT', root / 'skills'),
                        patch.dict(os.environ, {'COMS_DB': str(root / 'board.db'), 'COMS_AGENT': 'admin'})]:
            context.start()
            self.addCleanup(context.stop)
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.addCleanup(self.close_db)
        self.skill = api.save_skill(self.conn, 'evidence', 'Evidence',
                                    'UNIQUE EVIDENCE INSTRUCTIONS', 'admin',
                                    description='Use for evidence checks.',
                                    trigger_words=['research', 'peer review', 'C++'])

    def close_db(self):
        self.conn.close()
        db._local.__dict__.clear()

    def test_metadata_round_trips_and_old_skills_default_to_no_triggers(self):
        self.assertEqual(self.skill['trigger_words'], ['research', 'peer review', 'C++'])
        self.assertEqual(api.list_skills(self.conn)[0]['trigger_words'], self.skill['trigger_words'])
        self.assertIn('trigger_words: [', self.skill['body'])
        plain = api.save_skill(self.conn, 'plain', 'Plain', 'Instructions', 'admin')
        self.assertEqual(plain['trigger_words'], [])
        self.assertFalse(plain['builtin'])

    def test_builtin_workflows_are_real_skills_with_triggers(self):
        with patch.object(skill_store, 'SKILLS_ROOT', skill_store.APP_ROOT / 'skills'):
            skills = {s['slug']: s for s in api.list_skills(self.conn)}
            for slug in ['swarmboard', 'ticket-management', 'knowledge-management',
                         'scheduled-agent-tasks', 'team-orchestration', 'source-backed-research']:
                with self.subTest(slug=slug):
                    self.assertTrue(skills[slug]['builtin'])
                    self.assertTrue(skills[slug]['trigger_words'])
                    self.assertTrue(api.get_skill(self.conn, slug)['body'])

    def test_matching_handles_case_phrases_boundaries_and_regex_characters(self):
        for text in ['RESEARCH this', 'Request a Peer\n review.', 'Check C++ code']:
            with self.subTest(text=text):
                context = api.triggered_skill_context(self.conn, text, 'worker')
                self.assertEqual(context.count('UNIQUE EVIDENCE INSTRUCTIONS'), 1)
        for text in ['researcher', 'presearch', 'peer reviewer', 'C--', 'unrelated work', '']:
            with self.subTest(text=text):
                self.assertEqual(api.triggered_skill_context(self.conn, text, 'worker'), '')

    def test_repeated_triggers_load_once_and_record_usage(self):
        context = api.triggered_skill_context(self.conn, 'research RESEARCH peer review', 'worker')
        self.assertEqual(context.count('UNIQUE EVIDENCE INSTRUCTIONS'), 1)
        saved = api.get_skill(self.conn, 'evidence')
        self.assertEqual(saved['loaded_by'], ['worker'])
        self.assertEqual(saved['access_count'], 1)
        self.assertEqual(api.triggered_skill_context(self.conn, 'no match', 'worker'), '')
        self.assertEqual(api.get_skill(self.conn, 'evidence')['access_count'], 1)

    def test_disabled_skills_are_excluded_and_changes_apply_next_turn(self):
        api.set_skill_enabled(self.conn, 'evidence', False, 'admin')
        self.assertEqual(api.triggered_skill_context(self.conn, 'research', 'worker'), '')
        api.set_skill_enabled(self.conn, 'evidence', True, 'admin')
        saved = api.set_skill_trigger_words(self.conn, 'evidence', ['audit'], 'admin', self.skill['version'])
        self.assertEqual(api.triggered_skill_context(self.conn, 'research', 'worker'), '')
        self.assertIn('UNIQUE EVIDENCE INSTRUCTIONS', api.triggered_skill_context(self.conn, 'audit', 'worker'))
        self.assertEqual(saved['trigger_words'], ['audit'])

    def test_trigger_patch_normalizes_and_preserves_content_and_frontmatter(self):
        manifest = '---\nname: evidence\nowner: repository\ntrigger_words:\n  - old\n---\n\n# Evidence\nBody unchanged.\n'
        api.write_skill_file(self.conn, 'evidence', 'SKILL.md', manifest, 'admin')
        current = api.get_skill(self.conn, 'evidence')
        self.assertEqual(current['trigger_words'], ['old'])
        match = re.match(r'^/api/skills/([^/]+)$', '/api/skills/evidence')
        saved = r_skills(self.conn, 'PATCH', match, {},
                         {'trigger_words': ['  Peer   review ', 'peer review', 'audit'], 'version': current['version']}, 'admin')
        self.assertEqual(saved['trigger_words'], ['Peer review', 'audit'])
        self.assertIn('owner: repository', saved['body'])
        self.assertTrue(saved['body'].endswith('# Evidence\nBody unchanged.\n'))
        self.assertNotIn('  - old', saved['body'])
        cleared = api.set_skill_trigger_words(self.conn, 'evidence', [], 'admin', saved['version'])
        self.assertEqual(cleared['trigger_words'], [])

    def test_builtin_files_are_readonly_but_settings_can_be_changed(self):
        builtin = api.save_skill(self.conn, 'bundled', 'Bundled',
                                 '---\nbuiltin: true\ntrigger_words: ["old"]\n---\n# Bundled\nImmutable instructions.', 'admin')
        self.assertTrue(builtin['read_only'])
        api.upsert_agent(self.conn, 'lead', 'Lead', role='orchestrator')
        for actor in ['admin', 'lead']:
            actions = [
                lambda: api.save_skill(self.conn, 'bundled', 'Changed', 'Bad edit', actor, builtin['version']),
                lambda: api.write_skill_file(self.conn, 'bundled', 'SKILL.md', 'Bad edit', actor, builtin['version']),
                lambda: api.create_skill_entry(self.conn, 'bundled', 'notes.md', 'file', actor, builtin['tree_version']),
                lambda: api.move_skill_entry(self.conn, 'bundled', 'SKILL.md', 'renamed.md', actor, builtin['tree_version']),
                lambda: api.delete_skill(self.conn, 'bundled', actor),
            ]
            for action in actions:
                with self.subTest(actor=actor, action=action), self.assertRaisesRegex(api.BadRequest, 'read.only'):
                    action()
        saved = api.set_skill_trigger_words(self.conn, 'bundled', ['new'], 'admin', builtin['version'])
        self.assertEqual(saved['body'], builtin['body'])
        self.assertEqual(saved['trigger_words'], ['new'])
        self.assertNotEqual(saved['version'], builtin['version'])
        with self.assertRaises(api.BadRequest):
            api.set_skill_trigger_words(self.conn, 'bundled', ['stale'], 'admin', builtin['version'])
        self.assertIn('Immutable instructions.', api.triggered_skill_context(self.conn, 'new', 'worker'))
        api.set_skill_enabled(self.conn, 'bundled', False, 'admin')
        self.assertEqual(api.triggered_skill_context(self.conn, 'new', 'worker'), '')
        with self.assertRaisesRegex(api.BadRequest, 'read.only'):
            mcp.call_tool('skill_file_write', {'slug': 'bundled', 'path': 'SKILL.md', 'content': 'Bad edit',
                                              'version': skill_store.get_file('bundled', 'SKILL.md')['version']})

    def test_invalid_unauthorized_and_stale_edits_do_not_change_files(self):
        for triggers in ['research', [3], [''], ['x' * 81], ['x\nword'], ['word'] * 33, None]:
            with self.subTest(triggers=triggers), self.assertRaises(api.BadRequest):
                api.set_skill_trigger_words(self.conn, 'evidence', triggers, 'admin', self.skill['version'])
        api.upsert_agent(self.conn, 'worker', 'Worker', role='expert')
        with self.assertRaises(api.BadRequest):
            api.set_skill_trigger_words(self.conn, 'evidence', ['audit'], 'worker', self.skill['version'])
        with self.assertRaises(api.BadRequest):
            api.set_skill_trigger_words(self.conn, 'evidence', ['audit'], 'admin', 0)
        self.assertEqual(api.get_skill(self.conn, 'evidence')['body'], self.skill['body'])

    def test_mcp_creation_accepts_trigger_words(self):
        result = mcp.call_tool('skill_put', {'slug': 'audit', 'title': 'Audit', 'body': 'Inspect evidence.',
                                              'trigger_words': ['audit']})
        self.assertFalse(result.get('isError'))
        self.assertEqual(api.get_skill(self.conn, 'audit')['trigger_words'], ['audit'])

    def test_orchestrator_matches_current_text_without_reactivating_history(self):
        runner = Runner()
        self.addCleanup(runner.stop)
        connection = Mock(session_id='session', supports=Mock(return_value=False),
                          session_new=Mock(return_value={'sessionId': 'session'}),
                          prompt=Mock(return_value={'stopReason': 'end_turn'}))
        session = runner.create_orchestrator_session()
        with patch.object(runner, '_orchestrator_connection', return_value=connection), \
                patch.object(runner, '_orchestrator_workspace', return_value=str(Path(self.tmp.name))), \
                patch('coms.runner.history_before', return_value='User: research old topic'):
            runner._orchestrator_turn('Please RESEARCH this.', session['id'], session)
            self.assertIn('UNIQUE EVIDENCE INSTRUCTIONS', connection.prompt.call_args.args[0])
            runner._orchestrator_turn('Unrelated follow-up', session['id'], session)
            prompt = connection.prompt.call_args.args[0]
            self.assertIn('research old topic', prompt)
            self.assertNotIn('UNIQUE EVIDENCE INSTRUCTIONS', prompt)

    def test_ticket_followup_injects_for_live_and_recreated_sessions(self):
        runner = Runner()
        self.addCleanup(runner.stop)
        ticket = api.create_ticket(self.conn, 'Old research', '', 'admin', assignee='worker')
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,session_id,status,stage,agent_slug) VALUES(?,'codex','session','completed','execute','worker')", (ticket['id'],))
        self.conn.commit()
        connection = Mock(session_id='session', ticket_restored=False)
        runner._prompt(connection, ticket['id'], 'peer review this')
        self.assertIn('UNIQUE EVIDENCE INSTRUCTIONS', connection.prompt.call_args.args[0])
        with patch.object(runner, '_conn', return_value=connection), \
                patch('coms.runner.workspace_path', return_value=self.tmp.name):
            runner._prompt(None, ticket['id'], 'Unrelated follow-up')
            self.assertIn('Old research', connection.prompt.call_args.args[0])
            self.assertNotIn('UNIQUE EVIDENCE INSTRUCTIONS', connection.prompt.call_args.args[0])
            runner._prompt(None, ticket['id'], 'research new topic')
            self.assertEqual(connection.prompt.call_args.args[0].count('UNIQUE EVIDENCE INSTRUCTIONS'), 1)

    def test_worker_assignment_loads_matching_skill_and_records_recipient(self):
        runner = Runner()
        self.addCleanup(runner.stop)
        api.upsert_agent(self.conn, 'worker', 'Worker', role='expert')
        ticket = api.create_ticket(self.conn, 'Inspect files', 'Research the evidence.', 'admin',
                                   assignee='worker', ready_for_execution=True)
        connection = Mock(session_id='session')
        def deliver(*args, **kwargs):
            rid = self.conn.execute('SELECT MAX(id) FROM agent_runs').fetchone()[0]
            with patch.dict(os.environ, {'COMS_AGENT': 'worker', 'COMS_TICKET_ID': str(ticket['id']), 'COMS_RUN_ID': str(rid)}):
                mcp.call_tool('ticket_result', {'id': ticket['id'], 'result': 'Verified files.'})
        connection.prompt.side_effect = deliver
        with patch.object(runner, '_conn', return_value=connection), \
                patch('coms.runner.workspace_path', return_value=self.tmp.name):
            runner._work(ticket)
        self.assertEqual(connection.prompt.call_args.args[0].count('UNIQUE EVIDENCE INSTRUCTIONS'), 1)
        self.assertEqual(api.get_skill(self.conn, 'evidence')['loaded_by'], ['worker'])
        self.assertEqual(self.conn.execute('SELECT status FROM agent_runs').fetchone()[0], 'completed')

    def test_teammate_response_matches_message_and_not_job_history(self):
        runner = Runner()
        self.addCleanup(runner.stop)
        api.upsert_agent(self.conn, 'worker', 'Worker', role='expert')
        api.orchestrator_agent(self.conn)
        job = api.create_ticket(self.conn, 'Research job', '', 'admin', type_='job', assignee='orchestrator')
        api.create_ticket(self.conn, 'Assigned research', '', 'admin', parent_id=job['id'], assignee='worker')
        message = team.send(self.conn, job['id'], 'orchestrator', 'worker', 'Please peer review this evidence.')
        connection = Mock(session_id='session')
        connection.prompt.side_effect = lambda *args, **kwargs: team.reply(self.conn, message['id'], 'worker', 'Checked evidence.')
        with patch.object(runner, '_conn', return_value=connection), \
                patch('coms.runner.workspace_path', return_value=self.tmp.name):
            runner._reply_team(message['id'], 'worker')
        self.assertEqual(connection.prompt.call_args.args[0].count('UNIQUE EVIDENCE INSTRUCTIONS'), 1)
        self.assertEqual(self.conn.execute('SELECT status FROM team_deliveries').fetchone()[0], 'answered')
        message = team.send(self.conn, job['id'], 'orchestrator', 'worker', 'Unrelated follow-up')
        with patch.object(runner, '_conn', return_value=connection), \
                patch('coms.runner.workspace_path', return_value=self.tmp.name):
            runner._reply_team(message['id'], 'worker')
        self.assertNotIn('UNIQUE EVIDENCE INSTRUCTIONS', connection.prompt.call_args.args[0])

    def test_import_and_file_edits_reject_invalid_trigger_metadata_atomically(self):
        import base64
        invalid = '---\ntrigger_words: [12]\n---\nInstructions.'
        with self.assertRaises(api.BadRequest):
            api.write_skill_file(self.conn, 'evidence', 'SKILL.md', invalid, 'admin', self.skill['version'])
        self.assertEqual(api.get_skill(self.conn, 'evidence')['body'], self.skill['body'])
        with self.assertRaises(api.BadRequest):
            api.import_skill(self.conn, 'invalid', [{'path': 'SKILL.md', 'content_base64': base64.b64encode(invalid.encode()).decode()}], 'admin')
        self.assertFalse((skill_store.SKILLS_ROOT / 'invalid').exists())


if __name__ == '__main__':
    unittest.main()
