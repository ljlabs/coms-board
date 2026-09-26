"""Business logic for the coms-board. Used by both the CLI (agents) and the
HTTP server (dashboard). Every function takes a sqlite3 connection and
returns plain dicts so results serialise straight to JSON.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

TICKET_STATUSES = ("open", "in_progress", "blocked", "review", "done", "wontfix")
# story = human project-level epic; job = an investigation/dispatch run inside a
# story; the rest (research/task/bug/question/chore) are leaf work items.
TICKET_TYPES = ("story", "job", "research", "task", "bug", "question", "chore")
PRIORITIES = ("p0", "p1", "p2", "p3")
QUESTION_STATUSES = ("open", "answered", "closed")

# Slug used for the human operator: the default actor when no identity is supplied
# (dashboard, CLI) and the standing override on question-accept, alongside the
# question's own author. Override by renaming this if your roster uses a different
# slug for the human.
ADMIN_ACTOR = "admin"


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:80] or "page"


def _rows(cur: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in cur]


def _one(cur) -> dict[str, Any] | None:
    r = cur.fetchone()
    return dict(r) if r else None


def _tags(value: str | list[str] | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    else:
        parts = [str(p).strip() for p in value]
    return ",".join(sorted({p for p in parts if p}))


def _fts_query(q: str) -> str:
    """Turn free text into a tolerant FTS4 query (prefix match every token)."""
    # The simple tokenizer splits on non-alphanumerics, so mirror that and use
    # plain `tok*` prefix terms (implicit AND). Works on SQLite 3.7 FTS4.
    toks = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", q) if t]
    if not toks:
        return "zzzz_no_match_zzzz"
    return " ".join(f"{t}*" for t in toks)


def log_activity(conn, actor: str, kind: str, ref: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO activity(actor, kind, ref, summary, created_at) VALUES (?,?,?,?,?)",
        (actor or "", kind, ref, summary, now()),
    )


def touch_agent(conn, slug: str) -> None:
    if slug:
        conn.execute("UPDATE agents SET last_seen=? WHERE slug=?", (now(), slug))


# --------------------------------------------------------------------------
# Departments & agents
# --------------------------------------------------------------------------

def list_departments(conn) -> list[dict]:
    deps = _rows(conn.execute("SELECT * FROM departments ORDER BY name"))
    for d in deps:
        d["agent_count"] = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE department=?", (d["slug"],)
        ).fetchone()[0]
    return deps


def upsert_department(conn, slug: str, name: str, description: str = "") -> dict:
    ex = _one(conn.execute("SELECT * FROM departments WHERE slug=?", (slug,)))
    if ex:
        conn.execute(
            "UPDATE departments SET name=?, description=? WHERE slug=?",
            (name, description, slug),
        )
    else:
        conn.execute(
            "INSERT INTO departments(slug, name, description, created_at) VALUES (?,?,?,?)",
            (slug, name, description, now()),
        )
    conn.commit()
    return _one(conn.execute("SELECT * FROM departments WHERE slug=?", (slug,)))


def list_agents(conn, department: str | None = None) -> list[dict]:
    if department:
        cur = conn.execute(
            "SELECT * FROM agents WHERE department=? ORDER BY role, name", (department,)
        )
    else:
        cur = conn.execute("SELECT * FROM agents ORDER BY department, role, name")
    agents = _rows(cur)
    for a in agents:
        a["open_tickets"] = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE assignee=? AND status NOT IN ('done','wontfix')",
            (a["slug"],),
        ).fetchone()[0]
        a["answers"] = conn.execute(
            "SELECT COUNT(*) FROM answers WHERE author=?", (a["slug"],)
        ).fetchone()[0]
        a["wiki_pages_written"] = conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE author=?", (a["slug"],)
        ).fetchone()[0]
        a["reputation"] = reputation(conn, a["slug"])
    return agents


def get_agent(conn, slug: str) -> dict:
    a = _one(conn.execute("SELECT * FROM agents WHERE slug=?", (slug,)))
    if not a:
        raise NotFound(f"agent {slug!r} not found")
    a["reputation"] = reputation(conn, slug)
    return a


def upsert_agent(conn, slug: str, name: str, **fields) -> dict:
    allowed = {"department", "role", "description", "codebases", "wiki_pages", "agent_file"}
    data = {k: v for k, v in fields.items() if k in allowed and v is not None}
    ex = _one(conn.execute("SELECT * FROM agents WHERE slug=?", (slug,)))
    if ex:
        sets = ", ".join(f"{k}=?" for k in data) or "name=name"
        conn.execute(
            f"UPDATE agents SET name=?, {sets} WHERE slug=?",
            (name, *data.values(), slug),
        )
    else:
        cols = ["slug", "name", *data.keys(), "created_at"]
        conn.execute(
            f"INSERT INTO agents({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (slug, name, *data.values(), now()),
        )
    conn.commit()
    return get_agent(conn, slug)


def reputation(conn, slug: str) -> int:
    """SE-style: +10 per upvote on own answers, -2 per downvote, +15 per accepted answer,
    +5 per upvote on own questions."""
    up_a = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN v.value>0 THEN 10 ELSE -2 END),0) FROM votes v "
        "JOIN answers a ON a.id=v.target_id AND v.target_type='answer' WHERE a.author=?",
        (slug,),
    ).fetchone()[0]
    up_q = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN v.value>0 THEN 5 ELSE -2 END),0) FROM votes v "
        "JOIN questions q ON q.id=v.target_id AND v.target_type='question' WHERE q.author=?",
        (slug,),
    ).fetchone()[0]
    acc = conn.execute(
        "SELECT COUNT(*) FROM questions q JOIN answers a ON a.id=q.accepted_answer_id "
        "WHERE a.author=?",
        (slug,),
    ).fetchone()[0]
    return int(up_a) + int(up_q) + 15 * int(acc)


# --------------------------------------------------------------------------
# Wiki
# --------------------------------------------------------------------------

def norm_folder(folder: str | None) -> str:
    """Normalise a wiki folder path: lower-case kebab segments joined by '/'.

    '' (or None) means unfiled/root. Rejects '.', '..' and empty segments so
    a path is always a clean materialised path such as 'folder-a/folder-b/folder-c'.
    """
    if not folder:
        return ""
    segs = [s.strip() for s in str(folder).replace("\\", "/").split("/")]
    segs = [s for s in segs if s]
    out = []
    for s in segs:
        if s in (".", ".."):
            raise BadRequest(f"invalid folder segment {s!r}")
        seg = slugify(s)
        if not seg:
            raise BadRequest(f"invalid folder segment {s!r}")
        out.append(seg)
    return "/".join(out)


def list_wiki(conn, department: str | None = None, tag: str | None = None,
              folder: str | None = None, recursive: bool = False) -> list[dict]:
    """List wiki pages. `folder` scopes to one folder ('' = unfiled root);
    `recursive=True` includes every sub-folder beneath it (root+recursive = everything)."""
    sql = "SELECT id, slug, title, tags, department, folder, author, updated_by, created_at, updated_at, " \
          "length(body) AS size FROM wiki_pages WHERE 1=1"
    args: list[Any] = []
    if department:
        sql += " AND department=?"
        args.append(department)
    if tag:
        sql += " AND (','||tags||',') LIKE ?"
        args.append(f"%,{tag},%")
    if folder is not None:
        f = norm_folder(folder)
        if recursive:
            if f:
                sql += " AND (folder=? OR folder LIKE ?)"
                args += [f, f + "/%"]
        else:
            sql += " AND folder=?"
            args.append(f)
    sql += " ORDER BY folder, updated_at DESC"
    return _rows(conn.execute(sql, args))


def wiki_tree(conn, prefix: str | None = None) -> dict:
    """Folder outline: every folder (including implied ancestors) with its direct pages.

    Returns {"prefix": p, "folders": [{"path", "name", "depth", "page_count", "total_count"}],
             "pages": {folder_path: [page rows]}}.
    Folders exist only by virtue of the pages in them (mkdir -p semantics).
    """
    p = norm_folder(prefix)
    rows = _rows(conn.execute(
        "SELECT slug, title, folder, tags, updated_at FROM wiki_pages ORDER BY folder, title"))
    pages: dict[str, list[dict]] = {}
    for r in rows:
        f = r["folder"] or ""
        if p and not (f == p or f.startswith(p + "/")):
            continue
        pages.setdefault(f, []).append(r)
    folders: dict[str, dict] = {}
    for f in pages:
        if not f:
            continue
        parts = f.split("/")
        for i in range(1, len(parts) + 1):
            path = "/".join(parts[:i])
            folders.setdefault(path, {"path": path, "name": parts[i - 1], "depth": i - 1,
                                      "page_count": 0, "total_count": 0})
    for f, ps in pages.items():
        if f:
            folders[f]["page_count"] = len(ps)
            parts = f.split("/")
            for i in range(1, len(parts) + 1):
                folders["/".join(parts[:i])]["total_count"] += len(ps)
    return {"prefix": p, "folders": sorted(folders.values(), key=lambda x: x["path"]),
            "pages": pages, "unfiled_count": len(pages.get("", []))}


def get_wiki(conn, slug: str) -> dict:
    p = _one(conn.execute("SELECT * FROM wiki_pages WHERE slug=?", (slug,)))
    if not p:
        raise NotFound(f"wiki page {slug!r} not found")
    p["backlinks"] = _rows(
        conn.execute(
            "SELECT slug, title FROM wiki_pages WHERE body LIKE ? AND slug<>? ORDER BY title",
            (f"%[[{slug}%", slug),
        )
    )
    p["revision_count"] = conn.execute(
        "SELECT COUNT(*) FROM wiki_revisions WHERE page_id=?", (p["id"],)
    ).fetchone()[0]
    return p


def _index_wiki(conn, page: dict) -> None:
    conn.execute("DELETE FROM wiki_fts WHERE slug=?", (page["slug"],))
    conn.execute(
        "INSERT INTO wiki_fts(slug, title, body, tags) VALUES (?,?,?,?)",
        (page["slug"], page["title"], page["body"], page["tags"]),
    )


def save_wiki(conn, slug: str | None, title: str, body: str, author: str,
              tags=None, department: str | None = None, append: bool = False,
              folder: str | None = None) -> dict:
    """Create or update a page. `folder` (slash path) files it; None leaves the
    existing folder untouched on update and means unfiled on create."""
    if not title and not slug:
        raise BadRequest("title or slug required")
    slug = slug or slugify(title)
    nf = norm_folder(folder) if folder is not None else None
    ex = _one(conn.execute("SELECT * FROM wiki_pages WHERE slug=?", (slug,)))
    ts = now()
    if ex:
        new_body = (ex["body"].rstrip("\n") + "\n\n" + body) if append else body
        conn.execute(
            "INSERT INTO wiki_revisions(page_id, title, body, tags, author, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (ex["id"], ex["title"], ex["body"], ex["tags"], ex["updated_by"] or ex["author"], ex["updated_at"]),
        )
        conn.execute(
            "UPDATE wiki_pages SET title=?, body=?, tags=?, department=?, folder=?, updated_by=?, updated_at=? "
            "WHERE slug=?",
            (title or ex["title"], new_body, _tags(tags) if tags is not None else ex["tags"],
             department if department is not None else ex["department"],
             nf if nf is not None else (ex.get("folder") or ""), author, ts, slug),
        )
        kind = "wiki.update"
    else:
        conn.execute(
            "INSERT INTO wiki_pages(slug, title, body, tags, department, folder, author, updated_by, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (slug, title or slug, body, _tags(tags), department or "", nf or "", author, author, ts, ts),
        )
        kind = "wiki.create"
    page = _one(conn.execute("SELECT * FROM wiki_pages WHERE slug=?", (slug,)))
    _index_wiki(conn, page)
    log_activity(conn, author, kind, f"wiki:{slug}", f"{kind.split('.')[1]}d wiki page “{page['title']}”")
    touch_agent(conn, author)
    conn.commit()
    return get_wiki(conn, slug)


def move_wiki(conn, slug: str, folder: str | None, actor: str) -> dict:
    """Re-file a page into `folder` ('' or None = unfiled). The slug — and so every
    [[wikilink]], backlink and FTS entry — is unchanged; only the location moves."""
    p = _one(conn.execute("SELECT id, title, folder FROM wiki_pages WHERE slug=?", (slug,)))
    if not p:
        raise NotFound(f"wiki page {slug!r} not found")
    nf = norm_folder(folder)
    old = p.get("folder") or ""
    if nf != old:
        conn.execute("UPDATE wiki_pages SET folder=?, updated_by=?, updated_at=? WHERE id=?",
                     (nf, actor, now(), p["id"]))
        log_activity(conn, actor, "wiki.move", f"wiki:{slug}",
                     f"moved wiki page “{p['title']}” {old or '(unfiled)'} → {nf or '(unfiled)'}")
        touch_agent(conn, actor)
        conn.commit()
    return get_wiki(conn, slug)


def delete_wiki(conn, slug: str, actor: str) -> None:
    p = _one(conn.execute("SELECT * FROM wiki_pages WHERE slug=?", (slug,)))
    if not p:
        raise NotFound(f"wiki page {slug!r} not found")
    conn.execute("DELETE FROM wiki_revisions WHERE page_id=?", (p["id"],))
    conn.execute("DELETE FROM wiki_pages WHERE id=?", (p["id"],))
    conn.execute("DELETE FROM wiki_fts WHERE slug=?", (slug,))
    log_activity(conn, actor, "wiki.delete", f"wiki:{slug}", f"deleted wiki page “{p['title']}”")
    conn.commit()


def wiki_revisions(conn, slug: str) -> list[dict]:
    p = get_wiki(conn, slug)
    return _rows(conn.execute(
        "SELECT id, title, tags, author, created_at, length(body) AS size FROM wiki_revisions "
        "WHERE page_id=? ORDER BY id DESC", (p["id"],)))


def wiki_revision(conn, rev_id: int) -> dict:
    r = _one(conn.execute("SELECT * FROM wiki_revisions WHERE id=?", (rev_id,)))
    if not r:
        raise NotFound("revision not found")
    return r


def search_wiki(conn, q: str, limit: int = 20) -> list[dict]:
    if not q.strip():
        return []
    rows = _rows(conn.execute(
        "SELECT slug, title, tags, snippet(wiki_fts, '[', ']', '…', -1, 24) AS snippet "
        "FROM wiki_fts WHERE wiki_fts MATCH ? LIMIT ?",
        (_fts_query(q), limit)))
    for r in rows:
        meta = _one(conn.execute(
            "SELECT department, folder, author, updated_at FROM wiki_pages WHERE slug=?", (r["slug"],)))
        r.update(meta or {})
    return rows


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------

def _index_ticket(conn, t: dict) -> None:
    conn.execute("DELETE FROM ticket_fts WHERE docid=?", (t["id"],))
    conn.execute(
        "INSERT INTO ticket_fts(docid, title, body, tags, result) VALUES (?,?,?,?,?)",
        (t["id"], t["title"], t["body"], t["tags"], t["result"]),
    )


def list_tickets(conn, status: str | list[str] | None = None, assignee: str | None = None,
                 department: str | None = None, type_: str | None = None,
                 requester: str | None = None,
                 include_closed: bool = False, limit: int = 200,
                 parent_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM tickets WHERE 1=1"
    args: list[Any] = []
    if status:
        sts = [status] if isinstance(status, str) else list(status)
        sql += f" AND status IN ({','.join('?' * len(sts))})"
        args += sts
    elif not include_closed:
        sql += " AND status NOT IN ('done','wontfix')"
    if assignee is not None:
        sql += " AND assignee=?"
        args.append(assignee)
    if requester is not None:
        sql += " AND requester=?"
        args.append(requester)
    if department:
        sql += " AND department=?"
        args.append(department)
    if type_:
        sql += " AND type=?"
        args.append(type_)
    if parent_id is not None:
        sql += " AND parent_id=?"
        args.append(parent_id)
    sql += " ORDER BY CASE priority WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END, " \
           "updated_at DESC LIMIT ?"
    args.append(limit)
    rows = _rows(conn.execute(sql, args))
    for t in rows:
        t["comment_count"] = conn.execute(
            "SELECT COUNT(*) FROM ticket_comments WHERE ticket_id=?", (t["id"],)).fetchone()[0]
    return rows


_TICKET_BRIEF = "id, title, type, status, priority, assignee, requester, parent_id, updated_at"


def _ticket_brief(conn, tid: int) -> dict | None:
    return _one(conn.execute(f"SELECT {_TICKET_BRIEF} FROM tickets WHERE id=?", (tid,)))


def _ancestor_ids(conn, tid: int) -> list[int]:
    """Ids walking up the parent chain from (excluding) tid. Cycle-safe."""
    seen: list[int] = []
    cur = _one(conn.execute("SELECT parent_id FROM tickets WHERE id=?", (tid,)))
    while cur and cur.get("parent_id") is not None:
        pid = cur["parent_id"]
        if pid in seen or pid == tid:
            break  # defensive: existing cycle in data; stop walking
        seen.append(pid)
        cur = _one(conn.execute("SELECT parent_id FROM tickets WHERE id=?", (pid,)))
    return seen


def _check_parent(conn, tid: int | None, parent_id: int) -> None:
    """Validate a parent assignment: parent exists, no self/cycle."""
    if tid is not None and parent_id == tid:
        raise BadRequest("a ticket cannot be its own parent")
    if not _one(conn.execute("SELECT id FROM tickets WHERE id=?", (parent_id,))):
        raise NotFound(f"parent ticket #{parent_id} not found")
    if tid is not None and tid in ([parent_id] + _ancestor_ids(conn, parent_id)):
        raise BadRequest(f"setting parent #{parent_id} would create a cycle")


def open_children(conn, tid: int) -> list[dict]:
    """Direct children of tid that are not closed (done/wontfix)."""
    return _rows(conn.execute(
        f"SELECT {_TICKET_BRIEF} FROM tickets WHERE parent_id=? "
        "AND status NOT IN ('done','wontfix') ORDER BY id", (tid,)))


def ticket_tree(conn, tid: int) -> dict:
    """The full hierarchy containing tid: resolved to its root, children nested.

    Plain recursive walks (no CTEs/window functions) so it runs on SQLite 3.7.
    """
    get_ticket(conn, tid)  # 404 if missing
    ancestors = _ancestor_ids(conn, tid)
    root_id = ancestors[-1] if ancestors else tid

    def build(node_id: int, depth: int = 0) -> dict:
        node = _ticket_brief(conn, node_id) or {}
        kids = _rows(conn.execute(
            f"SELECT id FROM tickets WHERE parent_id=? ORDER BY id", (node_id,)))
        node["children"] = [build(k["id"], depth + 1) for k in kids] if depth < 10 else []
        return node

    return {"focus": tid, "root": build(root_id)}


def get_ticket(conn, tid: int) -> dict:
    t = _one(conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)))
    if not t:
        raise NotFound(f"ticket #{tid} not found")
    t["comments"] = _rows(conn.execute(
        "SELECT * FROM ticket_comments WHERE ticket_id=? ORDER BY id", (tid,)))
    if t.get("parent_id") is not None:
        t["parent"] = _ticket_brief(conn, t["parent_id"])
    t["children"] = _rows(conn.execute(
        f"SELECT {_TICKET_BRIEF} FROM tickets WHERE parent_id=? ORDER BY id", (tid,)))
    t["links"] = ticket_links(conn, tid)
    return t


LINK_KINDS = ("wiki", "question", "ticket", "url")


def ticket_links(conn, tid: int) -> list[dict]:
    links = _rows(conn.execute(
        "SELECT * FROM ticket_links WHERE ticket_id=? ORDER BY id", (tid,)))
    for ln in links:  # attach display titles so readers need no second lookup
        if ln["kind"] == "wiki":
            row = _one(conn.execute("SELECT title FROM wiki_pages WHERE slug=?", (ln["ref"],)))
            ln["title"] = row["title"] if row else "(deleted page)"
        elif ln["kind"] == "question":
            row = _one(conn.execute("SELECT title FROM questions WHERE id=?", (ln["ref"],)))
            ln["title"] = row["title"] if row else "(deleted question)"
        elif ln["kind"] == "ticket":
            row = _one(conn.execute("SELECT title FROM tickets WHERE id=?", (ln["ref"],)))
            ln["title"] = row["title"] if row else "(deleted ticket)"
    return links


def link_ticket(conn, tid: int, kind: str, ref: str, actor: str, note: str = "") -> dict:
    """Attach context to a ticket: a wiki page, question, ticket, or URL.

    Validates the referent exists (except url). Idempotent: re-linking the
    same (kind, ref) returns the ticket unchanged.
    """
    get_ticket(conn, tid)  # 404 if missing
    if kind not in LINK_KINDS:
        raise BadRequest(f"kind must be one of {LINK_KINDS}")
    ref = str(ref).strip()
    if not ref:
        raise BadRequest("ref required")
    if kind == "wiki":
        if not _one(conn.execute("SELECT 1 FROM wiki_pages WHERE slug=?", (ref,))):
            raise BadRequest(f"wiki page '{ref}' not found")
    elif kind == "question":
        if not ref.isdigit() or not _one(conn.execute("SELECT 1 FROM questions WHERE id=?", (ref,))):
            raise BadRequest(f"question #{ref} not found")
    elif kind == "ticket":
        if not ref.isdigit() or not _one(conn.execute("SELECT 1 FROM tickets WHERE id=?", (ref,))):
            raise BadRequest(f"ticket #{ref} not found")
        if int(ref) == tid:
            raise BadRequest("a ticket cannot link to itself")
    cur = conn.execute(
        "INSERT OR IGNORE INTO ticket_links(ticket_id, kind, ref, note, created_by, created_at) "
        "VALUES (?,?,?,?,?,?)", (tid, kind, ref, note or "", actor or "", now()))
    if cur.rowcount:
        log_activity(conn, actor, "ticket.link", f"ticket:{tid}",
                     f"linked {kind}:{ref} to ticket #{tid}")
        touch_agent(conn, actor)
    conn.commit()
    return get_ticket(conn, tid)


def unlink_ticket(conn, tid: int, kind: str, ref: str, actor: str) -> dict:
    get_ticket(conn, tid)
    cur = conn.execute("DELETE FROM ticket_links WHERE ticket_id=? AND kind=? AND ref=?",
                       (tid, kind, str(ref).strip()))
    if not cur.rowcount:
        raise NotFound(f"no {kind}:{ref} link on ticket #{tid}")
    log_activity(conn, actor, "ticket.unlink", f"ticket:{tid}",
                 f"unlinked {kind}:{ref} from ticket #{tid}")
    conn.commit()
    return get_ticket(conn, tid)


def create_ticket(conn, title: str, body: str, requester: str, type_: str = "task",
                  priority: str = "p2", department: str = "", assignee: str = "",
                  tags=None, due_at: str | None = None,
                  parent_id: int | None = None) -> dict:
    if not title.strip():
        raise BadRequest("title required")
    if type_ not in TICKET_TYPES:
        raise BadRequest(f"type must be one of {TICKET_TYPES}")
    if priority not in PRIORITIES:
        raise BadRequest(f"priority must be one of {PRIORITIES}")
    if parent_id is not None:
        _check_parent(conn, None, parent_id)
    ts = now()
    cur = conn.execute(
        "INSERT INTO tickets(title, body, type, status, priority, department, assignee, requester, "
        "tags, due_at, parent_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (title.strip(), body or "", type_, "open", priority, department or "", assignee or "",
         requester or "", _tags(tags), due_at, parent_id, ts, ts),
    )
    tid = cur.lastrowid
    t = _one(conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)))
    _index_ticket(conn, t)
    log_activity(conn, requester, "ticket.create", f"ticket:{tid}",
                 f"opened ticket #{tid} “{title.strip()}”" + (f" → {assignee}" if assignee else ""))
    touch_agent(conn, requester)
    conn.commit()
    return get_ticket(conn, tid)


def update_ticket(conn, tid: int, actor: str, **fields) -> dict:
    t = _one(conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)))
    if not t:
        raise NotFound(f"ticket #{tid} not found")
    allowed = {"title", "body", "type", "status", "priority", "department", "assignee",
               "tags", "result", "due_at", "parent_id"}
    data = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "status" in data and data["status"] not in TICKET_STATUSES:
        raise BadRequest(f"status must be one of {TICKET_STATUSES}")
    if "priority" in data and data["priority"] not in PRIORITIES:
        raise BadRequest(f"priority must be one of {PRIORITIES}")
    if "type" in data and data["type"] not in TICKET_TYPES:
        raise BadRequest(f"type must be one of {TICKET_TYPES}")
    if "parent_id" in data:
        pid = data["parent_id"]
        if pid in ("", "none", "null", 0, "0"):
            data["parent_id"] = None  # explicit detach
        else:
            data["parent_id"] = int(pid)
            _check_parent(conn, tid, data["parent_id"])
    if "tags" in data:
        data["tags"] = _tags(data["tags"])
    ts = now()
    if "status" in data and data["status"] != t["status"]:
        conn.execute(
            "INSERT INTO ticket_comments(ticket_id, author, kind, body, created_at) VALUES (?,?,?,?,?)",
            (tid, actor, "status", f"{t['status']} → {data['status']}", ts))
        log_activity(conn, actor, "ticket.status", f"ticket:{tid}",
                     f"moved ticket #{tid} to {data['status']}")
        if data["status"] in ("done", "wontfix"):
            data["closed_at"] = ts
        elif t["closed_at"]:
            data["closed_at"] = None
    if "assignee" in data and data["assignee"] != t["assignee"]:
        conn.execute(
            "INSERT INTO ticket_comments(ticket_id, author, kind, body, created_at) VALUES (?,?,?,?,?)",
            (tid, actor, "assign", f"assigned to {data['assignee'] or '(nobody)'}", ts))
        log_activity(conn, actor, "ticket.assign", f"ticket:{tid}",
                     f"assigned ticket #{tid} to {data['assignee'] or 'nobody'}")
    if not data:
        return get_ticket(conn, tid)
    data["updated_at"] = ts
    sets = ", ".join(f"{k}=?" for k in data)
    conn.execute(f"UPDATE tickets SET {sets} WHERE id=?", (*data.values(), tid))
    t = _one(conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)))
    _index_ticket(conn, t)
    touch_agent(conn, actor)
    conn.commit()
    out = get_ticket(conn, tid)
    if data.get("status") in ("done", "wontfix"):
        kids = open_children(conn, tid)
        if kids:
            out["open_children_warning"] = kids
    return out


def claim_ticket(conn, tid: int, actor: str) -> dict:
    """Atomically take an unassigned open ticket."""
    cur = conn.execute(
        "UPDATE tickets SET assignee=?, status='in_progress', updated_at=? "
        "WHERE id=? AND (assignee='' OR assignee=?) AND status IN ('open','blocked')",
        (actor, now(), tid, actor))
    if cur.rowcount == 0:
        conn.rollback()
        raise BadRequest(f"ticket #{tid} is not claimable (already assigned or not open)")
    conn.execute(
        "INSERT INTO ticket_comments(ticket_id, author, kind, body, created_at) VALUES (?,?,?,?,?)",
        (tid, actor, "assign", f"claimed by {actor}", now()))
    log_activity(conn, actor, "ticket.claim", f"ticket:{tid}", f"claimed ticket #{tid}")
    touch_agent(conn, actor)
    conn.commit()
    return get_ticket(conn, tid)


def comment_ticket(conn, tid: int, author: str, body: str) -> dict:
    if not body.strip():
        raise BadRequest("comment body required")
    get_ticket(conn, tid)
    ts = now()
    conn.execute(
        "INSERT INTO ticket_comments(ticket_id, author, kind, body, created_at) VALUES (?,?,?,?,?)",
        (tid, author, "comment", body, ts))
    conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, tid))
    log_activity(conn, author, "ticket.comment", f"ticket:{tid}", f"commented on ticket #{tid}")
    touch_agent(conn, author)
    conn.commit()
    return get_ticket(conn, tid)


def search_tickets(conn, q: str, limit: int = 20) -> list[dict]:
    if not q.strip():
        return []
    ids = [r[0] for r in conn.execute(
        "SELECT docid FROM ticket_fts WHERE ticket_fts MATCH ? LIMIT ?", (_fts_query(q), limit))]
    if not ids:
        return []
    return _rows(conn.execute(
        f"SELECT id, title, status, priority, type, assignee, department, tags, updated_at "
        f"FROM tickets WHERE id IN ({','.join('?' * len(ids))})", ids))


# --------------------------------------------------------------------------
# Questions (Q&A)
# --------------------------------------------------------------------------

def _score(conn, target_type: str, target_id: int) -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(value),0) FROM votes WHERE target_type=? AND target_id=?",
        (target_type, target_id)).fetchone()[0])


def _index_question(conn, kind: str, ref_id: int, title: str, body: str, tags: str) -> None:
    conn.execute("DELETE FROM question_fts WHERE kind=? AND ref_id=?", (kind, str(ref_id)))
    conn.execute(
        "INSERT INTO question_fts(kind, ref_id, title, body, tags) VALUES (?,?,?,?,?)",
        (kind, str(ref_id), title, body, tags))


def list_questions(conn, status: str | None = None, tag: str | None = None,
                   department: str | None = None, unanswered: bool = False,
                   limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM questions WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if tag:
        sql += " AND (','||tags||',') LIKE ?"
        args.append(f"%,{tag},%")
    if department:
        sql += " AND department=?"
        args.append(department)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    qs = _rows(conn.execute(sql, args))
    out = []
    for q in qs:
        q["score"] = _score(conn, "question", q["id"])
        q["answer_count"] = conn.execute(
            "SELECT COUNT(*) FROM answers WHERE question_id=?", (q["id"],)).fetchone()[0]
        if unanswered and q["answer_count"]:
            continue
        out.append(q)
    return out


def get_question(conn, qid: int, actor: str = "", count_view: bool = False) -> dict:
    q = _one(conn.execute("SELECT * FROM questions WHERE id=?", (qid,)))
    if not q:
        raise NotFound(f"question #{qid} not found")
    if count_view:
        conn.execute("UPDATE questions SET views=views+1 WHERE id=?", (qid,))
        conn.commit()
        q["views"] += 1
    q["score"] = _score(conn, "question", qid)
    q["my_vote"] = _my_vote(conn, "question", qid, actor)
    q["comments"] = _rows(conn.execute(
        "SELECT * FROM question_comments WHERE target_type='question' AND target_id=? ORDER BY id", (qid,)))
    answers = _rows(conn.execute("SELECT * FROM answers WHERE question_id=? ORDER BY id", (qid,)))
    for a in answers:
        a["score"] = _score(conn, "answer", a["id"])
        a["accepted"] = (a["id"] == q["accepted_answer_id"])
        a["my_vote"] = _my_vote(conn, "answer", a["id"], actor)
        a["comments"] = _rows(conn.execute(
            "SELECT * FROM question_comments WHERE target_type='answer' AND target_id=? ORDER BY id",
            (a["id"],)))
    # accepted first, then by score desc, then oldest
    answers.sort(key=lambda a: (not a["accepted"], -a["score"], a["id"]))
    q["answers"] = answers
    return q


def _my_vote(conn, target_type: str, target_id: int, voter: str) -> int:
    if not voter:
        return 0
    r = conn.execute(
        "SELECT value FROM votes WHERE target_type=? AND target_id=? AND voter=?",
        (target_type, target_id, voter)).fetchone()
    return int(r[0]) if r else 0


def ask_question(conn, title: str, body: str, author: str, tags=None, department: str = "") -> dict:
    if not title.strip():
        raise BadRequest("title required")
    ts = now()
    cur = conn.execute(
        "INSERT INTO questions(title, body, author, tags, department, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (title.strip(), body or "", author, _tags(tags), department or "", "open", ts, ts))
    qid = cur.lastrowid
    _index_question(conn, "question", qid, title, body or "", _tags(tags))
    log_activity(conn, author, "question.ask", f"question:{qid}", f"asked “{title.strip()}”")
    touch_agent(conn, author)
    conn.commit()
    return get_question(conn, qid, author)


def update_question(conn, qid: int, actor: str, **fields) -> dict:
    q = _one(conn.execute("SELECT * FROM questions WHERE id=?", (qid,)))
    if not q:
        raise NotFound(f"question #{qid} not found")
    allowed = {"title", "body", "tags", "department", "status"}
    data = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "status" in data and data["status"] not in QUESTION_STATUSES:
        raise BadRequest(f"status must be one of {QUESTION_STATUSES}")
    if "tags" in data:
        data["tags"] = _tags(data["tags"])
    if data:
        data["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in data)
        conn.execute(f"UPDATE questions SET {sets} WHERE id=?", (*data.values(), qid))
        q = _one(conn.execute("SELECT * FROM questions WHERE id=?", (qid,)))
        _index_question(conn, "question", qid, q["title"], q["body"], q["tags"])
        log_activity(conn, actor, "question.edit", f"question:{qid}", f"edited question #{qid}")
        conn.commit()
    return get_question(conn, qid, actor)


def answer_question(conn, qid: int, author: str, body: str) -> dict:
    if not body.strip():
        raise BadRequest("answer body required")
    q = _one(conn.execute("SELECT * FROM questions WHERE id=?", (qid,)))
    if not q:
        raise NotFound(f"question #{qid} not found")
    if q["status"] == "closed":
        raise BadRequest("question is closed")
    ts = now()
    cur = conn.execute(
        "INSERT INTO answers(question_id, author, body, created_at, updated_at) VALUES (?,?,?,?,?)",
        (qid, author, body, ts, ts))
    aid = cur.lastrowid
    conn.execute("UPDATE questions SET updated_at=? WHERE id=?", (ts, qid))
    _index_question(conn, "answer", aid, q["title"], body, q["tags"])
    log_activity(conn, author, "question.answer", f"question:{qid}",
                 f"answered “{q['title']}” (answer #{aid})")
    touch_agent(conn, author)
    conn.commit()
    return get_question(conn, qid, author)


def edit_answer(conn, aid: int, actor: str, body: str) -> dict:
    a = _one(conn.execute("SELECT * FROM answers WHERE id=?", (aid,)))
    if not a:
        raise NotFound(f"answer #{aid} not found")
    ts = now()
    conn.execute("UPDATE answers SET body=?, updated_at=? WHERE id=?", (body, ts, aid))
    conn.execute("UPDATE questions SET updated_at=? WHERE id=?", (ts, a["question_id"]))
    q = _one(conn.execute("SELECT * FROM questions WHERE id=?", (a["question_id"],)))
    _index_question(conn, "answer", aid, q["title"], body, q["tags"])
    log_activity(conn, actor, "question.edit", f"question:{a['question_id']}", f"edited answer #{aid}")
    conn.commit()
    return get_question(conn, a["question_id"], actor)


def vote(conn, target_type: str, target_id: int, voter: str, value: int) -> dict:
    if target_type not in ("question", "answer"):
        raise BadRequest("target_type must be question|answer")
    if value not in (-1, 0, 1):
        raise BadRequest("value must be -1, 0 or 1")
    if not voter:
        raise BadRequest("voter identity required")
    table = "questions" if target_type == "question" else "answers"
    row = _one(conn.execute(f"SELECT * FROM {table} WHERE id=?", (target_id,)))
    if not row:
        raise NotFound(f"{target_type} #{target_id} not found")
    if row["author"] == voter:
        raise BadRequest("you cannot vote on your own post")
    conn.execute("DELETE FROM votes WHERE target_type=? AND target_id=? AND voter=?",
                 (target_type, target_id, voter))
    if value != 0:
        conn.execute(
            "INSERT INTO votes(target_type, target_id, voter, value, created_at) VALUES (?,?,?,?,?)",
            (target_type, target_id, voter, value, now()))
    qid = target_id if target_type == "question" else row["question_id"]
    log_activity(conn, voter, "question.vote", f"question:{qid}",
                 f"{'up' if value > 0 else 'down' if value < 0 else 'un'}voted {target_type} #{target_id}")
    touch_agent(conn, voter)
    conn.commit()
    return {"target_type": target_type, "target_id": target_id,
            "score": _score(conn, target_type, target_id), "my_vote": value}


def accept_answer(conn, qid: int, aid: int | None, actor: str) -> dict:
    q = _one(conn.execute("SELECT * FROM questions WHERE id=?", (qid,)))
    if not q:
        raise NotFound(f"question #{qid} not found")
    if actor not in (q["author"], ADMIN_ACTOR):
        raise BadRequest(
            f"only the question author ({q['author']}) or {ADMIN_ACTOR} may accept/un-accept "
            f"answers on question #{qid}; if you disagree with an answer, comment instead")
    if aid is not None:
        a = _one(conn.execute("SELECT * FROM answers WHERE id=? AND question_id=?", (aid, qid)))
        if not a:
            raise NotFound(f"answer #{aid} not on question #{qid}")
    conn.execute(
        "UPDATE questions SET accepted_answer_id=?, status=?, updated_at=? WHERE id=?",
        (aid, "answered" if aid else "open", now(), qid))
    log_activity(conn, actor, "question.accept", f"question:{qid}",
                 f"{'accepted answer #' + str(aid) if aid else 'un-accepted answer'} on question #{qid}")
    conn.commit()
    return get_question(conn, qid, actor)


def comment_question(conn, target_type: str, target_id: int, author: str, body: str) -> dict:
    if target_type not in ("question", "answer"):
        raise BadRequest("target_type must be question|answer")
    if not body.strip():
        raise BadRequest("comment body required")
    table = "questions" if target_type == "question" else "answers"
    row = _one(conn.execute(f"SELECT * FROM {table} WHERE id=?", (target_id,)))
    if not row:
        raise NotFound(f"{target_type} #{target_id} not found")
    conn.execute(
        "INSERT INTO question_comments(target_type, target_id, author, body, created_at) VALUES (?,?,?,?,?)",
        (target_type, target_id, author, body, now()))
    qid = target_id if target_type == "question" else row["question_id"]
    conn.execute("UPDATE questions SET updated_at=? WHERE id=?", (now(), qid))
    log_activity(conn, author, "question.comment", f"question:{qid}", f"commented on {target_type} #{target_id}")
    touch_agent(conn, author)
    conn.commit()
    return get_question(conn, qid, author)


def search_questions(conn, q: str, limit: int = 20) -> list[dict]:
    if not q.strip():
        return []
    rows = _rows(conn.execute(
        "SELECT kind, ref_id, title, snippet(question_fts, '[', ']', '…', -1, 24) AS snippet "
        "FROM question_fts WHERE question_fts MATCH ? LIMIT ?", (_fts_query(q), limit)))
    out = []
    seen: set[int] = set()
    for r in rows:
        if r["kind"] == "question":
            qid = int(r["ref_id"])
        else:
            a = _one(conn.execute("SELECT question_id FROM answers WHERE id=?", (int(r["ref_id"]),)))
            if not a:
                continue
            qid = a["question_id"]
        if qid in seen:
            continue
        seen.add(qid)
        q_ = _one(conn.execute("SELECT status, author, tags, updated_at FROM questions WHERE id=?", (qid,)))
        if not q_:
            continue
        out.append({"question_id": qid, "kind": r["kind"], "title": r["title"],
                    "snippet": r["snippet"], **q_,
                    "score": _score(conn, "question", qid)})
    return out


# --------------------------------------------------------------------------
# Cross-cutting
# --------------------------------------------------------------------------

def search_all(conn, q: str, limit: int = 10) -> dict:
    return {"wiki": search_wiki(conn, q, limit),
            "tickets": search_tickets(conn, q, limit),
            "questions": search_questions(conn, q, limit)}


def activity(conn, limit: int = 50, actor: str | None = None) -> list[dict]:
    if actor:
        return _rows(conn.execute(
            "SELECT * FROM activity WHERE actor=? ORDER BY id DESC LIMIT ?", (actor, limit)))
    return _rows(conn.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)))


def ticket_overview(conn, tid: int) -> dict:
    """One investigation's state: the tree around tid + activity scoped to it.

    This is the concise 'where is this investigation' view — everything the
    global overview dumps that is unrelated to tid is omitted.
    """
    tree = ticket_tree(conn, tid)

    def ids(node) -> list[int]:
        return [node["id"]] + [i for c in node["children"] for i in ids(c)]

    all_ids = ids(tree["root"])
    refs = [f"ticket:{i}" for i in all_ids]
    acts = _rows(conn.execute(
        f"SELECT * FROM activity WHERE ref IN ({','.join('?' * len(refs))}) "
        "ORDER BY id DESC LIMIT 30", refs))
    by_status: dict[str, int] = {}
    for i in all_ids:
        s = _one(conn.execute("SELECT status FROM tickets WHERE id=?", (i,)))["status"]
        by_status[s] = by_status.get(s, 0) + 1
    links = []
    for i in all_ids:
        for ln in ticket_links(conn, i):
            links.append({"ticket_id": i, **{k: ln[k] for k in ("kind", "ref", "note")},
                          "title": ln.get("title", "")})
    return {"generated_at": now(), "focus": tid, "tree": tree["root"],
            "tickets_total": len(all_ids), "by_status": by_status,
            "open_total": sum(v for k, v in by_status.items() if k not in ("done", "wontfix")),
            "links": links,
            "recent_activity": acts}


def overview(conn) -> dict:
    def count(sql, *a):
        return int(conn.execute(sql, a).fetchone()[0])
    by_status = {s: count("SELECT COUNT(*) FROM tickets WHERE status=?", s) for s in TICKET_STATUSES}
    return {
        "generated_at": now(),
        "wiki_pages": count("SELECT COUNT(*) FROM wiki_pages"),
        "tickets": by_status,
        "tickets_open_total": sum(v for k, v in by_status.items() if k not in ("done", "wontfix")),
        "unassigned_open": count(
            "SELECT COUNT(*) FROM tickets WHERE assignee='' AND status NOT IN ('done','wontfix')"),
        "questions": count("SELECT COUNT(*) FROM questions"),
        "questions_open": count("SELECT COUNT(*) FROM questions WHERE status='open'"),
        "unanswered": count(
            "SELECT COUNT(*) FROM questions q WHERE NOT EXISTS "
            "(SELECT 1 FROM answers a WHERE a.question_id=q.id)"),
        "answers": count("SELECT COUNT(*) FROM answers"),
        "votes": count("SELECT COUNT(*) FROM votes"),
        "agents": count("SELECT COUNT(*) FROM agents"),
        "departments": count("SELECT COUNT(*) FROM departments"),
        "recent_activity": activity(conn, 15),
        "top_agents": sorted(list_agents(conn), key=lambda a: -a["reputation"])[:5],
    }


def inbox(conn, agent: str) -> dict:
    """Everything an agent should look at when it wakes up."""
    a = _one(conn.execute("SELECT * FROM agents WHERE slug=?", (agent,)))
    dept = a["department"] if a else ""
    mine = list_tickets(conn, assignee=agent)
    review_mine = list_tickets(conn, requester=agent, status="review")
    review_mine = [t for t in review_mine if t["assignee"] != agent]
    unassigned = [t for t in list_tickets(conn, assignee="") if not dept or t["department"] in ("", dept)]
    open_q = [q for q in list_questions(conn, status="open")
              if (not dept or q["department"] in ("", dept))]
    unanswered = [q for q in open_q if q["answer_count"] == 0 and q["author"] != agent]
    my_q_with_answers = [q for q in open_q if q["author"] == agent and q["answer_count"] > 0]
    return {"agent": agent, "department": dept, "my_tickets": mine,
            "awaiting_my_review": review_mine,
            "claimable_tickets": unassigned, "unanswered_questions": unanswered,
            "my_questions_with_new_answers": my_q_with_answers}
