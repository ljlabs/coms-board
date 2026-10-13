"""Regression cases for team convergence and bounded collaboration."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, mcp, skill_store, team
from coms.acp import AcpConnection, AcpError
from coms.runner import Runner


class TeamConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'COMS_DB': str(self.root / 'board.db')})
        self.env.start()
        self.skills = patch.object(skill_store, 'SKILLS_ROOT', self.root / 'skills')
        self.skills.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.job = api.create_ticket(self.conn, 'Travel job', 'Three destinations', 'admin',
                                     type_='job', ready_for_execution=True)
        self.lead = api.resolve_ticket_agent(self.conn, self.job)['slug']
        for slug in ('writer', 'peer', 'alternate'):
            api.upsert_agent(self.conn, slug, slug, role='expert')
        self.worker = api.create_ticket(self.conn, 'Nice research', 'Cite primary evidence',
                                        self.lead, parent_id=self.job['id'], assignee='writer',
                                        ready_for_execution=True, collaboration_required=True)
        self.peer = api.create_ticket(self.conn, 'Monaco research', '', self.lead,
                                      parent_id=self.job['id'], assignee='peer', ready_for_execution=True)
        self.alternate = api.create_ticket(self.conn, 'Review evidence', '', self.lead,
                                           parent_id=self.job['id'], assignee='alternate', ready_for_execution=True)
        self.runner = Runner()

    def tearDown(self):
        self.runner.stop()
        conn = getattr(db._local, 'conn', None)
        if conn: conn.close()
        self.conn.close()
        db._local.__dict__.clear()
        self.skills.stop()
        self.env.stop()
        self.tmp.cleanup()

    def source(self, url='https://example.org/nice'):
        stamp = api.now()
        sid = self.conn.execute("INSERT INTO research_sources(job_id,reader,url,final_url,status,body,retrieved_at) VALUES(?,'writer',?,?,'ok','Verified source',?)",
                                (self.job['id'], url, url, stamp)).lastrowid
        self.conn.commit()
        return sid, url

    def page(self):
        sid, url = self.source()
        page = api.save_wiki(self.conn, 'nice-guide', 'Nice guide', 'Findings: ' + url, 'writer')
        api.link_ticket(self.conn, self.worker['id'], 'wiki', page['slug'], 'writer')
        return page, sid

    def review(self, reviewer=None):
        return team.request_review(self.conn, self.job['id'], self.worker['id'], 'writer',
                                   'nice-guide', reviewer or self.lead)

    def accept(self, review, sid):
        self.conn.execute('INSERT INTO artifact_reads VALUES(?,?,?,?)',
                          (review['reviewer'], 100, 'nice-guide', review['artifact_version']))
        self.conn.execute('INSERT INTO source_reads VALUES(?,?,?)', (review['reviewer'], 100, sid))
        self.conn.commit()
        return team.review(self.conn, review['id'], review['reviewer'], 'accepted', 'Evidence checked', 100)

    def question(self, linked=True):
        q = api.ask_question(self.conn, 'Which local dishes?', 'Evidence needed for Nice', 'writer')
        if linked: api.link_ticket(self.conn, self.worker['id'], 'question', str(q['id']), 'writer')
        return q

    def test_chat_cannot_staff_a_ready_collaborative_job(self):
        with patch.dict(os.environ, {'COMS_AGENT': self.lead}, clear=True):
            os.environ['COMS_DB'] = str(self.root / 'board.db')
            with self.assertRaisesRegex(ValueError, 'staff'):
                mcp.call_tool('ticket_create', {'title': 'Duplicate Nice', 'parent_id': self.job['id'],
                              'assignee': 'writer', 'ready': True})
            with self.assertRaisesRegex(ValueError, 'staff'):
                mcp.call_tool('team_delegate', {'job_id': self.job['id'], 'name': 'New writer',
                              'capabilities': 'Nice research', 'title': 'Another Nice', 'body': 'Research Nice'})

    def test_differently_titled_active_assignment_cannot_be_duplicated(self):
        with self.assertRaisesRegex(api.BadRequest, 'active ticket'):
            team.delegate(self.conn, self.job['id'], self.lead, 'writer', 'Nice research',
                          'Nice Researcher', 'Same destination')
        self.assertEqual(len(api.list_tickets(self.conn, parent_id=self.job['id'])), 3)

    def test_question_notification_requires_link_and_originating_ticket(self):
        q = self.question(linked=False)
        with self.assertRaisesRegex(api.BadRequest, 'linked'):
            team.send(self.conn, self.job['id'], 'writer', 'peer', 'Please contribute',
                      question_id=q['id'], ticket_id=self.worker['id'])
        api.link_ticket(self.conn, self.worker['id'], 'question', str(q['id']), 'writer')
        message = team.send(self.conn, self.job['id'], 'writer', 'peer', 'Please contribute',
                            question_id=q['id'], ticket_id=self.worker['id'])
        self.assertEqual(message['question_id'], q['id'])
        self.assertEqual(message['ticket_id'], self.worker['id'])
        self.assertEqual(team.send(self.conn, self.job['id'], 'writer', 'peer', 'Reminder',
                                  question_id=q['id'], ticket_id=self.worker['id'])['id'], message['id'])
        with self.assertRaisesRegex(api.BadRequest, 'own ticket'):
            team.send(self.conn, self.job['id'], 'writer', 'peer', 'Please contribute',
                      question_id=q['id'], ticket_id=self.peer['id'])

    def test_structured_reply_requires_saved_board_answer(self):
        q = self.question()
        message = team.send(self.conn, self.job['id'], 'writer', 'peer', 'Please contribute',
                            question_id=q['id'], ticket_id=self.worker['id'])
        with self.assertRaisesRegex(api.BadRequest, 'Questions'):
            team.reply(self.conn, message['id'], 'peer', 'Some dishes')
        api.answer_question(self.conn, q['id'], 'peer', 'Evidence-backed dishes')
        team.reply(self.conn, message['id'], 'peer', 'Saved answer')
        self.assertFalse(team.pending(self.conn, self.worker['id'], 'writer'))

    def test_mcp_questions_cannot_bypass_structured_reference(self):
        with patch.dict(os.environ, {'COMS_AGENT': 'writer', 'COMS_TICKET_ID': str(self.worker['id'])}):
            with self.assertRaisesRegex(ValueError, 'question_id'):
                mcp.call_tool('team_send', {'job_id': self.job['id'], 'to': 'peer', 'body': 'Research this for me'})

    def test_pending_request_on_another_ticket_does_not_hold_worker(self):
        q = self.question()
        self.conn.execute('UPDATE tickets SET assignee=? WHERE id=?', ('writer', self.peer['id']))
        self.conn.commit()
        api.link_ticket(self.conn, self.peer['id'], 'question', str(q['id']), 'writer')
        team.send(self.conn, self.job['id'], 'writer', 'alternate', 'Please answer',
                  question_id=q['id'], ticket_id=self.peer['id'])
        self.assertFalse(team.pending(self.conn, self.worker['id'], 'writer'))
        self.assertTrue(team.pending(self.conn, self.peer['id'], 'writer'))

    def test_pending_and_accepted_pages_are_frozen_for_execution_writes(self):
        page, sid = self.page()
        review = self.review()
        for accepted in (False, True):
            if accepted: self.accept(review, sid)
            with self.assertRaisesRegex(api.BadRequest, 'review|deliver'):
                api.save_wiki(self.conn, page['slug'], page['title'], page['body'] + '\nMore',
                              'writer', ticket_id=self.worker['id'])

    def test_same_identity_cannot_edit_another_active_ticket_artifact(self):
        page, _ = self.page()
        other = api.create_ticket(self.conn, 'Duplicate legacy scope', '', 'admin',
                                   parent_id=self.job['id'], assignee='writer', ready_for_execution=True)
        with self.assertRaisesRegex(api.BadRequest, 'ticket'):
            api.save_wiki(self.conn, page['slug'], page['title'], 'Overwrite', 'writer', ticket_id=other['id'])

    def test_manual_revision_cancels_obsolete_review_without_acp_retry(self):
        page, _ = self.page()
        review = self.review()
        api.save_wiki(self.conn, page['slug'], page['title'], page['body'] + '\nCorrection', 'admin')
        with patch('coms.runner.threading.Thread') as thread:
            self.runner._schedule_team_replies(self.conn, 2)
        thread.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM peer_reviews WHERE id=?', (review['id'],)).fetchone()[0], 'cancelled')
        self.assertFalse(team.pending(self.conn, self.worker['id'], 'writer'))

    def test_identical_wiki_write_does_not_invalidate_review(self):
        page, _ = self.page()
        review = self.review()
        same = api.save_wiki(self.conn, page['slug'], page['title'], page['body'], 'writer', ticket_id=self.worker['id'])
        self.assertEqual(same['updated_at'], review['artifact_version'])

    def test_held_job_review_does_not_freeze_a_resubmitted_assignment(self):
        page, sid = self.page()
        self.accept(self.review(), sid)
        team.stop(self.conn, self.job['id'], 'admin')
        next_job = api.create_ticket(self.conn, 'Resubmitted job', '', 'admin', type_='job',
                                    assignee=self.lead, ready_for_execution=True)
        child = api.create_ticket(self.conn, 'Nice again', '', 'admin', parent_id=next_job['id'],
                                  assignee='writer', ready_for_execution=True)
        updated = api.save_wiki(self.conn, page['slug'], page['title'], page['body'] + '\nNew evidence',
                                'writer', ticket_id=child['id'])
        self.assertNotEqual(updated['updated_at'], page['updated_at'])

    def test_rejected_delegation_releases_its_write_lock(self):
        with self.assertRaises(api.BadRequest):
            team.delegate(self.conn, self.job['id'], self.lead, self.lead, 'staffing', 'Self', 'Self')
        self.assertFalse(self.conn.in_transaction)

    def test_review_budget_survives_artifact_revisions(self):
        page, _ = self.page()
        for revision in range(6):
            review = self.review()
            self.conn.execute("UPDATE peer_reviews SET status='changes_requested' WHERE id=?", (review['id'],))
            self.conn.execute("UPDATE team_deliveries SET status='answered' WHERE message_id=?", (review['message_id'],))
            self.conn.commit()
            api.save_wiki(self.conn, page['slug'], page['title'], page['body'] + str(revision),
                          'writer', ticket_id=self.worker['id'])
        with self.assertRaisesRegex(api.BadRequest, 'budget'):
            self.review()

    def test_concurrent_equivalent_requests_create_one_review(self):
        self.page()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def request():
            connection = sqlite3.connect(self.root / 'board.db', timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                barrier.wait(timeout=5)
                results.append(team.request_review(connection, self.job['id'], self.worker['id'],
                                                    'writer', 'nice-guide', self.lead)['id'])
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        workers = [threading.Thread(target=request) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=12)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    def test_low_temporary_space_holds_the_team_before_dispatch(self):
        with patch('coms.runner.shutil.disk_usage', return_value=Mock(free=0)):
            self.runner._guard_teams()
        self.assertTrue(team.is_stopped(self.conn, self.worker['id']))
        self.assertIn('temporary space', api.get_ticket(self.conn, self.job['id'])['comments'][-1]['body'])

    def test_failed_equivalent_review_keeps_its_attempt_budget(self):
        self.page()
        review = self.review()
        self.conn.execute("UPDATE peer_reviews SET status='failed' WHERE id=?", (review['id'],))
        self.conn.execute("UPDATE team_deliveries SET status='pending',attempt_count=1 WHERE message_id=?", (review['message_id'],))
        self.conn.commit()
        self.assertEqual(self.review()['id'], review['id'])
        self.conn.execute("UPDATE team_deliveries SET status='failed',attempt_count=3 WHERE message_id=?", (review['message_id'],))
        self.conn.commit()
        with self.assertRaisesRegex(api.BadRequest, 'exhausted'):
            self.review()
        alternate = self.review('peer')
        self.conn.execute("UPDATE peer_reviews SET status='failed' WHERE id=?", (alternate['id'],))
        self.conn.execute("UPDATE team_deliveries SET status='failed',attempt_count=3 WHERE message_id=?", (alternate['message_id'],))
        self.conn.commit()
        with self.assertRaisesRegex(api.BadRequest, 'budget'):
            self.review('alternate')

    def test_worker_can_deliver_without_manufacturing_a_question(self):
        _, sid = self.page()
        review = self.review()
        self.accept(review, sid)
        self.assertEqual(self.review('peer')['id'], review['id'])
        team.validate_delivery(self.conn, self.worker['id'], 'writer')

    def test_empty_redirect_url_cannot_qualify_uncited_evidence(self):
        page, sid = self.page()
        self.conn.execute("UPDATE research_sources SET final_url='' WHERE id=?", (sid,))
        self.conn.commit()
        api.save_wiki(self.conn, page['slug'], page['title'], 'An unsupported claim', 'writer')
        review = self.review()
        with self.assertRaisesRegex(api.BadRequest, 'cited'):
            self.accept(review, sid)

    def test_lead_can_deliver_without_manufacturing_a_skill(self):
        for child in api.list_tickets(self.conn, parent_id=self.job['id']):
            api.update_ticket(self.conn, child['id'], 'admin', status='wontfix')
        _, url = self.source()
        page = api.save_wiki(self.conn, 'synthesis', 'Plan', url, self.lead)
        api.link_ticket(self.conn, self.job['id'], 'wiki', page['slug'], self.lead)
        review = team.request_review(self.conn, self.job['id'], self.job['id'], self.lead, page['slug'], 'peer')
        self.conn.execute("UPDATE peer_reviews SET status='accepted' WHERE id=?", (review['id'],))
        self.conn.commit()
        team.validate_delivery(self.conn, self.job['id'], self.lead)

    def test_no_progress_hold_survives_restart_and_chatter(self):
        moment = datetime.now(timezone.utc)
        team.check_progress(self.conn, at=moment)
        team.send(self.conn, self.job['id'], self.lead, '*', 'Still thinking', kind='note')
        held = team.check_progress(self.conn, at=moment + timedelta(seconds=1801))
        self.assertIn(self.job['id'], held)
        ticket = api.get_ticket(self.conn, self.job['id'])
        self.assertEqual((ticket['status'], ticket['ready_for_execution']), ('blocked', 0))
        self.conn.close()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.assertTrue(team.is_stopped(self.conn, self.worker['id']))
        self.assertEqual(team.check_progress(self.conn, at=moment + timedelta(hours=3)), [])

    def test_new_evidence_advances_progress_but_repeated_fetch_does_not(self):
        moment = datetime.now(timezone.utc)
        team.check_progress(self.conn, at=moment)
        self.source()
        team.check_progress(self.conn, at=moment + timedelta(seconds=1700))
        self.source()  # Identical URL/content cannot buy another waiting period.
        self.assertEqual(team.check_progress(self.conn, at=moment + timedelta(seconds=3400)), [])
        self.assertIn(self.job['id'], team.check_progress(self.conn, at=moment + timedelta(seconds=3501)))

    def test_absolute_lifetime_is_not_reset_by_evidence(self):
        moment = datetime.now(timezone.utc)
        self.conn.execute('UPDATE tickets SET created_at=? WHERE id=?', ((moment - timedelta(seconds=3601)).isoformat(), self.job['id']))
        self.conn.commit()
        self.source()
        self.assertIn(self.job['id'], team.check_progress(self.conn, at=moment))

    def test_draft_dependency_is_held_with_visible_reason(self):
        api.create_ticket(self.conn, 'Unassigned synthesis draft', '', 'admin', parent_id=self.job['id'])
        with patch.object(self.runner, '_conn') as connect:
            self.runner._work(api.get_ticket(self.conn, self.job['id']))
        connect.assert_not_called()
        job = api.get_ticket(self.conn, self.job['id'])
        self.assertEqual(job['ready_for_execution'], 0)
        self.assertIn('dependency', job['comments'][-1]['body'])

    def test_review_packet_does_not_include_unrelated_artifacts(self):
        self.page()
        review = self.review()
        api.save_wiki(self.conn, 'unrelated', 'Huge unrelated page', 'UNRELATED_CONTENT' * 5000, 'peer')
        fake = Mock(session_id='review')
        fake.prompt.side_effect = RuntimeError('stop after recording prompt')
        with patch('coms.runner.workspace_path', return_value=str(self.root)), patch.object(self.runner, '_conn', return_value=fake):
            self.runner._reply_team(review['message_id'], self.lead)
        prompt = fake.prompt.call_args.args[0]
        self.assertIn('CITED SOURCES', prompt)
        self.assertNotIn('UNRELATED_CONTENT', prompt)
        self.assertLess(len(prompt), 16000)

    def test_legacy_schema_migrates_question_links_without_losing_messages(self):
        path = self.root / 'old.db'
        old = sqlite3.connect(path)
        old.executescript(db.SCHEMA)
        old.execute('DROP TABLE team_progress')
        old.execute("INSERT INTO team_messages(job_id,sender,recipient,kind,body,created_at) VALUES(1,'writer','peer','question','legacy','now')")
        old.commit()
        old.close()
        migrated = db.connect(path)
        item = migrated.execute('SELECT body,ticket_id,question_id FROM team_messages').fetchone()
        self.assertEqual(tuple(item), ('legacy', None, None))
        self.assertTrue(migrated.execute("SELECT name FROM sqlite_master WHERE name='team_progress'").fetchone())


class AcpTemporarySpaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.tmp.name)
        self.connection = AcpConnection('fake-acp', str(self.root), on_notification=Mock(),
                                        on_request=Mock(), on_close=Mock(),
                                        env={'COMS_ACP_TEMP_ROOT': str(self.root)})

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def test_native_files_are_isolated_and_removed_after_process_exit(self):
        process = Mock()
        state = {'code': None}
        process.poll.side_effect = lambda: state['code']
        process.wait.side_effect = lambda **kw: state.update(code=0)
        neighbor = self.root / 'unrelated.txt'
        neighbor.write_text('Keep this')
        with patch('coms.acp.subprocess.Popen', return_value=process) as spawn, \
                patch('coms.acp.threading.Thread'), patch.object(self.connection, 'request', return_value={}):
            self.connection.start()
        owned = Path(spawn.call_args.kwargs['env']['TMPDIR'])
        self.assertEqual(owned.parent, self.root)
        self.assertNotEqual(owned, self.root)
        (owned / 'native.so').write_bytes(b'Native extraction')
        self.connection.close()
        self.assertFalse(owned.exists())
        self.assertTrue(neighbor.exists())

    def test_low_space_prevents_subprocess_start(self):
        with patch('coms.acp.shutil.disk_usage', return_value=Mock(free=0)), \
                patch('coms.acp.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(AcpError, 'temporary space'):
                self.connection.start()
        spawn.assert_not_called()

    def test_spawn_failure_cleans_the_private_directory(self):
        with patch('coms.acp.subprocess.Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError): self.connection.start()
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
