"""HTTP API tests: real ThreadingHTTPServer on an ephemeral loopback port."""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
