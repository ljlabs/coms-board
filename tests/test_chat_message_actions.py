"""Message actions preserve canonical history and isolate regenerated sessions."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coms import api, db, server
from coms.orchestrator_transcript import Transcript, begin_turn, history_before
from coms.runner import Runner
from coms.ticket_chat import conversation


class ChatMessageActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.env = patch.dict(os.environ, {'COMS_DB': str(Path(self.tmp.name) / 'board.db')})
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.runner = Runner()
        self.workspace = patch('coms.runner.workspace_path', return_value=self.tmp.name)
        self.workspace.start()
        self.thread = patch('coms.runner.threading.Thread')
        self.thread.start()
        self.source = self.runner.create_orchestrator_session('Original')
        self.sid = self.source['id']
        self.turn, self.user = begin_turn(self.sid, 'First prompt', 'codex', 'model', self.tmp.name)
        transcript = Transcript(self.sid, self.turn['id'])
        transcript.apply({'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'Thinking'}})
        transcript.apply({'sessionUpdate': 'plan', 'entries': [{'content': 'Inspect', 'status': 'completed'}]})
        transcript.apply({'sessionUpdate': 'future_event', 'details': 'diagnostic'})
        transcript.apply({'sessionUpdate': 'tool_call', 'toolCallId': 'tool', 'status': 'completed',
                          'rawInput': {'cmd': 'ls'}, 'rawOutput': {'stdout': 'retained result'}})
        transcript.apply({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'First answer'}})
        transcript.finish('end_turn')
        self.answer = self.conn.execute("SELECT id FROM orchestrator_messages WHERE session_id=? AND role='assistant' AND kind='text'", (self.sid,)).fetchone()[0]
        turn, self.later_user = begin_turn(self.sid, 'Later prompt', 'codex', 'model', self.tmp.name)
        Transcript(self.sid, turn['id']).finish(error='Backend disconnected; send again')
        self.error = self.conn.execute("SELECT max(id) FROM orchestrator_messages WHERE session_id=?", (self.sid,)).fetchone()[0]
        self.conn.execute("UPDATE orchestrator_sessions SET acp_session_id='stale-session',backend='codex',workspace=? WHERE id=?", (self.tmp.name, self.sid))
        self.conn.commit()

    def tearDown(self):
        self.thread.stop()
        self.workspace.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def rows(self, sid=None):
        return [dict(row) for row in self.conn.execute('SELECT * FROM orchestrator_messages WHERE session_id=? ORDER BY id', (sid or self.sid,))]

    def test_fork_copies_exact_prefix_and_remaps_turns_without_acp_identity(self):
        project = self.runner.create_orchestrator_project('Project')
        self.runner.update_orchestrator_session(self.sid, project['id'])
        original = self.rows()
        fork = self.runner.fork_orchestrator_session(self.sid, self.answer)
        self.assertEqual(fork['title'], 'Fork of: Original')
        self.assertEqual(fork['project_id'], project['id'])
        self.assertEqual(fork['acp_session_id'], '')
        self.assertEqual(fork['workspace'], self.tmp.name)
        copied = self.rows(fork['id'])
        prefix = [row for row in original if row['id'] <= self.answer]
        for field in ('role', 'kind', 'body', 'metadata_json', 'state', 'tool_call_id'):
            self.assertEqual([row[field] for row in copied], [row[field] for row in prefix])
        self.assertTrue(set(row['id'] for row in original).isdisjoint(row['id'] for row in copied))
        turn = self.runner.orchestrator_detail(fork['id'])['turns'][0]
        self.assertNotEqual(turn['id'], self.turn['id'])
        self.assertEqual(turn['user_message_id'], copied[0]['id'])
        self.assertTrue(all(row['turn_id'] == turn['id'] for row in copied))
        self.assertEqual(self.rows(), original)
        self.assertIn('retained result', history_before(fork['id'], copied[-1]['id'] + 1))

    def test_error_fork_keeps_status_and_can_continue_independently(self):
        fork = self.runner.fork_orchestrator_session(self.sid, self.error)
        detail = self.runner.orchestrator_detail(fork['id'])
        self.assertEqual(detail['messages'][-1]['kind'], 'error')
        self.assertEqual(detail['turns'][-1]['status'], 'failed')
        result = self.runner.orchestrate('Continue fork', fork['id'])
        self.assertTrue(result['accepted'])
        self.assertNotIn('Continue fork', [row['body'] for row in self.rows()])

    def test_fork_rejects_user_tool_thinking_plan_log_missing_and_wrong_chat(self):
        original = self.rows()
        for row in original:
            if row['kind'] == 'error' or (row['role'] == 'assistant' and row['kind'] == 'text'):
                continue
            with self.subTest(kind=row['kind']), self.assertRaises(ValueError):
                self.runner.fork_orchestrator_session(self.sid, row['id'])
        other = self.runner.create_orchestrator_session('Other')
        for sid, mid in [(other['id'], self.answer), (self.sid, 999999), ('missing', self.answer)]:
            with self.assertRaises(ValueError): self.runner.fork_orchestrator_session(sid, mid)
        self.assertEqual(len(self.runner.orchestrator_sessions()), 2)
        self.assertEqual(self.rows(), original)

    def test_fork_busy_source_rejected_but_other_chat_busy_allowed(self):
        self.runner.orchestrator_busy = True
        self.runner.orchestrator_session = self.sid
        with self.assertRaises(ValueError): self.runner.fork_orchestrator_session(self.sid, self.answer)
        self.runner.orchestrator_session = 'different'
        self.assertEqual(self.runner.fork_orchestrator_session(self.sid, self.answer)['title'], 'Fork of: Original')

    def test_fork_legacy_response_and_cut_inside_turn(self):
        self.conn.execute("INSERT INTO orchestrator_messages(session_id,role,body,created_at) VALUES(?,'assistant','Another answer','now')", (self.sid,))
        self.conn.commit()
        legacy = self.rows()[-1]
        fork = self.runner.fork_orchestrator_session(self.sid, legacy['id'])
        self.assertIsNone(self.rows(fork['id'])[-1]['turn_id'])
        # A response before a later tool/error is a settled boundary too.
        self.conn.execute("UPDATE orchestrator_messages SET turn_id=? WHERE id=?", (self.turn['id'], self.error))
        self.conn.commit()
        fork = self.runner.fork_orchestrator_session(self.sid, self.answer)
        self.assertEqual(self.runner.orchestrator_detail(fork['id'])['turns'][0]['status'], 'completed')

    def test_edit_reuses_user_id_truncates_suffix_and_starts_clean_turn(self):
        result = self.runner.edit_orchestrator_message(self.sid, self.user['id'], 'Corrected prompt')
        self.assertTrue(result['accepted'])
        self.assertEqual(result['message']['id'], self.user['id'])
        self.assertEqual([row['body'] for row in self.rows()], ['Corrected prompt'])
        self.assertNotEqual(result['turn_id'], self.turn['id'])
        self.assertGreater(result['message']['version'], self.user['version'])
        self.assertEqual(len(self.runner.orchestrator_detail(self.sid)['turns']), 1)
        self.assertEqual(self.conn.execute('SELECT acp_session_id FROM orchestrator_sessions WHERE id=?', (self.sid,)).fetchone()[0], '')

    def test_edit_later_message_preserves_prefix_and_history(self):
        original = self.rows()
        result = self.runner.edit_orchestrator_message(self.sid, self.later_user['id'], 'New direction')
        self.assertEqual(self.rows()[:-1], [row for row in original if row['id'] < self.later_user['id']])
        self.assertIn('First answer', history_before(self.sid, result['message']['id']))
        self.assertNotIn('Later prompt', history_before(self.sid, result['message']['id']))

    def test_invalid_busy_and_unavailable_workspace_edits_are_non_destructive(self):
        original = self.rows()
        other = self.runner.create_orchestrator_session('Other')
        for sid, mid, body in [(self.sid,self.user['id'],' '),(self.sid,self.user['id'],None),
                               (self.sid,self.answer,'x'),(other['id'],self.user['id'],'x'),(self.sid,999999,'x')]:
            with self.assertRaises(ValueError): self.runner.edit_orchestrator_message(sid, mid, body)
        self.runner.orchestrator_busy = True
        with self.assertRaises(ValueError): self.runner.edit_orchestrator_message(self.sid, self.user['id'], 'x')
        self.runner.orchestrator_busy = False
        with patch('coms.runner.workspace_path', side_effect=ValueError('Workspace missing')):
            with self.assertRaises(ValueError): self.runner.edit_orchestrator_message(self.sid, self.user['id'], 'x')
        self.assertEqual(self.rows(), original)

    def test_ticket_edit_regenerates_without_rerunning_execution_or_restoring_old_session(self):
        ticket = api.create_ticket(self.conn, 'Ticket', '', 'admin')
        tid = ticket['id']
        rid = self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,session_id,status,stage) VALUES(?,'codex','stale','completed','execute')", (tid,)).lastrowid
        ids = []
        for role, kind, body in [('assistant','acp-response','Execution'),('user','text','Old follow-up'),('assistant','acp-response','Old reply')]:
            ids.append(self.conn.execute('INSERT INTO agent_messages(ticket_id,role,kind,body,created_at) VALUES(?,?,?,?,?)', (tid,role,kind,body,api.now())).lastrowid)
        self.conn.commit()
        connection = Mock(alive=True)
        self.runner.conns[tid] = connection
        result = self.runner.edit_ticket_message(tid, ids[1], 'Corrected follow-up')
        self.assertTrue(result['accepted'])
        messages = conversation(self.conn, tid, self.runner)['messages']
        self.assertEqual([m['body'] for m in messages], ['Execution','Corrected follow-up'])
        self.assertEqual(messages[-1]['id'], ids[1])
        self.assertNotIn(tid, self.runner.conns)
        connection.close.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT session_id FROM agent_runs WHERE id=?',(rid,)).fetchone()[0], '')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM agent_runs WHERE ticket_id=?',(tid,)).fetchone()[0], 1)

    def test_full_public_history_is_available_for_fork_continuation(self):
        payload = 'z' * 70000
        self.conn.execute('UPDATE orchestrator_messages SET body=? WHERE id=?',(payload,self.user['id']))
        self.conn.execute("UPDATE orchestrator_messages SET metadata_json=? WHERE kind='tool_call' AND session_id=?", (json.dumps({'rawOutput':'y'*5000}),self.sid))
        self.conn.commit()
        fork = self.runner.fork_orchestrator_session(self.sid,self.answer)
        history = history_before(fork['id'],self.rows(fork['id'])[-1]['id']+1)
        self.assertIn(payload, history)
        self.assertIn('y'*5000, history)
        self.assertNotIn('diagnostic', history)

    def test_http_routes_expose_actions_and_reject_invalid_boundaries(self):
        with patch('coms.runner.RUNNER', self.runner):
            for route, body in [(f'/api/runner/sessions/{self.sid}/messages/{self.user["id"]}/fork',{}),
                                (f'/api/runner/sessions/{self.sid}/messages/{self.answer}',{'body':'bad'})]:
                pattern, handler = next((p,h) for p,h in server.ROUTES if re.fullmatch(p,route))
                with self.assertRaises(api.BadRequest): handler(self.conn,'POST' if route.endswith('/fork') else 'PATCH',re.fullmatch(pattern,route),{},body,'admin')
            route = f'/api/runner/sessions/{self.sid}/messages/{self.answer}/fork'
            pattern, handler = next((p,h) for p,h in server.ROUTES if re.fullmatch(p,route))
            self.assertEqual(handler(self.conn,'POST',re.fullmatch(pattern,route),{},{},'admin')['title'], 'Fork of: Original')

    def test_regenerated_or_forked_turn_uses_fresh_acp_with_retained_context(self):
        for action in ('edit', 'fork'):
            with self.subTest(action=action):
                if action == 'edit':
                    result = self.runner.edit_orchestrator_message(self.sid,self.later_user['id'],'Corrected follow-up')
                    sid = self.sid
                else:
                    fork = self.runner.fork_orchestrator_session(self.sid,self.answer)
                    sid = fork['id']
                    result = self.runner.orchestrate('Continue fork',sid)
                connection = Mock(alive=True,session_id='fresh-session',config_options=[])
                connection.supports.return_value = True
                connection.session_new.return_value = {'sessionId':'fresh-session'}
                connection.prompt.return_value = {'stopReason':'end_turn'}
                session = dict(self.conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone())
                with patch.object(self.runner,'_orchestrator_connection',return_value=connection):
                    self.runner._orchestrator_turn(result['message']['body'],sid,session,result['turn_id'])
                connection.session_load.assert_not_called()
                connection.session_new.assert_called_once()
                prompt = connection.prompt.call_args.args[0]
                self.assertIn('First prompt',prompt)
                self.assertIn('First answer',prompt)
                self.assertIn('retained result',prompt)
                self.assertNotIn('Later prompt',prompt)
                self.assertNotIn('Backend disconnected',prompt)

    def test_fork_and_edit_refuse_unfinished_persisted_turns(self):
        self.conn.execute("UPDATE orchestrator_turns SET status='running' WHERE id=?",(self.turn['id'],))
        self.conn.commit()
        original = self.rows()
        with self.assertRaises(ValueError): self.runner.fork_orchestrator_session(self.sid,self.answer)
        with self.assertRaises(ValueError): self.runner.edit_orchestrator_message(self.sid,self.user['id'],'New')
        self.assertEqual(self.rows(),original)

    def test_ticket_invalid_or_busy_edit_does_not_truncate_messages(self):
        ticket = api.create_ticket(self.conn,'Ticket','','admin')
        tid = ticket['id']
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,session_id,status,stage) VALUES(?,'codex','old','completed','execute')",(tid,))
        mid = self.conn.execute("INSERT INTO agent_messages(ticket_id,role,body,created_at) VALUES(?,'user','Original','now')",(tid,)).lastrowid
        self.conn.commit()
        for message_id, text in ((mid,''),(mid,None),(9999,'New')):
            with self.assertRaises(ValueError): self.runner.edit_ticket_message(tid,message_id,text)
        for key in (tid,'review:'+str(tid)):
            self.runner.active[key] = True
            with self.assertRaises(ValueError): self.runner.edit_ticket_message(tid,mid,'New')
            self.runner.active.clear()
        self.assertEqual(self.conn.execute('SELECT body FROM agent_messages WHERE id=?',(mid,)).fetchone()[0],'Original')
        self.assertEqual(self.conn.execute('SELECT session_id FROM agent_runs WHERE ticket_id=?',(tid,)).fetchone()[0],'old')

    def test_ticket_fresh_session_receives_full_retained_public_history(self):
        ticket = api.create_ticket(self.conn,'Ticket','','admin')
        tid = ticket['id']
        self.conn.execute("INSERT INTO agent_runs(ticket_id,backend,session_id,status,stage,agent_slug) VALUES(?,'codex','','completed','execute','orchestrator')",(tid,))
        for i in range(25):
            self.conn.execute("INSERT INTO agent_messages(ticket_id,role,kind,body,created_at) VALUES(?,'assistant','acp-response',?,'now')",(tid,'Old answer '+str(i)))
        self.conn.execute("INSERT INTO agent_messages(ticket_id,role,kind,body,created_at) VALUES(?,'tool','acp-tool_call',?,'now')",(tid,json.dumps({'rawOutput':'Retained tool evidence'})))
        self.conn.execute("INSERT INTO agent_messages(ticket_id,role,body,created_at) VALUES(?,'user','Current question','now')",(tid,))
        self.conn.commit()
        connection = Mock(alive=True,session_id='new',ticket_restored=False)
        with patch.object(self.runner,'_conn',return_value=connection):
            self.runner._prompt(None,tid,'Current question')
        prompt = connection.prompt.call_args.args[0]
        self.assertIn('Old answer 0',prompt)
        self.assertIn('Retained tool evidence',prompt)
        self.assertEqual(prompt.count('Current question'),1)

    def test_ticket_edit_route_and_no_ticket_fork_route(self):
        route = '/api/tickets/1/runner/messages/7'
        pattern, handler = next((p,h) for p,h in server.ROUTES if re.fullmatch(p,route))
        with patch('coms.server.api.get_ticket'),patch('coms.runner.RUNNER',self.runner),patch.object(self.runner,'edit_ticket_message',return_value={'accepted':True}) as edit:
            self.assertTrue(handler(self.conn,'PATCH',re.fullmatch(pattern,route),{},{'body':'Revised'},'admin')['accepted'])
            edit.assert_called_once_with(1,7,'Revised')
        self.assertFalse(any(re.fullmatch(p,route+'/fork') for p,_h in server.ROUTES))


if __name__ == '__main__':
    unittest.main()
