"""Regression cases for source evidence and streamed final-review decisions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, team
from coms.runner import Runner


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'COMS_DB': str(self.root / 'board.db')})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.job = api.create_ticket(self.conn, 'Travel', '', 'admin', type_='job',
                                     ready_for_execution=True)
        for slug in ('writer', 'peer'):
            api.upsert_agent(self.conn, slug, slug, role='expert')
        self.worker = api.create_ticket(self.conn, 'St. Tropez', 'Known for, food, sights',
                                        self.job['assignee'], parent_id=self.job['id'],
                                        assignee='writer', ready_for_execution=True,
                                        collaboration_required=True)
        api.create_ticket(self.conn, 'Peer work', '', self.job['assignee'],
                          parent_id=self.job['id'], assignee='peer', ready_for_execution=True)
        self.runner = Runner()

    def tearDown(self):
        self.runner.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def source(self, *, job_id=None, status='ok'):
        sid = self.conn.execute("INSERT INTO research_sources(job_id,reader,url,final_url,status,body,retrieved_at) VALUES(?,'writer','https://example.org/guide','',?,'Recorded evidence',?)",
                                (job_id or self.job['id'], status, api.now())).lastrowid
        self.conn.commit()
        return sid

    def review(self, body, sid, *, read_source=True):
        page = api.save_wiki(self.conn, 'travel-evidence', 'Evidence', body, 'writer')
        api.link_ticket(self.conn, self.worker['id'], 'wiki', page['slug'], 'writer')
        review = team.request_review(self.conn, self.job['id'], self.worker['id'],
                                     'writer', page['slug'], 'peer')
        self.conn.execute('INSERT INTO artifact_reads VALUES(?,?,?,?)',
                          ('peer', 100, page['slug'], page['updated_at']))
        if read_source:
            self.conn.execute('INSERT INTO source_reads VALUES(?,?,?)', ('peer', 100, sid))
        self.conn.commit()
        return review

    def test_source_id_citations_allow_review_and_delivery(self):
        sid = self.source()
        review = self.review(f'Findings [source {sid} – recorded guide].', sid)
        accepted = team.review(self.conn, review['id'], 'peer', 'accepted',
                               'Read the current page and cited source.', 100)
        self.assertEqual(accepted['status'], 'accepted')
        delivered = api.deliver_ticket(self.conn, self.worker['id'], 'writer',
                                       'Findings in [[travel-evidence]].')
        self.assertEqual(delivered['status'], 'review')

    def test_citation_matching_has_exact_ids_and_nonempty_urls(self):
        source = {'id': 45, 'url': 'https://example.org/guide', 'final_url': ''}
        for body in ('[source 45 – guide]', 'Source #45', 'SOURCE 45.',
                     'https://example.org/guide'):
            with self.subTest(body=body):
                self.assertTrue(team.source_is_cited(source, body))
        for body in ('45 attractions', 'source 450', 'source 45abc', 'resource 45',
                     'sources 450', 'Uncited text'):
            with self.subTest(body=body):
                self.assertFalse(team.source_is_cited(source, body))
        self.assertFalse(team.source_is_cited({'id': 45, 'url': '', 'final_url': ''}, 'Uncited'))

    def test_id_citations_preserve_successful_read_and_job_guards(self):
        sid = self.source()
        review = self.review(f'Source #{sid}', sid, read_source=False)
        with self.assertRaisesRegex(api.BadRequest, 'source_get'):
            team.review(self.conn, review['id'], 'peer', 'accepted', 'Checked.', 100)
        for foreign, status in ((False, 'failed'), (True, 'ok')):
            other = api.create_ticket(self.conn, 'Other job', '', 'admin', type_='job')
            sid = self.source(job_id=other['id'] if foreign else None, status=status)
            self.conn.execute('UPDATE wiki_pages SET body=? WHERE slug=?',
                              (f'Source #{sid}', review['artifact_slug']))
            self.conn.execute('INSERT INTO source_reads VALUES(?,?,?)', ('peer', 100, sid))
            self.conn.commit()
            with self.subTest(foreign=foreign, status=status), self.assertRaisesRegex(api.BadRequest, 'source_get'):
                team.review(self.conn, review['id'], 'peer', 'accepted', 'Checked.', 100)

    def test_source_id_citations_appear_in_focused_reviewer_packet(self):
        sid = self.source()
        review = self.review(f'Source #{sid}', sid)
        fake = Mock(session_id='review')
        fake.prompt.side_effect = RuntimeError('Stop after capturing the packet')
        with patch('coms.runner.workspace_path', return_value=str(self.root)), \
                patch.object(self.runner, '_conn', return_value=fake):
            self.runner._reply_team(review['message_id'], 'peer')
        packet = fake.prompt.call_args.args[0].split('CITED SOURCES', 1)[1].split('Peer review', 1)[0]
        self.assertIn('"id": ' + str(sid), packet)
        self.assertIn('https://example.org/guide', packet)


class FinalReviewResponseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'COMS_DB': str(self.root / 'board.db')})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.runner = Runner()
        ticket = api.create_ticket(self.conn, 'Research', 'Publish evidence', 'admin')
        self.ticket = api.update_ticket(self.conn, ticket['id'], 'admin', status='review',
                                        result='Evidence published.')

    def tearDown(self):
        self.runner.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def run_review(self, updates):
        rid = self.conn.execute("INSERT OR REPLACE INTO ticket_reviews(ticket_id,status,ticket_version,created_at) VALUES(?,'queued',?,?)",
                                (self.ticket['id'], self.ticket['updated_at'], api.now())).lastrowid
        self.conn.commit()
        def connection(*args, **kwargs):
            fake = Mock(session_id='final-review')
            def stream(*args, **kw):
                for update in updates:
                    kwargs['on_notification']({'params': {'update': update}})
            fake.prompt.side_effect = stream
            return fake
        with patch('coms.runner.AcpConnection', side_effect=connection), \
                patch('coms.runner.workspace_path', return_value=str(self.root)), \
                patch.object(self.runner, 'settings', return_value={'active_backend': 'codex', 'codex_command': 'fake-acp'}), \
                patch.object(self.runner, '_apply_model'):
            self.runner._review_ticket(self.ticket, rid, None)
        return dict(self.conn.execute('SELECT * FROM ticket_reviews WHERE id=?', (rid,)).fetchone())

    @staticmethod
    def text(body, **kwargs):
        return {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': body}, **kwargs}

    def test_commentary_and_tool_reads_do_not_contaminate_streamed_final_json(self):
        decision = json.dumps({'decision': 'close', 'reason': 'Criteria met.'})
        review = self.run_review([self.text('Checking citations first.'),
                                 {'sessionUpdate': 'tool_call', 'toolCallId': 'read', 'title': 'wiki_get'},
                                 {'sessionUpdate': 'tool_call_update', 'toolCallId': 'read', 'status': 'completed'},
                                 self.text(decision[:20]), self.text(decision[20:])])
        self.assertEqual(review['status'], 'closed', review['reason'])
        self.assertEqual(api.get_ticket(self.conn, self.ticket['id'])['status'], 'done')
        bodies = [row[0] for row in self.conn.execute("SELECT body FROM agent_messages WHERE kind='review-decision' ORDER BY id")]
        self.assertEqual(bodies, ['Checking citations first.', decision])

    def test_explicit_message_boundary_uses_only_final_message(self):
        review = self.run_review([self.text('Inspecting evidence.', messageId='progress'),
                                 self.text('```json\n{"decision":"human_review",', messageId='final'),
                                 self.text('"reason":"Missing date."}\n```', messageId='final')])
        self.assertEqual(review['status'], 'human_review', review['reason'])
        self.assertEqual(api.get_ticket(self.conn, self.ticket['id'])['status'], 'review')

    def test_invalid_or_missing_final_decision_keeps_ticket_in_review(self):
        for updates in ([self.text('Accepted, looks fine.')],
                        [self.text('Checking. {"decision":"close"}')],
                        [self.text('{"decision":"unknown"}')],
                        [self.text('{"decision":"close"}'),
                         {'sessionUpdate': 'tool_call', 'toolCallId': 'read', 'title': 'wiki_get'}]):
            with self.subTest(updates=updates):
                review = self.run_review(updates)
                self.assertEqual(review['status'], 'failed')
                self.assertEqual(api.get_ticket(self.conn, self.ticket['id'])['status'], 'review')


if __name__ == '__main__':
    unittest.main()
