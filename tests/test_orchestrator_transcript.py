"""Conversation records stay ordered and survive ACP updates and restoration."""
import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import *  # noqa: F401,F403
from coms import db
from coms.orchestrator_transcript import Transcript, begin_turn, history_before, message_dict, recover_turns


class TestTranscript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'chat.db')})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.conn.execute("INSERT INTO orchestrator_sessions(id,created_at,updated_at) VALUES('chat','now','now')")
        self.conn.commit()
        self.turn, self.user = begin_turn('chat', 'List the workspace.', 'codex', 'model', '/workspace')
        self.transcript = Transcript('chat', self.turn['id'])

    def tearDown(self):
        conn = getattr(db._local, 'conn', None)
        if conn: conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def rows(self):
        return [message_dict(r) for r in self.conn.execute("SELECT * FROM orchestrator_messages WHERE kind!='log' ORDER BY id")]

    def text(self, text, message_id=None):
        event = {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}
        if message_id is not None: event['messageId'] = message_id
        self.transcript.apply(event)

    def tool(self, ident='ls', **fields):
        self.transcript.apply({'sessionUpdate': 'tool_call', 'toolCallId': ident, 'title': 'List workspace', **fields})

    def update(self, ident='ls', **fields):
        self.transcript.apply({'sessionUpdate': 'tool_call_update', 'toolCallId': ident, **fields})

    def test_commentary_tool_final_answer_are_separate_persisted_entries(self):
        self.text('I’ll list ', 'one')
        self.text('the workspace.', 'one')
        self.tool(rawInput={'command': 'ls', 'cwd': '/workspace'})
        self.update(status='in_progress')
        self.update(status='completed', rawOutput={'stdout': 'src\n', 'exitCode': 0})
        self.text('Found ', 'two')
        self.text('src.', 'two')
        self.transcript.finish('end_turn')
        rows = self.rows()
        self.assertEqual([r['role'] for r in rows], ['user', 'assistant', 'tool', 'assistant'])
        self.assertEqual([r['body'] for r in rows if r['role']=='assistant'], ['I’ll list the workspace.', 'Found src.'])
        self.assertEqual(rows[2]['data']['rawOutput']['stdout'], 'src\n')
        self.assertEqual(rows[2]['state'], 'completed')
        self.assertEqual(self.conn.execute('SELECT status FROM orchestrator_turns').fetchone()[0], 'completed')
        self.assertTrue(all(r['turn_id']==self.turn['id'] for r in rows))

    def test_missing_or_reused_message_id_splits_only_at_new_tools(self):
        for message_id in [None, 'same']:
            self.text('before', message_id)
            self.tool(str(message_id))
            self.text('after', message_id)
            self.update(str(message_id), status='completed')
            self.text(' more', message_id)
        self.assertEqual([r['body'] for r in self.rows() if r['role']=='assistant'], ['before', 'after more', 'before', 'after more'])

    def test_message_ids_split_without_tools_and_echoes_do_not_duplicate_user(self):
        self.transcript.apply({'sessionUpdate':'user_message_chunk','messageId':'echo','content':{'type':'text','text':'List the workspace.'}})
        self.text('first', 'a')
        self.text('second', 'b')
        self.assertEqual([r['body'] for r in self.rows()], ['List the workspace.', 'first', 'second'])

    def test_sparse_updates_preserve_input_replace_output_and_do_not_move_cards(self):
        self.update('b', rawOutput=False)
        self.tool('a', rawInput=['ls'], content=[{'type':'content','content':{'type':'text','text':'old'}}])
        self.tool('a', status='in_progress')
        ids = [r['id'] for r in self.rows()]
        self.update('a', status='completed', rawInput=None, content=[], rawOutput=0)
        self.update('b', status='failed', rawOutput='failed')
        self.assertEqual([r['id'] for r in self.rows()], ids)
        tools = [r for r in self.rows() if r['role']=='tool']
        self.assertEqual([r['tool_call_id'] for r in tools], ['b', 'a'])
        self.assertEqual(tools[1]['data']['rawInput'], ['ls'])
        self.assertEqual(tools[1]['data']['content'], [])
        self.assertEqual(tools[1]['data']['rawOutput'], 0)
        self.assertGreater(tools[1]['version'], 1)

    def test_private_thoughts_and_unknown_events_are_diagnostics(self):
        self.transcript.apply({'sessionUpdate':'agent_thought_chunk','content':{'type':'text','text':'private'}})
        self.transcript.apply({'sessionUpdate':'future_variant','payload':42})
        self.transcript.apply({'sessionUpdate':'tool_call_update'})
        self.transcript.apply({'sessionUpdate':'agent_message_chunk','content':None})
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM orchestrator_messages WHERE kind='log'").fetchone()[0], 3)

    def test_permission_metadata_merges_with_the_reported_tool(self):
        self.transcript.permission({'toolCall':{'toolCallId':'ls','title':'Run ls','rawInput':{'command':'ls'}}}, 'allow-once')
        self.tool(status='completed')
        tools = [r for r in self.rows() if r['role']=='tool']
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['data']['permission'], 'allow-once')

    def test_public_content_blocks_keep_their_order_between_text_chunks(self):
        self.text('Before ', 'mixed')
        self.transcript.apply({'sessionUpdate':'agent_message_chunk','messageId':'mixed','content':{'type':'image','mimeType':'image/png','data':'AAAA'}})
        self.text('after.', 'mixed')
        assistant=self.rows()[1]
        self.assertEqual(assistant['body'],'Before after.')
        self.assertEqual([b['type'] for b in assistant['data']['blocks']],['text','image','text'])
        self.assertEqual(assistant['data']['blocks'][0]['text'],'Before ')
        self.assertEqual(assistant['data']['blocks'][2]['text'],'after.')

    def test_real_codex_adapter_trace_preserves_the_tool_and_both_messages(self):
        fixture=Path(__file__).with_name('fixtures')/'codex_acp_workspace.json'
        for event in json.loads(fixture.read_text(encoding='utf-8'))['events']:
            self.transcript.apply(event)
        self.transcript.finish('end_turn')
        rows=self.rows()
        self.assertEqual([r['role'] for r in rows],['user','assistant','tool','assistant'])
        self.assertEqual(rows[2]['state'],'completed')
        self.assertEqual(rows[2]['data']['rawOutput']['exit_code'],0)
        self.assertIn('README.txt',rows[2]['data']['rawOutput']['formatted_output'])
        self.assertIn('README.txt',rows[3]['body'])

    def test_error_preserves_partial_output_and_interrupts_unfinished_tools(self):
        self.text('partial')
        self.tool()
        self.transcript.finish(error='process exited')
        self.transcript.finish(error='process exited')
        self.assertEqual([r['kind'] for r in self.rows()], ['text', 'text', 'tool_call', 'error'])
        self.assertEqual(self.rows()[2]['state'], 'interrupted')
        self.assertIn('process exited', self.rows()[-1]['body'])

    def test_stop_reasons_and_empty_responses_are_visible(self):
        self.transcript.finish('max_tokens')
        self.assertEqual(self.conn.execute('SELECT status FROM orchestrator_turns').fetchone()[0], 'limited')
        self.assertEqual(self.rows()[-1]['kind'], 'error')
        turn, _ = begin_turn('chat', 'Again', 'codex', 'model', '/workspace')
        Transcript('chat', turn['id']).finish('end_turn')
        self.assertIn('without public output', self.rows()[-1]['body'])

    def test_history_excludes_current_turn_and_contains_tool_results_once(self):
        self.text('before')
        self.tool(rawInput={'command':'ls'})
        self.update(status='completed', rawOutput='src')
        self.text('after')
        self.transcript.finish('end_turn')
        _, user = begin_turn('chat', 'Current prompt', 'codex', 'model', '/workspace')
        history = history_before('chat', user['id'])
        self.assertIn('before', history)
        self.assertIn('after', history)
        self.assertIn('"rawOutput": "src"', history)
        self.assertEqual(history.count('List the workspace.'), 1)
        self.assertNotIn('Current prompt', history)

    def test_startup_recovery_preserves_tool_and_marks_turn_interrupted(self):
        self.tool()
        recover_turns(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM orchestrator_turns').fetchone()[0], 'interrupted')
        self.assertEqual(self.rows()[1]['state'], 'interrupted')
        self.assertEqual(self.rows()[-1]['kind'], 'error')

    def test_legacy_migration_is_repeatable_and_keeps_old_text(self):
        legacy = sqlite3.connect(str(Path(self.tmp.name) / 'legacy.db'))
        legacy.executescript(db.SCHEMA)
        legacy.execute("INSERT INTO orchestrator_messages(role,body,created_at) VALUES('assistant','old combined text','old')")
        db._migrate(legacy)
        db._migrate(legacy)
        row = legacy.execute('SELECT body,turn_id,version FROM orchestrator_messages').fetchone()
        self.assertEqual(row, ('old combined text', None, 0))
        legacy.close()
