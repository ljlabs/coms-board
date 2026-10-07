"""Project ticket isolation, shared context, deletion, and legacy migration."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from coms import api, db


class TestTicketBoards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "boards.db"
        db._local.__dict__.clear()
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        db._local.__dict__.clear()
        self.tmp.cleanup()

    def board(self, name="Project"):
        return api.save_ticket_board(self.conn, name, "admin")

    def ticket(self, title="sharedword task", **kwargs):
        return api.create_ticket(self.conn, title, "", "admin", **kwargs)

    def test_legacy_migration_is_idempotent_and_preserves_tickets(self):
        self.conn.close()
        db._local.__dict__.clear()
        path = Path(self.tmp.name) / "legacy.db"
        legacy = sqlite3.connect(path)
        legacy.executescript(db.SCHEMA)
        legacy.execute("DROP TABLE ticket_boards")
        legacy.execute("INSERT INTO tickets(title, created_at, updated_at) VALUES ('old', 'x', 'x')")
        legacy.commit()
        legacy.close()
        self.conn = db.connect(path)
        self.assertEqual(api.get_ticket(self.conn, 1)["board_id"], 1)
        self.assertEqual(api.list_ticket_boards(self.conn)[0]["ticket_count"], 1)
        api.save_ticket_board(self.conn, "Existing work", "admin", board_id=1)
        self.conn.close()
        db._local.__dict__.clear()
        self.conn = db.connect(path)
        self.assertEqual(api.list_ticket_boards(self.conn)[0]["name"], "Existing work")
        self.assertEqual(api.get_ticket(self.conn, 1)["title"], "old")

    def test_lists_and_search_are_scoped_before_limit(self):
        other = self.ticket()
        board = self.board()
        ticket = self.ticket(board_id=board["id"], tags="project")
        api.update_ticket(self.conn, ticket["id"], "admin", status="done")
        self.assertEqual(api.list_tickets(self.conn, board_id=board["id"]), [])
        scoped = api.list_tickets(self.conn, board_id=board["id"], include_closed=True)
        self.assertEqual([t["id"] for t in scoped], [ticket["id"]])
        self.assertEqual([t["id"] for t in api.search_tickets(
            self.conn, "sharedword", limit=1, board_id=board["id"])], [ticket["id"]])
        self.assertEqual(len(api.search_all(self.conn, "sharedword")["tickets"]), 2)
        self.assertEqual(api.get_ticket(self.conn, other["id"])["board_id"], 1)

    def test_board_validation(self):
        board = self.board()
        for name in ("", "  ", " project "):
            with self.assertRaises(api.BadRequest):
                self.board(name)
        for value in (0, -1, "bad", True, 1.5):
            with self.assertRaises(api.BadRequest):
                self.ticket(board_id=value)
        with self.assertRaises(api.NotFound):
            self.ticket(board_id=999)
        with self.assertRaises(api.NotFound):
            api.save_ticket_board(self.conn, "Missing wiki", "admin", wiki_slug="missing")
        self.assertEqual(api.get_ticket_board(self.conn, board["id"])["name"], "Project")

    def test_hierarchy_stays_within_board_and_children_inherit(self):
        board = self.board()
        parent = self.ticket(board_id=board["id"])
        child = self.ticket(parent_id=parent["id"])
        self.assertEqual(child["board_id"], board["id"])
        with self.assertRaises(api.BadRequest):
            self.ticket(parent_id=parent["id"], board_id=1)
        with self.assertRaises(api.BadRequest):
            api.update_ticket(self.conn, child["id"], "admin", board_id=1)
        with self.assertRaises(api.BadRequest):
            api.update_ticket(self.conn, parent["id"], "admin", board_id=1)
        moved = api.update_ticket(self.conn, child["id"], "admin", parent_id="none", board_id=1)
        self.assertEqual(moved["board_id"], 1)
        with self.assertRaises(api.BadRequest):
            api.update_ticket(self.conn, moved["id"], "admin", parent_id=parent["id"])

    def test_delete_cleans_tickets_and_preserves_shared_resources(self):
        wiki = api.save_wiki(self.conn, "project-docs", "Docs", "codebase notes", "admin")
        board = api.save_ticket_board(self.conn, "Project", "admin", wiki_slug=wiki["slug"])
        question = api.ask_question(self.conn, "Shared question", "", "admin")
        ticket = self.ticket(board_id=board["id"])
        child = self.ticket(parent_id=ticket["id"])
        shared = self.ticket()
        api.comment_ticket(self.conn, ticket["id"], "admin", "comment")
        api.link_ticket(self.conn, ticket["id"], "wiki", wiki["slug"], "admin")
        api.link_ticket(self.conn, shared["id"], "ticket", str(child["id"]), "admin")
        api.delete_ticket_board(self.conn, board["id"], "admin")
        with self.assertRaises(api.NotFound):
            api.get_ticket(self.conn, child["id"])
        with self.assertRaises(api.NotFound):
            api.get_ticket_board(self.conn, board["id"])
        for table in ("ticket_comments", "ticket_links"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual([t["id"] for t in api.search_tickets(self.conn, "sharedword")], [shared["id"]])
        self.assertEqual(api.get_wiki(self.conn, wiki["slug"])["body"], "codebase notes")
        self.assertEqual(api.get_question(self.conn, question["id"])["title"], "Shared question")
        with self.assertRaises(api.BadRequest):
            api.delete_ticket_board(self.conn, 1, "admin")
