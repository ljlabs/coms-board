"""coms-board dashboard server (stdlib only).

    python3 -m coms.server [--host 127.0.0.1] [--port 8765]

Serves the web UI from ../web and a JSON API under /api/. Binds to loopback
by default: there is NO authentication, so do not expose it beyond localhost.
The acting identity for write calls comes from the X-Coms-Agent header
(the UI sends the admin actor by default, see coms.api.ADMIN_ACTOR) or an
`actor` field in the JSON body.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coms import api, db  # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "web"

# Font MIME types for the vendored KaTeX fonts. Python's mimetypes has no
# entries for these on some platforms (guess_type -> None), which would serve
# them as application/octet-stream; declare them explicitly.
for _ext, _mime in ((".woff2", "font/woff2"), (".woff", "font/woff"), (".ttf", "font/ttf")):
    if not mimetypes.guess_type(f"x{_ext}")[0]:
        mimetypes.add_type(_mime, _ext)


class Handler(BaseHTTPRequestHandler):
    server_version = "coms-board/1.0"

    # ---------------------------------------------------------------- helpers
    def _json(self, status: int, data) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except json.JSONDecodeError as e:
            raise api.BadRequest(f"invalid JSON body: {e}")

    def _actor(self, body: dict | None = None) -> str:
        return (body or {}).get("actor") or self.headers.get("X-Coms-Agent") or api.ADMIN_ACTOR

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        f = (WEB / rel).resolve()
        if not str(f).startswith(str(WEB)) or not f.is_file():
            f = WEB / "index.html"   # SPA fallback
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quieter log
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- routing
    def do_GET(self):
        u = urlsplit(self.path)
        if not u.path.startswith("/api/"):
            return self._static(u.path)
        self._dispatch("GET", u.path, parse_qs(u.query, keep_blank_values=True), {})

    def do_POST(self):
        u = urlsplit(self.path)
        try:
            body = self._body()
        except api.BadRequest as e:
            return self._json(400, {"error": str(e)})
        self._dispatch("POST", u.path, parse_qs(u.query, keep_blank_values=True), body)

    do_PUT = do_PATCH = do_DELETE = do_POST

    def _dispatch(self, method: str, path: str, qs: dict, body: dict) -> None:
        method = self.command if self.command in ("PUT", "PATCH", "DELETE") else method
        conn = db.connect()
        q1 = {k: v[0] for k, v in qs.items()}
        actor = self._actor(body)
        try:
            for pattern, handler in ROUTES:
                m = re.fullmatch(pattern, path)
                if m:
                    result = handler(conn, method, m, q1, body, actor)
                    if result is None:
                        return self._json(405, {"error": f"{method} not allowed on {path}"})
                    return self._json(200, result)
            return self._json(404, {"error": f"no route {path}"})
        except api.NotFound as e:
            return self._json(404, {"error": str(e)})
        except api.BadRequest as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover
            traceback.print_exc()
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            # ThreadingHTTPServer hands each request its own thread, and
            # db.connect() caches one sqlite3 connection per thread. Close it
            # once the request is done so the OS file handle doesn't linger
            # (harmless on POSIX, but blocks deleting/replacing the db file
            # on Windows while a stale handler thread is still around).
            conn.close()
            db._local.__dict__.clear()


# -------------------------------------------------------------------- routes
def _bool(v) -> bool:
    return str(v).lower() in ("1", "true", "yes")


def r_overview(conn, m, _mm, q, b, actor):
    if m != "GET":
        return None
    if q.get("ticket"):
        return api.ticket_overview(conn, int(q["ticket"]))
    return api.overview(conn)


def r_search(conn, m, _mm, q, b, actor):
    return api.search_all(conn, q.get("q", ""), int(q.get("limit", 10))) if m == "GET" else None


def r_activity(conn, m, _mm, q, b, actor):
    return api.activity(conn, int(q.get("limit", 50)), q.get("actor")) if m == "GET" else None


def r_departments(conn, m, _mm, q, b, actor):
    if m == "GET":
        return api.list_departments(conn)
    if m == "POST":
        return api.upsert_department(conn, b["slug"], b.get("name") or b["slug"], b.get("description", ""))


def r_agents(conn, m, _mm, q, b, actor):
    if m == "GET":
        return api.list_agents(conn, q.get("dept"))
    if m == "POST":
        return api.upsert_agent(conn, b["slug"], b.get("name") or b["slug"],
                                **{k: b.get(k) for k in ("department", "role", "description",
                                                          "codebases", "wiki_pages", "agent_file")})


def r_agent(conn, m, mm, q, b, actor):
    if m == "GET":
        a = api.get_agent(conn, mm.group(1))
        a["activity"] = api.activity(conn, 30, mm.group(1))
        a["tickets"] = api.list_tickets(conn, assignee=mm.group(1))
        return a


def r_inbox(conn, m, mm, q, b, actor):
    return api.inbox(conn, mm.group(1)) if m == "GET" else None


def r_wiki(conn, m, _mm, q, b, actor):
    if m == "GET":
        if q.get("q"):
            return api.search_wiki(conn, q["q"], int(q.get("limit", 20)))
        folder = q.get("folder")  # '' or '/' = unfiled root; absent = all
        if folder is not None and folder.strip() == "/":
            folder = ""
        return api.list_wiki(conn, q.get("dept"), q.get("tag"), folder=folder,
                             recursive=_bool(q.get("recursive", False)))
    if m == "POST":
        return api.save_wiki(conn, b.get("slug"), b.get("title", ""), b.get("body", ""), actor,
                             tags=b.get("tags"), department=b.get("department"),
                             append=_bool(b.get("append", False)), folder=b.get("folder"))


def r_wiki_tree(conn, m, _mm, q, b, actor):
    return api.wiki_tree(conn, q.get("prefix", "")) if m == "GET" else None


def r_wiki_page(conn, m, mm, q, b, actor):
    slug = unquote(mm.group(1))
    if m == "GET":
        return api.get_wiki(conn, slug)
    if m in ("PUT", "PATCH", "POST"):
        return api.save_wiki(conn, slug, b.get("title", ""), b.get("body", ""), actor,
                             tags=b.get("tags"), department=b.get("department"),
                             append=_bool(b.get("append", False)), folder=b.get("folder"))
    if m == "DELETE":
        api.delete_wiki(conn, slug, actor)
        return {"deleted": slug}


def r_wiki_move(conn, m, mm, q, b, actor):
    """POST /api/wiki/<slug>/move {"folder": "a/b"} — re-file a page; slug unchanged."""
    if m == "POST":
        return api.move_wiki(conn, unquote(mm.group(1)), b.get("folder", ""), actor)


def r_wiki_revs(conn, m, mm, q, b, actor):
    return api.wiki_revisions(conn, unquote(mm.group(1))) if m == "GET" else None


def r_wiki_rev(conn, m, mm, q, b, actor):
    return api.wiki_revision(conn, int(mm.group(1))) if m == "GET" else None


def r_ticket_boards(conn, m, _mm, q, b, actor):
    if m == "GET":
        return api.list_ticket_boards(conn)
    if m == "POST":
        return api.save_ticket_board(conn, b.get("name", ""), actor,
                                     b.get("description", ""), b.get("wiki_slug", ""))


def r_ticket_board(conn, m, mm, q, b, actor):
    bid = int(mm.group(1))
    if m == "GET":
        return api.get_ticket_board(conn, bid)
    if m in ("PUT", "PATCH"):
        board = api.get_ticket_board(conn, bid)
        return api.save_ticket_board(conn, b.get("name", board["name"]), actor,
                                     b.get("description", board["description"]),
                                     b.get("wiki_slug", board["wiki_slug"]), board_id=bid)
    if m == "DELETE":
        api.delete_ticket_board(conn, bid, actor)
        return {"deleted": bid}


def r_tickets(conn, m, _mm, q, b, actor):
    if m == "GET":
        if q.get("q"):
            return api.search_tickets(conn, q["q"], board_id=q.get("board"))
        status = q.get("status")
        assignee = q.get("assignee")
        if _bool(q.get("unassigned", False)):
            assignee = ""
        return api.list_tickets(
            conn,
            status=status.split(",") if status else None,
            assignee=assignee,
            department=q.get("dept"),
            type_=q.get("type"),
            include_closed=_bool(q.get("all", False)),
            parent_id=int(q["parent"]) if q.get("parent") else None,
            board_id=q.get("board"),
        )
    if m == "POST":
        pid = b.get("parent_id")
        return api.create_ticket(conn, b.get("title", ""), b.get("body", ""), actor,
                                 b.get("type", "task"), b.get("priority", "p2"), b.get("department", ""),
                                 b.get("assignee", ""), b.get("tags"), b.get("due_at"),
                                 parent_id=int(pid) if pid not in (None, "") else None,
                                 board_id=b.get("board_id"))


def r_ticket(conn, m, mm, q, b, actor):
    tid = int(mm.group(1))
    if m == "GET":
        return api.get_ticket(conn, tid)
    if m in ("PATCH", "PUT", "POST"):
        return api.update_ticket(conn, tid, actor, **{k: b.get(k) for k in (
            "title", "body", "type", "status", "priority", "department", "assignee",
            "tags", "result", "due_at", "parent_id", "board_id")})


def r_ticket_tree(conn, m, mm, q, b, actor):
    return api.ticket_tree(conn, int(mm.group(1))) if m == "GET" else None


def r_ticket_claim(conn, m, mm, q, b, actor):
    return api.claim_ticket(conn, int(mm.group(1)), actor) if m == "POST" else None


def r_ticket_comments(conn, m, mm, q, b, actor):
    return api.comment_ticket(conn, int(mm.group(1)), actor, b.get("body", "")) if m == "POST" else None


def r_ticket_comment(conn, m, mm, _q, b, actor):
    tid, cid = int(mm.group(1)), int(mm.group(2))
    if m in ("PATCH", "PUT"):
        return api.update_ticket_comment(conn, tid, cid, actor, b.get("body", ""))
    if m == "DELETE":
        return api.delete_ticket_comment(conn, tid, cid, actor)


def r_ticket_links(conn, m, mm, q, b, actor):
    tid = int(mm.group(1))
    if m == "POST":
        return api.link_ticket(conn, tid, b.get("kind", ""), b.get("ref", ""), actor, b.get("note", ""))
    if m == "DELETE":
        return api.unlink_ticket(conn, tid, b.get("kind") or q.get("kind", ""),
                                 b.get("ref") or q.get("ref", ""), actor)
    if m == "GET":
        return api.ticket_links(conn, tid)


def r_questions(conn, m, _mm, q, b, actor):
    if m == "GET":
        if q.get("q"):
            return api.search_questions(conn, q["q"])
        return api.list_questions(conn, q.get("status"), q.get("tag"), q.get("dept"),
                                  _bool(q.get("unanswered", False)))
    if m == "POST":
        return api.ask_question(conn, b.get("title", ""), b.get("body", ""), actor, b.get("tags"),
                                b.get("department", ""))


def r_question(conn, m, mm, q, b, actor):
    qid = int(mm.group(1))
    if m == "GET":
        return api.get_question(conn, qid, actor, count_view=_bool(q.get("view", False)))
    if m in ("PATCH", "PUT", "POST"):
        return api.update_question(conn, qid, actor, **{k: b.get(k) for k in (
            "title", "body", "tags", "department", "status")})


def r_answers(conn, m, mm, q, b, actor):
    return api.answer_question(conn, int(mm.group(1)), actor, b.get("body", "")) if m == "POST" else None


def r_answer(conn, m, mm, q, b, actor):
    if m in ("PATCH", "PUT", "POST"):
        return api.edit_answer(conn, int(mm.group(1)), actor, b.get("body", ""))


def r_accept(conn, m, mm, q, b, actor):
    if m == "POST":
        aid = b.get("answer_id")
        return api.accept_answer(conn, int(mm.group(1)), int(aid) if aid is not None else None, actor)


def r_vote(conn, m, _mm, q, b, actor):
    if m == "POST":
        return api.vote(conn, b["target_type"], int(b["target_id"]), actor, int(b["value"]))


def r_question_comment(conn, m, _mm, q, b, actor):
    if m == "POST":
        return api.comment_question(conn, b["target_type"], int(b["target_id"]), actor, b.get("body", ""))


def r_question_comment_item(conn, m, mm, _q, b, actor):
    comment_id = int(mm.group(1))
    if m in ("PATCH", "PUT"):
        return api.update_question_comment(conn, comment_id, actor, b.get("body", ""))
    if m == "DELETE":
        return api.delete_question_comment(conn, comment_id, actor)


ROUTES = [
    (r"/api/overview", r_overview),
    (r"/api/search", r_search),
    (r"/api/activity", r_activity),
    (r"/api/departments", r_departments),
    (r"/api/agents", r_agents),
    (r"/api/agents/([^/]+)/inbox", r_inbox),
    (r"/api/agents/([^/]+)", r_agent),
    (r"/api/wiki", r_wiki),
    (r"/api/wiki-tree", r_wiki_tree),
    (r"/api/wiki/([^/]+)/revisions", r_wiki_revs),
    (r"/api/wiki/([^/]+)/move", r_wiki_move),
    (r"/api/wiki-revisions/(\d+)", r_wiki_rev),
    (r"/api/wiki/([^/]+)", r_wiki_page),
    (r"/api/ticket-boards", r_ticket_boards),
    (r"/api/ticket-boards/(\d+)", r_ticket_board),
    (r"/api/tickets", r_tickets),
    (r"/api/tickets/(\d+)/claim", r_ticket_claim),
    (r"/api/tickets/(\d+)/comments/(\d+)", r_ticket_comment),
    (r"/api/tickets/(\d+)/comments", r_ticket_comments),
    (r"/api/tickets/(\d+)/links", r_ticket_links),
    (r"/api/tickets/(\d+)/tree", r_ticket_tree),
    (r"/api/tickets/(\d+)", r_ticket),
    (r"/api/questions", r_questions),
    (r"/api/questions/comments/(\d+)", r_question_comment_item),
    (r"/api/questions/(\d+)/answers", r_answers),
    (r"/api/questions/(\d+)/accept", r_accept),
    (r"/api/questions/(\d+)", r_question),
    (r"/api/answers/(\d+)", r_answer),
    (r"/api/questions/vote", r_vote),
    (r"/api/questions/comments", r_question_comment),
]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(argv)
    db.connect()  # ensure schema exists before first request
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"coms-board dashboard: http://{a.host}:{a.port}/   db={db.db_path()}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
