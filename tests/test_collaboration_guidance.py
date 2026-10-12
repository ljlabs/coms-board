"""Durable Q&A and prompt/context/priority regressions for peer responses."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, mcp, skill_store, team
from coms.runner import Runner


class CollaborationGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'work'
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {'COMS_DB': str(self.root / 'board.db')})
        self.env.start()
        self.skills = patch.object(skill_store, 'SKILLS_ROOT', self.root / 'skills')
        self.skills.start()
        self.policy = patch('coms.workspace.workspace_path',
                            side_effect=lambda settings, **kw: settings['workspace'])
        self.policy.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.conn.executemany('INSERT INTO runner_settings VALUES(?,?)', [
            ('workspace', str(self.workspace)), ('active_backend', 'codex')])
        self.conn.commit()
        self.runner = Runner()
        self.job = api.create_ticket(self.conn, 'Travel guide', 'Cite verified local insights.',
                                     'admin', type_='job', ready_for_execution=True,
                                     collaboration_required=True, workspace=str(self.workspace))
        self.lead = api.resolve_ticket_agent(self.conn, self.job)['slug']
        self.worker = team.delegate(self.conn, self.job['id'], self.lead, 'Margaux',
                                    'Local research', 'Research Monte Carlo',
                                    'Find useful insights with sources')['ticket']
        self.peer = team.delegate(self.conn, self.job['id'], self.lead, 'Ivy',
                                  'Documentation review', 'Research Nice',
                                  'Check current source evidence')['ticket']

    def tearDown(self):
        self.runner.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.skills.stop()
        self.policy.stop()
        self.env.stop()
        self.tmp.cleanup()

    def question(self):
        question = api.ask_question(self.conn, 'Which Monte Carlo sights deserve coverage?',
                                    'Ticket #%s needs sourced insights beyond the main attractions.'
                                    % self.worker['id'], self.worker['assignee'])
        api.link_ticket(self.conn, self.worker['id'], 'question', str(question['id']),
                        self.worker['assignee'])
        message = team.send(self.conn, self.job['id'], self.worker['assignee'],
                            self.peer['assignee'], 'Please answer question #%s: #/questions/%s'
                            % (question['id'], question['id']))
        return question, message

    def review_request(self, reviewer=None, slug='monte-carlo-guide'):
        source_file = self.workspace / 'notes.txt'
        source_file.write_text('Verified local travel evidence.\n', encoding='utf-8')
        source = team.read_file_source(self.conn, self.job['id'], self.worker['assignee'],
                                       'notes.txt', ticket_id=self.worker['id'])
        page = api.save_wiki(self.conn, slug, 'Guide', source['url'], self.worker['assignee'])
        review = team.request_review(self.conn, self.job['id'], self.worker['id'],
                                     self.worker['assignee'], page['slug'], reviewer or self.lead)
        return review, source

    def test_response_saves_board_answer_before_releasing_sender(self):
        question, message = self.question()
        with patch.dict(os.environ, {'COMS_AGENT': self.peer['assignee'],
                                     'COMS_TICKET_ID': str(self.peer['id']),
                                     'COMS_TEAM_MESSAGE_ID': str(message['id'])}):
            surface = mcp.dispatch({'id': 1, 'method': 'tools/list'}, collaboration=True)
            names = {t['name'] for t in surface['result']['tools']}
            self.assertTrue({'questions_get', 'questions_answer'} <= names)
            self.assertNotIn('questions_ask', names)
            mcp.call_tool('questions_get', {'id': question['id']}, collaboration=True)
            mcp.call_tool('questions_answer', {'id': question['id'],
                          'body': 'A sourced local insight; unverified opening hours need checking.'},
                          collaboration=True)
            self.assertTrue(team.pending(self.conn, self.worker['id'], self.worker['assignee']))
            mcp.call_tool('team_reply', {'message_id': message['id'],
                          'body': 'Saved my evidence and limitations on question #%s.' % question['id']},
                          collaboration=True)
        self.assertEqual(len(api.get_question(self.conn, question['id'])['answers']), 1)
        self.assertFalse(team.pending(self.conn, self.worker['id'], self.worker['assignee']))

    def test_response_cannot_answer_an_unlinked_or_foreign_question(self):
        _, message = self.question()
        foreign_job = api.create_ticket(self.conn, 'Unrelated project', '', 'admin', type_='job')
        foreign = api.ask_question(self.conn, 'Other project question', '', 'admin')
        api.link_ticket(self.conn, foreign_job['id'], 'question', str(foreign['id']), 'admin')
        unlinked = api.ask_question(self.conn, 'Unlinked question', '', 'admin')
        with patch.dict(os.environ, {'COMS_AGENT': self.peer['assignee'],
                                     'COMS_TICKET_ID': str(self.peer['id']),
                                     'COMS_TEAM_MESSAGE_ID': str(message['id'])}):
            for question in (foreign, unlinked):
                with self.subTest(question=question['id']), self.assertRaisesRegex(ValueError, 'linked'):
                    mcp.call_tool('questions_answer', {'id': question['id'], 'body': 'Out of scope'},
                                  collaboration=True)
                self.assertEqual(api.get_question(self.conn, question['id'])['answers'], [])

    def test_response_without_assigned_message_cannot_write_board_answers(self):
        question, _ = self.question()
        with patch.dict(os.environ, {'COMS_AGENT': self.peer['assignee'], 'COMS_TEAM_MESSAGE_ID': ''}):
            with self.assertRaisesRegex(ValueError, 'response turn'):
                mcp.call_tool('questions_answer', {'id': question['id'], 'body': 'Unassigned answer'},
                              collaboration=True)

    def test_response_cannot_write_answers_after_team_is_stopped(self):
        question, message = self.question()
        team.stop(self.conn, self.job['id'], 'admin')
        with patch.dict(os.environ, {'COMS_AGENT': self.peer['assignee'],
                                     'COMS_TEAM_MESSAGE_ID': str(message['id'])}):
            with self.assertRaises(api.BadRequest):
                mcp.call_tool('questions_answer', {'id': question['id'], 'body': 'Late answer'},
                              collaboration=True)
        self.assertEqual(api.get_question(self.conn, question['id'])['answers'], [])

    def test_question_response_prompt_explains_durable_answer_order(self):
        _, message = self.question()
        fake = Mock(session_id='reply')
        fake.prompt.side_effect = lambda *a, **kw: team.reply(
            self.conn, message['id'], self.peer['assignee'], 'See the saved board answer.')
        with patch('coms.runner.workspace_path', return_value=str(self.workspace)), \
                patch.object(self.runner, '_conn', return_value=fake):
            self.runner._reply_team(message['id'], self.peer['assignee'])
        prompt = fake.prompt.call_args.args[0]
        self.assertIn('questions_answer', prompt)
        self.assertIn('questions_get', prompt)
        self.assertIn('ticket_link', prompt)
        self.assertIn('before team_reply', prompt)

    def test_worker_and_orchestrator_share_question_guidance(self):
        standalone = api.create_ticket(self.conn, 'Standalone task', '', 'admin',
                                         assignee=self.peer['assignee'], ready_for_execution=True,
                                         workspace=str(self.workspace))
        worker_connection = Mock(session_id='worker')
        worker_connection.prompt.side_effect = lambda *a, **kw: api.update_ticket(
            self.conn, standalone['id'], self.peer['assignee'], result='Verified result')
        chat_connection = Mock(session_id='chat')
        chat_connection.supports.return_value = False
        chat_connection.session_new.return_value = {'sessionId': 'chat'}
        with patch('coms.runner.workspace_path', return_value=str(self.workspace)), \
                patch.object(self.runner, '_conn', return_value=worker_connection):
            self.runner._work(standalone)
        session = self.runner.create_orchestrator_session()
        with patch.object(self.runner, '_orchestrator_connection', return_value=chat_connection), \
                patch.object(self.runner, '_orchestrator_workspace', return_value=str(self.workspace)):
            self.runner._orchestrator_turn('Help with local travel questions.', session['id'], session)
        for prompt in (worker_connection.prompt.call_args.args[0], chat_connection.prompt.call_args.args[0]):
            self.assertIn('questions_ask', prompt)
            self.assertIn('before team_send', prompt)
            self.assertIn('questions_answer', prompt)
            self.assertIn('ticket_link', prompt)

    def test_reviews_take_priority_and_one_reserved_slot_with_full_workers(self):
        self.question()  # Older ordinary question must not take the review slot.
        first, _ = self.review_request()
        self.review_request(self.peer['assignee'], 'second-guide')
        self.runner.active['worker-slot'] = True
        self.runner.orchestrator_busy = True
        with patch('coms.runner.threading.Thread') as thread:
            self.runner._schedule_team_replies(self.conn, 1)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs['args'][2:], (first['message_id'], self.lead))
        self.assertEqual(self.runner._active_slots(), 1)
        self.assertEqual(len(self.runner.peer_review_keys), 1)
        with patch('coms.runner.threading.Thread') as thread:
            self.runner._schedule_team_replies(self.conn, 1)
        thread.assert_not_called()

    def test_specialist_reviews_keep_busy_recipient_guard(self):
        self.review_request(self.peer['assignee'])
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,agent_slug,stage) "
                          "VALUES(?,'codex','running',?,'execute')", (self.peer['id'], self.peer['assignee']))
        self.conn.commit()
        with patch('coms.runner.threading.Thread') as thread:
            self.runner._schedule_team_replies(self.conn, 1)
        thread.assert_not_called()

    def test_reserved_slot_is_released_if_thread_start_fails(self):
        self.review_request()
        with patch('coms.runner.threading.Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('Cannot start thread')
            with self.assertRaisesRegex(RuntimeError, 'Cannot start thread'):
                self.runner._schedule_team_replies(self.conn, 1)
        self.assertFalse(self.runner.active)
        self.assertFalse(self.runner.peer_review_keys)

    def test_busy_orchestrator_does_not_receive_parallel_ordinary_question_turns(self):
        team.send(self.conn, self.job['id'], self.worker['assignee'], self.lead, 'A legacy question')
        self.runner.orchestrator_busy = True
        with patch('coms.runner.threading.Thread') as thread:
            self.runner._schedule_team_replies(self.conn, 1)
        thread.assert_not_called()

    def test_existing_collaboration_run_blocks_another_orchestrator_review(self):
        review, _ = self.review_request()
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,agent_slug,stage) "
                          "VALUES(?,'codex','running',?,'collaborate')", (self.job['id'], self.lead))
        self.conn.commit()
        with patch('coms.runner.threading.Thread') as thread, \
                patch.object(self.runner, '_conn') as connect:
            self.runner._schedule_team_replies(self.conn, 1)
            self.runner._reply_team(review['message_id'], self.lead)
        thread.assert_not_called()
        connect.assert_not_called()

    def test_orchestrator_review_has_role_and_assignment_context_and_can_run_while_busy(self):
        review, source = self.review_request()
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,agent_slug,stage) "
                          "VALUES(?,'codex','running',?,'execute')", (self.job['id'], self.lead))
        self.conn.commit()
        fake = Mock(session_id='isolated-review')
        def decide(*args, **kwargs):
            rid = self.conn.execute('SELECT max(id) FROM agent_runs').fetchone()[0]
            with patch.dict(os.environ, {'COMS_AGENT': self.lead,
                                         'COMS_TICKET_ID': str(self.job['id']),
                                         'COMS_RUN_ID': str(rid),
                                         'COMS_TEAM_MESSAGE_ID': str(review['message_id'])}):
                mcp.call_tool('wiki_get', {'slug': review['artifact_slug']}, collaboration=True)
                mcp.call_tool('source_get', {'source_id': source['id']}, collaboration=True)
                mcp.call_tool('team_review', {'review_id': review['id'], 'decision': 'accepted',
                              'body': 'Verified the cited snapshot and assignment scope.'}, collaboration=True)
        fake.prompt.side_effect = decide
        self.runner.orchestrator_busy = True
        with patch('coms.runner.workspace_path', return_value=str(self.workspace)), \
                patch.object(self.runner, '_conn', return_value=fake) as connect:
            self.runner._reply_team(review['message_id'], self.lead)
        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs['collaboration_message_id'], review['message_id'])
        prompt = fake.prompt.call_args.args[0]
        self.assertIn('main orchestrator', prompt)
        self.assertIn(self.worker['title'], prompt)
        self.assertIn(self.worker['body'], prompt)
        self.assertIn('TEAM CONVERSATION', prompt)
        self.assertEqual(self.conn.execute('SELECT status FROM peer_reviews WHERE id=?',
                                          (review['id'],)).fetchone()[0], 'accepted')
        self.assertEqual(fake.prompt.call_args.kwargs['total_timeout'], 180)
        fake.close.assert_called_once()

    def test_failed_review_attempt_retains_bounded_retry_and_closes_session(self):
        review, _ = self.review_request()
        fake = Mock(session_id='failed-review')
        fake.prompt.side_effect = RuntimeError('hard timeout waiting for session/prompt')
        with patch('coms.runner.workspace_path', return_value=str(self.workspace)), \
                patch.object(self.runner, '_conn', return_value=fake):
            self.runner._reply_team(review['message_id'], self.lead)
        delivery = self.conn.execute('SELECT status,attempt_count FROM team_deliveries WHERE message_id=?',
                                      (review['message_id'],)).fetchone()
        self.assertEqual(tuple(delivery), ('pending', 1))
        self.assertEqual(self.conn.execute('SELECT status FROM peer_reviews WHERE id=?',
                                          (review['id'],)).fetchone()[0], 'failed')
        fake.close.assert_called_once()
        self.assertFalse(self.runner.run_conns)

    def test_reserved_slot_is_released_when_worker_finishes_or_raises(self):
        for failing in (False, True):
            key = 'team:' + self.lead
            self.runner.active[key] = True
            self.runner.peer_review_keys.add(key)
            callback = Mock(side_effect=RuntimeError('failure') if failing else None)
            with patch('coms.runner.logging.exception'):
                self.runner._worker(key, callback)
            # _worker releases the calling thread's handle, just like real response threads.
            self.conn = db.connect()
            self.assertNotIn(key, self.runner.active)
            self.assertFalse(self.runner.peer_review_keys)


if __name__ == '__main__':
    unittest.main()
