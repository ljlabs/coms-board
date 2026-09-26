"""Wiki folders: a page's LOCATION (folder path) is separate from its IDENTIFIER (slug).

Covers the api (normalise / save / move / list / tree), the ALTER TABLE migration of a
pre-folder database, the CLI as a subprocess (put --folder, mv, tree, list --folder -r),
and the HTTP server (folder on save, /api/wiki-tree, /api/wiki/<slug>/move).
Invariant under test everywhere: moving a page never changes its slug, so [[wikilinks]],
backlinks and full-text search keep working.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests import *  # noqa: F401,F403
from coms import api, db
from coms.server import Handler

ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db._local.__dict__.clear()
        self.conn = db.connect(Path(self._tmp.name) / "w.db")

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._local.__dict__.clear()
        self._tmp.cleanup()

    def put(self, slug, folder=None, body="", title=None, author="agent-a"):
        return api.save_wiki(self.conn, slug, title or slug.replace("-", " "), body, author,
                             folder=folder)


class TestNormFolder(Base):
    def test_root_forms(self):
        for v in (None, "", "/", "//", " / "):
            self.assertEqual(api.norm_folder(v), "")

    def test_kebab_and_trim(self):
        self.assertEqual(api.norm_folder("/Folder A/ Folder B /Folder C/"),
                         "folder-a/folder-b/folder-c")
        self.assertEqual(api.norm_folder("a\\b"), "a/b")  # backslashes tolerated

    def test_rejects_dot_segments(self):
        with self.assertRaises(api.BadRequest):
            api.norm_folder("a/../b")
        with self.assertRaises(api.BadRequest):
            api.norm_folder("./a")


class TestSaveMoveList(Base):
    def test_create_in_folder_and_default_unfiled(self):
        a = self.put("wiki-slug-3", folder="folder-a/folder-b")
        b = self.put("loose-note")
        self.assertEqual(a["folder"], "folder-a/folder-b")
        self.assertEqual(b["folder"], "")

    def test_update_without_folder_keeps_folder(self):
        self.put("p", folder="research")
        p = api.save_wiki(self.conn, "p", "p", "new body", "agent-a")  # folder=None
        self.assertEqual(p["folder"], "research")
        p = api.save_wiki(self.conn, "p", "p", "more", "agent-a", append=True, folder="folder-d")
        self.assertEqual((p["folder"], p["body"].endswith("more")), ("folder-d", True))

    def test_move_keeps_slug_links_and_fts(self):
        self.put("wiki-slug-1", folder="a", body="wiki-slug-1 body about zorblefrax keys")
        self.put("wiki-slug-2", body="see [[wiki-slug-1]] for details")
        before = api.get_wiki(self.conn, "wiki-slug-1")
        moved = api.move_wiki(self.conn, "wiki-slug-1", "folder-a/folder-b/folder-d", "agent-a")
        self.assertEqual(moved["slug"], "wiki-slug-1")
        self.assertEqual(moved["id"], before["id"])
        self.assertEqual(moved["folder"], "folder-a/folder-b/folder-d")
        # backlink still resolves and FTS still finds it
        self.assertEqual([b["slug"] for b in moved["backlinks"]], ["wiki-slug-2"])
        self.assertEqual([r["slug"] for r in api.search_wiki(self.conn, "zorblefrax")], ["wiki-slug-1"])
        self.assertEqual(api.search_wiki(self.conn, "zorblefrax")[0]["folder"], "folder-a/folder-b/folder-d")
        # move to root
        self.assertEqual(api.move_wiki(self.conn, "wiki-slug-1", "", "agent-a")["folder"], "")
        # move logs activity; a no-op move does not
        acts = [a for a in api.activity(self.conn, 50) if a["kind"] == "wiki.move"]
        self.assertEqual(len(acts), 2)
        api.move_wiki(self.conn, "wiki-slug-1", "/", "agent-a")
        self.assertEqual(len([a for a in api.activity(self.conn, 50) if a["kind"] == "wiki.move"]), 2)

    def test_move_missing_page(self):
        with self.assertRaises(api.NotFound):
            api.move_wiki(self.conn, "nope", "x", "agent-a")

    def test_list_folder_exact_recursive_and_root(self):
        self.put("r1")
        self.put("s1", folder="folder-a")
        self.put("s2", folder="folder-a/folder-b")
        self.put("s3", folder="folder-a/folder-b/folder-c")
        self.put("o1", folder="folder-d")
        slugs = lambda rows: sorted(r["slug"] for r in rows)  # noqa: E731
        self.assertEqual(slugs(api.list_wiki(self.conn)), ["o1", "r1", "s1", "s2", "s3"])
        self.assertEqual(slugs(api.list_wiki(self.conn, folder="")), ["r1"])
        self.assertEqual(slugs(api.list_wiki(self.conn, folder="folder-a")), ["s1"])
        self.assertEqual(slugs(api.list_wiki(self.conn, folder="folder-a", recursive=True)),
                         ["s1", "s2", "s3"])
        self.assertEqual(slugs(api.list_wiki(self.conn, folder="folder-a/folder-b", recursive=True)),
                         ["s2", "s3"])
        # root + recursive = everything; 'folder-a-x' must NOT match the 'folder-a' prefix
        self.put("sx", folder="folder-a-x")
        self.assertEqual(slugs(api.list_wiki(self.conn, folder="", recursive=True)),
                         ["o1", "r1", "s1", "s2", "s3", "sx"])
        self.assertEqual(slugs(api.list_wiki(self.conn, folder="folder-a", recursive=True)),
                         ["s1", "s2", "s3"])


class TestTree(Base):
    def test_tree_implies_ancestors_and_counts(self):
        self.put("r1")
        self.put("deep", folder="folder-a/folder-b/folder-c")  # 'folder-a' and 'folder-a/folder-b' have no pages
        self.put("ops", folder="folder-d")
        t = api.wiki_tree(self.conn)
        paths = [f["path"] for f in t["folders"]]
        self.assertEqual(paths, ["folder-a", "folder-a/folder-b", "folder-a/folder-b/folder-c", "folder-d"])
        by = {f["path"]: f for f in t["folders"]}
        self.assertEqual((by["folder-a"]["page_count"], by["folder-a"]["total_count"]), (0, 1))
        self.assertEqual(by["folder-a/folder-b/folder-c"]["depth"], 2)
        self.assertEqual(t["unfiled_count"], 1)
        self.assertEqual([p["slug"] for p in t["pages"]["folder-d"]], ["ops"])

    def test_tree_prefix(self):
        self.put("a", folder="folder-a/folder-b")
        self.put("b", folder="folder-d")
        t = api.wiki_tree(self.conn, "folder-a")
        self.assertEqual([f["path"] for f in t["folders"]], ["folder-a", "folder-a/folder-b"])
        self.assertNotIn("folder-d", t["pages"])


class TestMigration(unittest.TestCase):
    """A database created before `folder` existed gets the column on first write connect,
    every existing page lands unfiled, and reads work in read-only mode afterwards."""

    def test_legacy_db_gains_folder_column(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "legacy.db"
            legacy_schema = db.SCHEMA.replace(
                "  folder      TEXT NOT NULL DEFAULT '',            -- slash-separated location path; '' = unfiled\n", "")
            self.assertNotIn("folder", legacy_schema.split("CREATE TABLE IF NOT EXISTS wiki_pages")[1]
                             .split(");")[0])
            raw = sqlite3.connect(str(p))
            raw.executescript(legacy_schema)
            raw.execute("INSERT INTO wiki_pages(slug,title,body,tags,department,author,updated_by,"
                        "created_at,updated_at) VALUES ('old','Old','b','','','k','k','t','t')")
            raw.commit(); raw.close()
            db._local.__dict__.clear()
            conn = db.connect(p)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)")}
            self.assertIn("folder", cols)
            self.assertEqual(api.get_wiki(conn, "old")["folder"], "")
            api.move_wiki(conn, "old", "archive", "agent-a")
            conn.close()
            db._local.__dict__.clear()
            ro = db.connect(p, readonly=True)
            self.assertEqual(api.get_wiki(ro, "old")["folder"], "archive")
            self.assertEqual([f["path"] for f in api.wiki_tree(ro)["folders"]], ["archive"])
            ro.close()
            db._local.__dict__.clear()


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "cli.db")

    def tearDown(self):
        self._tmp.cleanup()

    def run_coms(self, *argv, agent="agent-a", check=True):
        r = subprocess.run([sys.executable, str(ROOT / "coms.py"), "--as", agent, *argv],
                           capture_output=True, text=True, env=dict(os.environ, COMS_DB=self.db),
                           cwd=str(ROOT))
        if check:
            self.assertEqual(r.returncode, 0, msg=f"stderr: {r.stderr}")
        return r

    def j(self, r):
        return json.loads(r.stdout)

    def test_put_folder_mv_tree_list(self):
        p = self.j(self.run_coms("wiki", "put", "wiki-slug-4", "--title", "Wiki slug four",
                                 "--body", "x", "--folder", "Folder A/SubFolder"))
        self.assertEqual(p["folder"], "folder-a/subfolder")
        self.j(self.run_coms("wiki", "put", "loose", "--title", "Loose", "--body", "y"))
        m = self.j(self.run_coms("wiki", "mv", "loose", "folder-d/folder-e"))
        self.assertEqual((m["slug"], m["folder"]), ("loose", "folder-d/folder-e"))
        tree = self.j(self.run_coms("wiki", "tree"))
        self.assertEqual([f["path"] for f in tree["folders"]],
                         ["folder-a", "folder-a/subfolder", "folder-d", "folder-d/folder-e"])
        txt = self.run_coms("--text", "wiki", "tree").stdout
        self.assertIn("folder-a/", txt); self.assertIn("- wiki-slug-4", txt)
        sub = self.j(self.run_coms("wiki", "tree", "folder-d"))
        self.assertEqual([f["path"] for f in sub["folders"]], ["folder-d", "folder-d/folder-e"])
        # list scoping
        self.assertEqual([x["slug"] for x in self.j(self.run_coms("wiki", "list", "--folder", "folder-d"))], [])
        self.assertEqual([x["slug"] for x in self.j(self.run_coms("wiki", "list", "--folder", "folder-d", "-r"))],
                         ["loose"])
        # back to root via '/'
        self.assertEqual(self.j(self.run_coms("wiki", "mv", "loose", "/"))["folder"], "")
        self.assertEqual([x["slug"] for x in self.j(self.run_coms("wiki", "list", "--folder", "/"))], ["loose"])
        # get shows folder in text mode
        self.assertIn("folder=/folder-a/subfolder", self.run_coms("--text", "wiki", "get", "wiki-slug-4").stdout)

    def test_bad_folder_is_a_clean_error(self):
        r = self.run_coms("wiki", "put", "p", "--title", "P", "--body", "x", "--folder", "a/../b", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("invalid folder", r.stderr + r.stdout)

    def test_tree_is_readonly(self):
        self.run_coms("wiki", "put", "p", "--title", "P", "--body", "x", "--folder", "a")
        self.run_coms("wiki", "tree")
        self.assertFalse(Path(self.db + "-journal").exists())
        self.assertFalse(Path(self.db + "-wal").exists())


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["COMS_DB"] = str(Path(cls._tmp.name) / "srv.db")
        db._local.__dict__.clear()
        db.connect()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        os.environ.pop("COMS_DB", None)
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._local.__dict__.clear()
        cls._tmp.cleanup()

    def req(self, method, path, body=None):
        r = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-Coms-Agent": "agent-a"})
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read())

    def test_folders_over_http(self):
        p = self.req("POST", "/api/wiki", {"slug": "http-page", "title": "HTTP", "body": "b",
                                            "folder": "folder-e/folder-f"})
        self.assertEqual(p["folder"], "folder-e/folder-f")
        self.req("POST", "/api/wiki", {"slug": "root-page", "title": "R", "body": "b"})
        tree = self.req("GET", "/api/wiki-tree")
        self.assertEqual([f["path"] for f in tree["folders"]], ["folder-e", "folder-e/folder-f"])
        self.assertEqual(tree["unfiled_count"], 1)
        lst = self.req("GET", "/api/wiki?folder=folder-e&recursive=1")
        self.assertEqual([x["slug"] for x in lst], ["http-page"])
        self.assertEqual([x["slug"] for x in self.req("GET", "/api/wiki?folder=")], ["root-page"])
        mv = self.req("POST", "/api/wiki/http-page/move", {"folder": "archive"})
        self.assertEqual((mv["slug"], mv["folder"]), ("http-page", "archive"))
        # PUT without folder keeps it
        upd = self.req("PUT", "/api/wiki/http-page", {"title": "HTTP", "body": "b2"})
        self.assertEqual(upd["folder"], "archive")
        self.assertEqual(self.req("GET", "/api/wiki-tree?prefix=archive")["folders"][0]["page_count"], 1)


if __name__ == "__main__":
    unittest.main()
