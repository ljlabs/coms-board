"""HTTP API tests: real ThreadingHTTPServer on an ephemeral loopback port."""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests import *  # noqa: F401,F403
from coms import db
from coms.server import Handler


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        import os
        os.environ["COMS_DB"] = str(Path(cls._tmp.name) / "srv.db")
        db._local.__dict__.clear()
        db.connect()  # schema
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        import os
        os.environ.pop("COMS_DB", None)
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._local.__dict__.clear()
        cls._tmp.cleanup()

    def req(self, method, path, body=None, actor="admin"):
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-Coms-Agent": actor})
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read())

    def test_hierarchy_over_http(self):
        s = self.req("POST", "/api/tickets", {"title": "story", "type": "story"})
        j = self.req("POST", "/api/tickets", {"title": "job", "type": "job", "parent_id": s["id"]})
        t = self.req("POST", "/api/tickets", {"title": "task", "parent_id": j["id"]})

        got = self.req("GET", f"/api/tickets/{j['id']}")
        self.assertEqual(got["parent"]["id"], s["id"])
        self.assertEqual([c["id"] for c in got["children"]], [t["id"]])

        tree = self.req("GET", f"/api/tickets/{t['id']}/tree")
        self.assertEqual(tree["root"]["id"], s["id"])

        kids = self.req("GET", f"/api/tickets?parent={j['id']}")
        self.assertEqual([k["id"] for k in kids], [t["id"]])

        ov = self.req("GET", f"/api/overview?ticket={j['id']}")
        self.assertEqual(ov["tickets_total"], 3)

        closed = self.req("PATCH", f"/api/tickets/{j['id']}", {"status": "done"})
        self.assertIn("open_children_warning", closed)

    def test_failed_ticket_can_be_retried_over_http(self):
        from coms import api
        conn = db.connect()
        ticket = api.create_ticket(conn, 'Retry over HTTP', '', 'admin', assignee='agent-a', ready_for_execution=True)
        conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,finished_at,error,agent_slug,stage) VALUES(?,'opencode','failed',?,'Agent did not deliver','agent-a','execute')", (ticket['id'], api.now()))
        conn.commit()
        retried = self.req('POST', f"/api/tickets/{ticket['id']}/retry", {})
        self.assertEqual(retried['retry_kind'], 'agent run')
        state = self.req('GET', f"/api/tickets/{ticket['id']}/runner")['retry']
        self.assertFalse(state['eligible'])
        self.assertIn('queued', state['reason'])
        self.assertEqual(state['kind'], 'agent run')

    def test_links_over_http(self):
        t = self.req("POST", "/api/tickets", {"title": "linked work"})
        self.req("POST", "/api/wiki", {"title": "Note Page", "body": "b"})
        t2 = self.req("POST", f"/api/tickets/{t['id']}/links",
                      {"kind": "wiki", "ref": "note-page", "note": "findings"})
        self.assertEqual([(l["kind"], l["ref"]) for l in t2["links"]], [("wiki", "note-page")])
        self.assertEqual(t2["links"][0]["title"], "Note Page")

        links = self.req("GET", f"/api/tickets/{t['id']}/links")
        self.assertEqual(len(links), 1)

        t3 = self.req("DELETE", f"/api/tickets/{t['id']}/links",
                      {"kind": "wiki", "ref": "note-page"})
        self.assertEqual(t3["links"], [])

        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req("POST", f"/api/tickets/{t['id']}/links", {"kind": "wiki", "ref": "nope"})
        self.assertEqual(cm.exception.code, 400)

    def test_cycle_rejected_over_http(self):
        a = self.req("POST", "/api/tickets", {"title": "a"})
        b = self.req("POST", "/api/tickets", {"title": "b", "parent_id": a["id"]})
        try:
            self.req("PATCH", f"/api/tickets/{a['id']}", {"parent_id": b["id"]})
            self.fail("cycle accepted")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_questions_over_http(self):
        q = self.req("POST", "/api/questions", {"title": "how does voting work?", "body": "b"},
                     actor="expert-a")
        got = self.req("GET", f"/api/questions/{q['id']}")
        self.assertEqual(got["title"], "how does voting work?")
        a = self.req("POST", f"/api/questions/{q['id']}/answers", {"body": "like this"})
        self.assertTrue(a["answers"])
        v = self.req("POST", "/api/questions/vote",
                     {"target_type": "question", "target_id": q["id"], "value": 1})
        self.assertEqual(v["score"], 1)
        c = self.req("POST", "/api/questions/comments",
                     {"target_type": "question", "target_id": q["id"], "body": "noted"})
        self.assertEqual(len(c["comments"]), 1)
        # the old /api/board/* paths are gone
        try:
            self.req("GET", "/api/board/questions")
            self.fail("legacy board route still served")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_global_overview_still_works(self):
        ov = self.req("GET", "/api/overview")
        self.assertIn("tickets", ov)

    def test_chat_and_project_deletion_preserve_unrelated_data(self):
        project=self.req("POST","/api/runner/projects",{"name":"Delete me"})
        chat=self.req("POST","/api/runner/sessions",{"title":"Keep this chat","project_id":project['id']})
        other=self.req("POST","/api/runner/sessions",{"title":"Delete this chat"})
        conn=db.connect()
        conn.execute("INSERT INTO orchestrator_messages(session_id,role,kind,body,created_at) VALUES(?,?,?,?,?)",(chat['id'],'user','text','preserve me','now'))
        conn.execute("INSERT INTO orchestrator_messages(session_id,role,kind,body,created_at) VALUES(?,?,?,?,?)",(other['id'],'user','text','remove me','now'))
        conn.commit()

        self.assertEqual(self.req('DELETE',f"/api/runner/projects/{project['id']}")['deleted'],project['id'])
        preserved=self.req('GET',f"/api/runner/sessions/{chat['id']}")
        self.assertIsNone(preserved['project_id'])
        self.assertEqual([m['body'] for m in preserved['messages']],['preserve me'])

        self.assertEqual(self.req('DELETE',f"/api/runner/sessions/{other['id']}")['deleted'],other['id'])
        self.assertEqual(conn.execute('SELECT count(*) FROM orchestrator_messages WHERE session_id=?',(other['id'],)).fetchone()[0],0)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.req('GET',f"/api/runner/sessions/{other['id']}")
        self.assertEqual(error.exception.code,404)

        from coms.runner import RUNNER
        with patch.object(RUNNER,'orchestrator_busy',True),patch.object(RUNNER,'orchestrator_session',chat['id']):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.req('DELETE',f"/api/runner/sessions/{chat['id']}")
            self.assertEqual(error.exception.code,400)
        self.assertEqual(self.req('DELETE',f"/api/runner/sessions/{chat['id']}")['deleted'],chat['id'])
        self.assertEqual(conn.execute('SELECT count(*) FROM orchestrator_sessions').fetchone()[0],0)
        db._migrate(conn)
        conn.commit()
        self.assertEqual(conn.execute('SELECT count(*) FROM orchestrator_sessions').fetchone()[0],0)

    def test_orchestrator_stop_route_calls_runner_and_maps_invalid_state(self):
        from coms.runner import RUNNER
        with patch.object(RUNNER, 'stop_orchestrator', create=True, return_value={'stopping': True}) as stop:
            result = self.req('POST', '/api/runner/sessions/stop-route-test/stop', {})
        self.assertEqual(result, {'stopping': True})
        stop.assert_called_once_with('stop-route-test')

        with patch.object(RUNNER, 'stop_orchestrator', create=True, side_effect=ValueError('chat is not running')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.req('POST', '/api/runner/sessions/stop-route-test/stop', {})
        self.assertEqual(error.exception.code, 400)

    def test_invalid_json_shapes_and_parameters_are_client_errors(self):
        for body in [[], "text", 3, None]:
            # A JSON null body is distinct from no body.
            r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/tickets", method="POST",
                                       data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(r)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.req("GET", "/api/overview?ticket=invalid")
        self.assertEqual(error.exception.code, 400)

    def test_structured_chat_api_keeps_errors_visible_and_logs_opt_in(self):
        from coms.orchestrator_transcript import Transcript, begin_turn
        chat = self.req('POST','/api/runner/sessions',{'title':'Structured API chat'})
        try:
            turn, user = begin_turn(chat['id'],'List files','codex','model','/workspace')
            transcript = Transcript(chat['id'],turn['id'])
            transcript.apply({'sessionUpdate':'tool_call','toolCallId':'ls','title':'List files'})
            transcript.apply({'sessionUpdate':'tool_call_update','toolCallId':'ls','status':'completed','rawOutput':''})
            transcript.apply({'sessionUpdate':'agent_thought_chunk','content':{'type':'text','text':'private'}})
            transcript.finish(error='Backend disconnected')
            detail = self.req('GET',f"/api/runner/sessions/{chat['id']}")
            self.assertEqual([m['kind'] for m in detail['messages']],['text','tool_call','error'])
            self.assertEqual(detail['messages'][0]['id'],user['id'])
            self.assertEqual(detail['messages'][1]['data']['rawOutput'],'')
            self.assertEqual(detail['turns'][0]['status'],'failed')
            self.assertEqual(detail['logs'],[])
            logs = self.req('GET',f"/api/runner/sessions/{chat['id']}?include_logs=1")['logs']
            self.assertEqual(len(logs),1)
        finally:
            self.req('DELETE',f"/api/runner/sessions/{chat['id']}")
        self.assertEqual(db.connect().execute('SELECT count(*) FROM orchestrator_turns WHERE session_id=?',(chat['id'],)).fetchone()[0],0)

    def test_cross_origin_and_untrusted_host_rejected(self):
        for headers in [{"Host": "evil.example"}, {"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Host": "["}]:
            r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", headers=headers)
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(r)
            self.assertEqual(error.exception.code, 403)

    def test_workspace_settings_reject_app_and_accept_separate_directory(self):
        from coms.workspace import APP_ROOT
        for workspace in [str(APP_ROOT), str(APP_ROOT.parent), str(APP_ROOT / "data"), "relative", ""]:
            with self.subTest(workspace=workspace), self.assertRaises(urllib.error.HTTPError) as error:
                self.req("PUT", "/api/runner/settings", {"workspace": workspace})
            self.assertEqual(error.exception.code, 400)
        with tempfile.TemporaryDirectory() as folder:
            self.req("PUT", "/api/runner/settings", {"workspace": folder})
            settings = self.req("GET", "/api/runner/settings")
            self.assertEqual(settings["workspace"], folder)
            self.assertEqual(settings["workspace_error"], "")

    def test_template_edits_preserve_id_and_can_be_cleared(self):
        first = self.req("POST", "/api/runner/templates", {"name": "HTTP template", "prompt": "First"})
        t = self.req("POST", "/api/tickets", {"title": "Templated work", "agent_template_id": first["id"]})
        second = self.req("POST", "/api/runner/templates", {"name": "HTTP template", "prompt": "Second"})
        self.assertEqual(first["id"], second["id"])
        self.req("PATCH", f"/api/tickets/{t['id']}", {"priority": "p1"})
        self.assertEqual(self.req("GET", f"/api/tickets/{t['id']}")["agent_template_id"], first["id"])
        cleared = self.req("PATCH", f"/api/tickets/{t['id']}", {"agent_template_id": None})
        self.assertIsNone(cleared["agent_template_id"])

    def test_execution_gate_requires_boolean_and_links_reject_script_urls(self):
        for ready in ["false", "true", [], 2]:
            with self.subTest(ready=ready), self.assertRaises(urllib.error.HTTPError) as error:
                self.req("POST", "/api/tickets", {"title": "Bad gate", "ready_for_execution": ready})
            self.assertEqual(error.exception.code, 400)
        t = self.req("POST", "/api/tickets", {"title": "URL test"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.req("POST", f"/api/tickets/{t['id']}/links", {"kind": "url", "ref": "javascript:alert(1)"})
        self.assertEqual(error.exception.code, 400)

    def test_static_security_headers_and_encoded_traversal(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as response:
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/%2e%2e/coms/server.py") as response:
            self.assertIn(b"<!doctype html>", response.read().lower())


if __name__ == "__main__":
    unittest.main()
