"""api-level tests: ticket lifecycle, hierarchy (story > job > task), overview."""
import tempfile
import unittest
from pathlib import Path

from tests import *  # noqa: F401,F403
from coms import api, db


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db._local.__dict__.clear()
        self.conn = db.connect(Path(self._tmp.name) / "t.db")

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._local.__dict__.clear()
        self._tmp.cleanup()


class TestTicketLifecycle(Base):
    def test_create_claim_done(self):
        t = api.create_ticket(self.conn, "do a thing", "body", "admin", assignee="")
        self.assertEqual(t["status"], "open")
        t = api.claim_ticket(self.conn, t["id"], "expert-a")
        self.assertEqual((t["status"], t["assignee"]), ("in_progress", "expert-a"))
        t = api.update_ticket(self.conn, t["id"], "expert-a", status="done", result="did it")
        self.assertEqual(t["status"], "done")
        self.assertNotIn("open_children_warning", t)

    def test_list_filters_and_include_closed(self):
        a = api.create_ticket(self.conn, "open one", "", "admin")
        b = api.create_ticket(self.conn, "closed one", "", "admin")
        api.update_ticket(self.conn, b["id"], "admin", status="done")
        active = api.list_tickets(self.conn)
        self.assertEqual([t["id"] for t in active], [a["id"]])
        # regression for the 2026-09-11 positional-binding bug:
        # include_closed must include done tickets, requester filter must not eat it
        everything = api.list_tickets(self.conn, include_closed=True)
        self.assertEqual({t["id"] for t in everything}, {a["id"], b["id"]})
        by_requester = api.list_tickets(self.conn, requester="admin", include_closed=True)
        self.assertEqual(len(by_requester), 2)

    def test_fts_search_finds_ticket(self):
        api.create_ticket(self.conn, "investigate zorblefrax latency", "", "admin")
        hits = api.search_tickets(self.conn, "zorblefrax")
        self.assertEqual(len(hits), 1)


class TestHierarchy(Base):
    def _family(self):
        story = api.create_ticket(self.conn, "Epic 2 story", "", "admin", type_="story")
        job = api.create_ticket(self.conn, "parent work item", "", "coordinator",
                                type_="job", parent_id=story["id"])
        t1 = api.create_ticket(self.conn, "trace code path", "", "coordinator",
                               type_="task", parent_id=job["id"], assignee="expert-a")
        t2 = api.create_ticket(self.conn, "check metrics", "", "coordinator",
                               type_="task", parent_id=job["id"], assignee="expert-b")
        return story, job, t1, t2

    def test_story_job_task_types_valid(self):
        for k in ("story", "job", "task"):
            self.assertIn(k, api.TICKET_TYPES)

    def test_parent_and_children_on_get(self):
        story, job, t1, t2 = self._family()
        j = api.get_ticket(self.conn, job["id"])
        self.assertEqual(j["parent"]["id"], story["id"])
        self.assertEqual([c["id"] for c in j["children"]], [t1["id"], t2["id"]])

    def test_parent_must_exist(self):
        with self.assertRaises(api.NotFound):
            api.create_ticket(self.conn, "orphan", "", "admin", parent_id=999)

    def test_self_parent_rejected(self):
        t = api.create_ticket(self.conn, "loner", "", "admin")
        with self.assertRaises(api.BadRequest):
            api.update_ticket(self.conn, t["id"], "admin", parent_id=t["id"])

    def test_cycle_rejected(self):
        story, job, t1, _ = self._family()
        with self.assertRaises(api.BadRequest):
            # story under its own grandchild
            api.update_ticket(self.conn, story["id"], "admin", parent_id=t1["id"])

    def test_reparent_and_detach(self):
        story, job, t1, _ = self._family()
        moved = api.update_ticket(self.conn, t1["id"], "admin", parent_id=story["id"])
        self.assertEqual(moved["parent"]["id"], story["id"])
        detached = api.update_ticket(self.conn, t1["id"], "admin", parent_id="none")
        self.assertIsNone(detached["parent_id"])

    def test_tree_resolves_to_root_from_any_node(self):
        story, job, t1, t2 = self._family()
        tree = api.ticket_tree(self.conn, t2["id"])  # ask from a leaf
        self.assertEqual(tree["root"]["id"], story["id"])
        self.assertEqual(tree["focus"], t2["id"])
        job_node = tree["root"]["children"][0]
        self.assertEqual(job_node["id"], job["id"])
        self.assertEqual({c["id"] for c in job_node["children"]}, {t1["id"], t2["id"]})

    def test_close_with_open_children_warns(self):
        story, job, t1, t2 = self._family()
        closed = api.update_ticket(self.conn, job["id"], "coordinator", status="done")
        warn = closed.get("open_children_warning")
        self.assertIsNotNone(warn)
        self.assertEqual({k["id"] for k in warn}, {t1["id"], t2["id"]})
        # close the children, reopen+reclose the job: warning gone
        api.update_ticket(self.conn, t1["id"], "expert-a", status="done")
        api.update_ticket(self.conn, t2["id"], "expert-b", status="wontfix")
        api.update_ticket(self.conn, job["id"], "coordinator", status="open")
        closed = api.update_ticket(self.conn, job["id"], "coordinator", status="done")
        self.assertNotIn("open_children_warning", closed)

    def test_children_listing_via_parent_filter(self):
        story, job, t1, t2 = self._family()
        kids = api.list_tickets(self.conn, parent_id=job["id"], include_closed=True)
        self.assertEqual({t["id"] for t in kids}, {t1["id"], t2["id"]})

    def test_ticket_overview_scoped(self):
        story, job, t1, t2 = self._family()
        # unrelated noise that must NOT appear in the scoped view
        api.create_ticket(self.conn, "unrelated noise", "", "admin")
        ov = api.ticket_overview(self.conn, t1["id"])
        self.assertEqual(ov["tickets_total"], 4)
        self.assertEqual(ov["tree"]["id"], story["id"])
        refs = {a["ref"] for a in ov["recent_activity"]}
        self.assertTrue(all(r.startswith("ticket:") for r in refs))
        family = {f"ticket:{x['id']}" for x in (story, job, t1, t2)}
        self.assertTrue(refs.issubset(family))


class TestInbox(Base):
    def test_awaiting_my_review_bucket(self):
        api.upsert_agent(self.conn, "coordinator", "Coordinator")
        api.upsert_agent(self.conn, "expert-a", "Expert A")
        t = api.create_ticket(self.conn, "research", "", "coordinator", assignee="expert-a")
        api.update_ticket(self.conn, t["id"], "expert-a", status="review")
        box = api.inbox(self.conn, "coordinator")
        self.assertEqual([x["id"] for x in box["awaiting_my_review"]], [t["id"]])


if __name__ == "__main__":
    unittest.main()


class TestLinks(Base):
    """ticket link: attach wiki pages, questions, tickets, urls."""

    def _ticket(self, title="work"):
        return api.create_ticket(self.conn, title, "", "admin")

    def test_link_wiki_question_ticket_url(self):
        t = self._ticket()
        api.save_wiki(self.conn, None, "Wiki slug five", "body", "admin")
        q = api.ask_question(self.conn, "how does X work?", "", "admin")
        other = self._ticket("related")
        t = api.link_ticket(self.conn, t["id"], "wiki", "wiki-slug-five", "expert-a", note="findings")
        t = api.link_ticket(self.conn, t["id"], "question", str(q["id"]), "expert-a")
        t = api.link_ticket(self.conn, t["id"], "ticket", str(other["id"]), "expert-a")
        t = api.link_ticket(self.conn, t["id"], "url", "https://example.com/x", "expert-a")
        kinds = [(ln["kind"], ln["ref"]) for ln in t["links"]]
        self.assertEqual(kinds, [("wiki", "wiki-slug-five"), ("question", str(q["id"])),
                                 ("ticket", str(other["id"])), ("url", "https://example.com/x")])
        # display titles resolved for readers
        self.assertEqual(t["links"][0]["title"], "Wiki slug five")
        self.assertEqual(t["links"][1]["title"], "how does X work?")

    def test_link_is_idempotent(self):
        t = self._ticket()
        api.save_wiki(self.conn, None, "Wiki slug five", "b", "admin")
        api.link_ticket(self.conn, t["id"], "wiki", "wiki-slug-five", "a")
        t = api.link_ticket(self.conn, t["id"], "wiki", "wiki-slug-five", "a")
        self.assertEqual(len(t["links"]), 1)


class TestAcceptGuard(Base):
    """questions accept: only the question author (or the admin actor) may accept/un-accept."""

    def _qa(self):
        q = api.ask_question(self.conn, "how does X work?", "", "asker-a")
        a = api.answer_question(self.conn, q["id"], "expert-b", "because Y")
        return q, a

    def test_author_can_accept(self):
        q, a = self._qa()
        out = api.accept_answer(self.conn, q["id"], a["id"], "asker-a")
        self.assertEqual(out["accepted_answer_id"], a["id"])
        self.assertEqual(out["status"], "answered")

    def test_admin_can_accept(self):
        q, a = self._qa()
        out = api.accept_answer(self.conn, q["id"], a["id"], "admin")
        self.assertEqual(out["accepted_answer_id"], a["id"])

    def test_non_author_refused(self):
        q, a = self._qa()
        with self.assertRaises(api.BadRequest):
            api.accept_answer(self.conn, q["id"], a["id"], "expert-b")  # answerer self-accept
        with self.assertRaises(api.BadRequest):
            api.accept_answer(self.conn, q["id"], a["id"], "bystander-c")
        # unchanged: still open, no accepted answer
        fresh = api.get_question(self.conn, q["id"], "asker-a")
        self.assertIsNone(fresh["accepted_answer_id"])
        self.assertEqual(fresh["status"], "open")

    def test_non_author_cannot_unaccept(self):
        q, a = self._qa()
        api.accept_answer(self.conn, q["id"], a["id"], "asker-a")
        with self.assertRaises(api.BadRequest):
            api.accept_answer(self.conn, q["id"], None, "expert-b")


class TestLinksMore(Base):
    def _ticket(self, title="work"):
        return api.create_ticket(self.conn, title, "", "admin")

    def test_link_validates_referent(self):
        t = self._ticket()
        with self.assertRaises(api.BadRequest):
            api.link_ticket(self.conn, t["id"], "wiki", "no-such-page", "a")
        with self.assertRaises(api.BadRequest):
            api.link_ticket(self.conn, t["id"], "question", "999", "a")
        with self.assertRaises(api.BadRequest):
            api.link_ticket(self.conn, t["id"], "ticket", str(t["id"]), "a")  # self-link
        with self.assertRaises(api.BadRequest):
            api.link_ticket(self.conn, t["id"], "bogus", "x", "a")

    def test_unlink(self):
        t = self._ticket()
        api.save_wiki(self.conn, None, "Wiki slug five", "b", "admin")
        api.link_ticket(self.conn, t["id"], "wiki", "wiki-slug-five", "a")
        t = api.unlink_ticket(self.conn, t["id"], "wiki", "wiki-slug-five", "a")
        self.assertEqual(t["links"], [])
        with self.assertRaises(api.NotFound):
            api.unlink_ticket(self.conn, t["id"], "wiki", "wiki-slug-five", "a")

    def test_links_in_ticket_overview(self):
        job = api.create_ticket(self.conn, "job", "", "admin", type_="job")
        task = api.create_ticket(self.conn, "task", "", "admin", parent_id=job["id"])
        api.save_wiki(self.conn, None, "Wiki slug five", "b", "admin")
        api.link_ticket(self.conn, task["id"], "wiki", "wiki-slug-five", "expert-a")
        ov = api.ticket_overview(self.conn, job["id"])
        self.assertEqual([(l["ticket_id"], l["kind"], l["ref"]) for l in ov["links"]],
                         [(task["id"], "wiki", "wiki-slug-five")])
