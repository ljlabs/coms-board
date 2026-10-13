"""CLI tests: run coms.py as a subprocess against a throwaway COMS_DB."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "cli.db")

    def tearDown(self):
        for p in Path(self._tmp.name).rglob("*"):
            os.chmod(p, stat.S_IRWXU)
        os.chmod(self._tmp.name, stat.S_IRWXU)
        self._tmp.cleanup()

    def run_coms(self, *argv, agent="admin", check=True):
        env = dict(os.environ, COMS_DB=self.db)
        r = subprocess.run(
            [sys.executable, str(ROOT / "coms.py"), "--as", agent, *argv],
            capture_output=True, text=True, env=env, cwd=str(ROOT))
        if check:
            self.assertEqual(r.returncode, 0, msg=f"stderr: {r.stderr}")
        return r

    def jout(self, r):
        return json.loads(r.stdout)

    def test_shim_exists_and_is_executable(self):
        shim = ROOT / "scripts" / "coms"
        self.assertTrue(shim.exists())
        self.assertTrue(os.access(shim, os.X_OK))

    def test_project_boards(self):
        self.run_coms("wiki", "put", "codebase", "--title", "Codebase", "--body", "docs")
        board = self.jout(self.run_coms("ticket-board", "create", "CLI project", "--wiki-page", "codebase"))
        bid = str(board["id"])
        t = self.jout(self.run_coms("ticket", "create", "cliboardword", "--board", bid))
        self.run_coms("ticket", "create", "cliboardword default")
        self.assertEqual([x["id"] for x in self.jout(self.run_coms("ticket", "list", "--board", bid))], [t["id"]])
        self.assertEqual([x["id"] for x in self.jout(self.run_coms("ticket", "search", "cliboardword", "--board", bid))], [t["id"]])
        updated = self.jout(self.run_coms("ticket-board", "update", bid, "Renamed CLI project"))
        self.assertEqual(updated["wiki_slug"], "codebase")
        self.assertEqual(len(self.jout(self.run_coms("ticket-board", "list"))), 2)
        self.run_coms("ticket-board", "delete", bid)
        self.assertEqual(len(self.jout(self.run_coms("ticket-board", "list"))), 1)
        self.assertEqual(self.jout(self.run_coms("wiki", "get", "codebase"))["body"], "docs")

    def test_create_with_parent_and_tree(self):
        s = self.jout(self.run_coms("ticket", "create", "the story", "--type", "story"))
        j = self.jout(self.run_coms("ticket", "create", "the job", "--type", "job",
                                    "--parent", str(s["id"])))
        t = self.jout(self.run_coms("ticket", "create", "the task", "--type", "task",
                                    "--parent", str(j["id"]), "--assignee", "expert-a"))
        tree = self.jout(self.run_coms("ticket", "tree", str(t["id"])))
        self.assertEqual(tree["root"]["id"], s["id"])
        self.assertEqual(tree["root"]["children"][0]["children"][0]["id"], t["id"])
        # text mode renders the focus marker
        txt = self.run_coms("--text", "ticket", "tree", str(t["id"])).stdout
        self.assertIn("◀", txt)

    def test_done_with_open_children_warns_on_stderr(self):
        j = self.jout(self.run_coms("ticket", "create", "the job", "--type", "job"))
        self.jout(self.run_coms("ticket", "create", "the task", "--parent", str(j["id"])))
        r = self.run_coms("ticket", "done", str(j["id"]), "--result", "partial")
        self.assertIn("open child", r.stderr)

    def test_children_and_parent_filter(self):
        j = self.jout(self.run_coms("ticket", "create", "the job", "--type", "job"))
        t = self.jout(self.run_coms("ticket", "create", "kid", "--parent", str(j["id"])))
        kids = self.jout(self.run_coms("ticket", "children", str(j["id"])))
        self.assertEqual([k["id"] for k in kids], [t["id"]])
        kids2 = self.jout(self.run_coms("ticket", "list", "--parent", str(j["id"])))
        self.assertEqual([k["id"] for k in kids2], [t["id"]])

    def test_overview_scoped_to_ticket(self):
        j = self.jout(self.run_coms("ticket", "create", "the job", "--type", "job"))
        self.jout(self.run_coms("ticket", "create", "unrelated"))
        ov = self.jout(self.run_coms("overview", "--ticket", str(j["id"])))
        self.assertEqual(ov["tickets_total"], 1)
        self.assertEqual(ov["focus"], j["id"])

    def test_read_commands_work_on_readonly_db(self):
        t = self.jout(self.run_coms("ticket", "create", "seed"))
        os.chmod(self.db, stat.S_IRUSR)
        os.chmod(self._tmp.name, stat.S_IRUSR | stat.S_IXUSR)

        for argv in (["overview"], ["inbox"], ["ticket", "list"], ["search", "seed"],
                     ["ticket", "get", str(t["id"])], ["activity"]):
            r = self.run_coms(*argv)
            self.assertEqual(r.returncode, 0, msg=f"{argv} failed: {r.stderr}")
        self.assertFalse(os.path.exists(self.db + "-wal"), "read command created -wal")
        self.assertFalse(os.path.exists(self.db + "-journal"), "read command created -journal")
        # and a WRITE command against the locked db must fail, not silently pass
        r = self.run_coms("ticket", "create", "nope", check=False)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
