"""db.connect: write mode, read-only mode, and the parent_id migration."""
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from tests import *  # noqa: F401,F403  (path setup)
from coms import api, db


def fresh(tmp: Path, name: str = "t.db") -> Path:
    return tmp / name


class TestConnect(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # drop the per-thread cache so each test gets clean connections
        db._local.__dict__.clear()

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._local.__dict__.clear()
        # restore write permission so cleanup can delete
        for p in self.tmp.rglob("*"):
            os.chmod(p, stat.S_IRWXU)
        os.chmod(self.tmp, stat.S_IRWXU)
        self._tmp.cleanup()

    def test_write_mode_creates_schema_without_wal(self):
        p = fresh(self.tmp)
        conn = db.connect(p)
        # rollback journal, NOT wal: 3.7 cannot read WAL dbs without write access
        self.assertNotEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
        self.assertIn("parent_id", cols)

    def test_legacy_wal_db_is_flipped_back(self):
        p = fresh(self.tmp)
        legacy = sqlite3.connect(str(p))
        legacy.execute("PRAGMA journal_mode=WAL")
        legacy.executescript(db.SCHEMA)
        legacy.commit()
        legacy.close()
        conn = db.connect(p)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_migration_adds_parent_id_to_legacy_db(self):
        """A DB created before the hierarchy (no parent_id) gets the column.

        db.SCHEMA itself is the pre-migration shape — parent_id is only ever
        added by the MIGRATIONS ALTER — so a plain executescript(SCHEMA) IS
        a legacy database.
        """
        p = fresh(self.tmp)
        legacy = sqlite3.connect(str(p))
        legacy.executescript(db.SCHEMA)
        legacy.execute("INSERT INTO tickets(title, created_at, updated_at) VALUES ('old', 'x', 'x')")
        legacy.commit()
        legacy.close()

        conn = db.connect(p)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
        self.assertIn("parent_id", cols)
        t = api.get_ticket(conn, 1)  # legacy row survives, parent_id is NULL
        self.assertEqual(t["title"], "old")
        self.assertIsNone(t["parent_id"])

    def test_readonly_creates_no_sidecar_files_and_refuses_writes(self):
        p = fresh(self.tmp)
        conn = db.connect(p)
        api.create_ticket(conn, "seed", "", "admin")
        conn.close()
        db._local.__dict__.clear()

        ro = db.connect(p, readonly=True)
        rows = api.list_tickets(ro)
        self.assertEqual(len(rows), 1)
        self.assertFalse(Path(str(p) + "-wal").exists(), "-wal created by read-only connect")
        self.assertFalse(Path(str(p) + "-shm").exists(), "-shm created by read-only connect")
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO activity(actor, kind, ref, summary, created_at) "
                       "VALUES ('x','x','x','x','x')")

    def test_readonly_works_without_write_permission(self):
        p = fresh(self.tmp)
        conn = db.connect(p)
        api.create_ticket(conn, "seed", "", "admin")
        conn.close()
        db._local.__dict__.clear()
        os.chmod(p, stat.S_IRUSR)                       # file: r--
        os.chmod(self.tmp, stat.S_IRUSR | stat.S_IXUSR)  # dir: no create

        ro = db.connect(p, readonly=True)
        self.assertEqual(len(api.list_tickets(ro)), 1)

    def test_readonly_fallback_denies_writes_on_legacy_wal_db(self):
        """A db still in WAL (no sidecars) falls back to a write-denying conn."""
        p = fresh(self.tmp)
        legacy = sqlite3.connect(str(p))
        legacy.execute("PRAGMA journal_mode=WAL")
        legacy.executescript(db.SCHEMA)
        legacy.execute("INSERT INTO tickets(title, created_at, updated_at) VALUES ('w','x','x')")
        legacy.commit()
        legacy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        legacy.close()
        for side in (Path(str(p) + "-wal"), Path(str(p) + "-shm")):
            if side.exists():
                side.unlink()

        ro = db.connect(p, readonly=True)
        self.assertEqual(ro.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        with self.assertRaises(sqlite3.DatabaseError):
            ro.execute("INSERT INTO activity(actor, kind, ref, summary, created_at) "
                       "VALUES ('x','x','x','x','x')")

    def test_readonly_missing_file_falls_back_to_write(self):
        p = fresh(self.tmp, "does-not-exist-yet.db")
        conn = db.connect(p, readonly=True)  # falls back, creates schema
        self.assertTrue(p.exists())
        self.assertEqual(api.list_tickets(conn), [])

    def test_legacy_board_tables_are_renamed(self):
        """A DB created before the questions rename gets board_comments/board_fts
        copied to question_comments/question_fts, with data preserved."""
        p = fresh(self.tmp)
        legacy_schema = db.SCHEMA.replace("question_comments", "board_comments") \
                                 .replace("question_fts", "board_fts")
        self.assertIn("board_comments", legacy_schema)
        raw = sqlite3.connect(str(p))
        raw.executescript(legacy_schema)
        raw.execute("INSERT INTO questions(title, body, author, tags, department, status, "
                    "created_at, updated_at) VALUES ('q','b','expert-a','','','open','t','t')")
        raw.execute("INSERT INTO board_comments(target_type, target_id, author, body, created_at) "
                    "VALUES ('question',1,'expert-b','c','t')")
        raw.execute("INSERT INTO board_fts(kind, ref_id, title, body, tags) "
                    "VALUES ('question','1','q','b','')")
        raw.commit()
        raw.close()

        db._local.__dict__.clear()
        conn = db.connect(p)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertIn("question_comments", tables)
        self.assertIn("question_fts", tables)
        self.assertNotIn("board_comments", tables)
        self.assertNotIn("board_fts", tables)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM question_comments").fetchone()[0], 1)
        # data survives and the new code paths read it
        q = api.get_question(conn, 1, "expert-a")
        self.assertEqual([c["body"] for c in q["comments"]], ["c"])
        self.assertEqual([r["question_id"] for r in api.search_questions(conn, "q")], [1])
        # second connect is a no-op (idempotent)
        conn.close()
        db._local.__dict__.clear()
        conn = db.connect(p)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM question_comments").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
