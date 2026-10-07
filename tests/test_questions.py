"""The Q&A subsystem is addressed as `coms questions ...` (the questions page).

Q&A has no `board` CLI alias, /api/board/* routes, or `board` search key.
Project ticket boards are separate. Q&A activity uses the `question.*` prefix.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import *  # noqa: F401,F403
from coms import api, cli, db

ROOT = Path(__file__).resolve().parents[1]


class TestQuestionsCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "q.db")

    def tearDown(self):
        self._tmp.cleanup()

    def run_coms(self, *argv, agent="admin", check=True):
        r = subprocess.run([sys.executable, str(ROOT / "coms.py"), "--as", agent, *argv],
                           capture_output=True, text=True, env=dict(os.environ, COMS_DB=self.db),
                           cwd=str(ROOT))
        if check:
            self.assertEqual(r.returncode, 0, msg=f"stderr: {r.stderr}")
        return r

    def j(self, r):
        return json.loads(r.stdout)

    def test_questions_ask_comment_list_get_search(self):
        q = self.j(self.run_coms("questions", "ask", "How does tag search handle multiple tags?", "--body", "context",
                                 "--tags", "tags,search"))
        self.assertEqual(q["status"], "open")
        self.j(self.run_coms("questions", "comment", "question", str(q["id"]), "--body", "cited answer",
                             agent="dept-expert"))
        got = self.j(self.run_coms("questions", "get", str(q["id"])))
        self.assertEqual([c["body"] for c in got["comments"]], ["cited answer"])
        self.assertEqual([x["id"] for x in self.j(self.run_coms("questions", "list"))], [q["id"]])
        self.assertEqual([x["question_id"] for x in self.j(self.run_coms("questions", "search", "tags"))],
                         [q["id"]])
        # global search text mode labels the section QUESTIONS
        txt = self.run_coms("--text", "search", "tags").stdout
        self.assertIn("QUESTIONS:", txt)

    def test_no_board_alias(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["board", "list"])
        r = self.run_coms("board", "list", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_questions_reads_are_readonly(self):
        for cmd in (("questions", "list"), ("questions", "get"), ("questions", "search")):
            self.assertIn(cmd, cli.READONLY_CMDS)
        self.run_coms("questions", "ask", "q", "--body", "b")
        self.run_coms("questions", "list")
        self.assertFalse(Path(self.db + "-journal").exists())

    def test_help_advertises_questions(self):
        h = cli.build_parser().format_help()
        self.assertIn("questions", h)
        # Reject the retired Q&A command, while allowing `ticket-board`.
        self.assertNotRegex(h, r"(?m)^\s+board\s|[\{,]board[,\}]")


class TestQuestionsApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db._local.__dict__.clear()
        self.conn = db.connect(Path(self._tmp.name) / "q.db")

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._local.__dict__.clear()
        self._tmp.cleanup()

    def test_search_all_uses_questions_key(self):
        api.ask_question(self.conn, "how does voting work?", "body", "expert-a")
        r = api.search_all(self.conn, "voting")
        self.assertIn("questions", r)
        self.assertNotIn("board", r)

    def test_activity_kinds_use_question_prefix(self):
        q = api.ask_question(self.conn, "how does voting work?", "body", "expert-a")
        api.answer_question(self.conn, q["id"], "expert-b", "like this")
        kinds = {a["kind"] for a in api.activity(self.conn, 50)}
        self.assertTrue(kinds)
        self.assertTrue(all(not k.startswith("board.") for k in kinds))
        self.assertIn("question.ask", kinds)


class TestWebTab(unittest.TestCase):
    def test_web_tab_is_questions(self):
        html = (ROOT / "web/index.html").read_text()
        self.assertIn('data-tab="questions"', html)
        js = (ROOT / "web/app.js").read_text()
        self.assertNotIn("#/board", js)
        self.assertNotIn("/api/board", js)


class TestUiShellContract(unittest.TestCase):
    """Column layouts use the shared layout helper and shared table styles."""

    def setUp(self):
        self.js = (ROOT / "web/app.js").read_text()
        self.css = (ROOT / "web/app.css").read_text()

    def test_no_handrolled_page_shells(self):
        code = "\n".join(l for l in self.js.splitlines() if not l.lstrip().startswith("//"))
        for bad in ('class="row"><div class="col-2"', 'class="row wiki', 'class="col-fixed"', "'side-collapsed'"):
            self.assertNotIn(bad, code, msg=f"hand-rolled page shell in app.js: {bad!r} — use layout()")

    def test_layout_used_by_every_columned_route(self):
        self.assertGreaterEqual(self.js.count("layout({"), 5, "home, wiki index, wiki read, ticket, agent pages must use layout()")
        self.assertIn("wireAside(", self.js)

    def test_no_per_page_layout_css(self):
        for bad in (".wiki-index", ".wiki-read", ".col-fixed", ".wiki-side", ".wiki-main", ".wiki-aside"):
            self.assertNotIn(bad, self.css, msg=f"per-page layout CSS {bad!r} — extend .layout / table.grid.fixed instead")
        for must in (".layout-body { flex: 1 1 auto; min-width: 0; }", "table.grid.fixed", "col.w120"):
            self.assertIn(must, self.css)


class TestVendoredAssets(unittest.TestCase):
    """Dashboard assets are vendored locally: no remote script/style URLs."""

    def test_katex_vendored_and_referenced_locally(self):
        self.assertTrue((ROOT / "web/vendor/katex/katex.min.js").exists())
        self.assertTrue((ROOT / "web/vendor/katex/katex.min.css").exists())
        self.assertTrue((ROOT / "web/vendor/katex/fonts").is_dir())
        self.assertTrue(list((ROOT / "web/vendor/katex/fonts").glob("*.woff2")))
        html = (ROOT / "web/index.html").read_text()
        self.assertIn("/vendor/katex/katex.min.css", html)
        self.assertIn("/vendor/katex/katex.min.js", html)

    def test_no_remote_urls_in_shell(self):
        html = (ROOT / "web/index.html").read_text()
        self.assertNotIn('src="http', html)
        self.assertNotIn("href=\"http", html)
        for name in ("app.js", "editor.js", "md.js", "mermaid.js", "pretty.js"):
            js = (ROOT / "web" / name).read_text()
            self.assertNotIn("https://", js, msg=f"remote URL in web/{name}")
            self.assertNotIn("http://", js, msg=f"remote URL in web/{name}")


if __name__ == "__main__":
    unittest.main()
