"""Business logic for Swarm Agent. Used by both the CLI (agents) and the
HTTP server (dashboard). Every function takes a sqlite3 connection and
returns plain dicts so results serialise straight to JSON.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from . import db, skill_store

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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _execution_flag(value):
    if type(value) is bool: return int(value)
    if type(value) is int and value in (0, 1): return value
    raise BadRequest("ready_for_execution must be a boolean")


def _worker_count(value):
    if value in (None, ''): return None
    if type(value) is not int or not 1 <= value <= 15:
        raise BadRequest('recommended_workers must be an integer between 1 and 15, or null for automatic staffing')
    return value


def _template_id(conn, value):
    if value in (None, ""): return None
    if type(value) is not int and not (isinstance(value, str) and value.isdigit()): raise BadRequest("invalid agent template")
    try: tid = int(value)
    except (ValueError, TypeError): raise BadRequest("invalid agent template")
    if not conn.execute("SELECT 1 FROM agent_templates WHERE id=? AND enabled=1", (tid,)).fetchone():
        raise BadRequest("agent template must exist and be enabled")
    return tid


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


def _search_terms(q: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z0-9]+", q) if t]


def _like_search(columns: tuple[str, ...], terms: list[str]) -> tuple[str, list[str]]:
    """Build a portable AND-of-tokens / OR-of-fields LIKE predicate."""
    if not terms:
        return "0", []
    groups = []
    args: list[str] = []
    for term in terms:
        groups.append("(" + " OR ".join(
            f"LOWER(COALESCE({column},'')) LIKE ?" for column in columns) + ")")
        args.extend([f"%{term}%"] * len(columns))
    return " AND ".join(groups), args


def _text_snippet(text: str, terms: list[str], width: int = 180) -> str:
    text = " ".join((text or "").split())
    lower = text.lower()
    starts = [lower.find(term) for term in terms if lower.find(term) >= 0]
    start = max(0, (min(starts) if starts else 0) - 50)
    snippet = text[start:start + width]
    if start:
        snippet = "…" + snippet
    if start + width < len(text):
        snippet += "…"
    return snippet


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
        a['runtime'] = _one(conn.execute("SELECT key,ticket_id,state,phase,summary,updated_at FROM agent_runtime WHERE agent_slug=? ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,updated_at DESC LIMIT 1", (a['slug'],)))
    return agents


def get_agent(conn, slug: str) -> dict:
    a = _one(conn.execute("SELECT * FROM agents WHERE slug=?", (slug,)))
    if not a:
        raise NotFound(f"agent {slug!r} not found")
    a["reputation"] = reputation(conn, slug)
    return a


def upsert_agent(conn, slug: str, name: str, **fields) -> dict:
    allowed = {"department", "role", "description", "codebases", "wiki_pages", "agent_file", "instructions"}
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


def ensure_agent(conn, name: str, role: str = "expert", description: str = "",
                 slug: str = "", instructions: str = "", *, commit=True) -> dict:
    """Prefer an existing identity; never overwrite its persona during reuse."""
    if role not in ("orchestrator", "lead", "expert", "generalist"):
        raise BadRequest("execution role must be orchestrator, lead, expert, or generalist")
    if not isinstance(name, str) or not name.strip(): raise BadRequest("agent name is required")
    candidate = slug or slugify(name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", candidate): raise BadRequest("invalid agent slug")
    existing = _one(conn.execute("SELECT * FROM agents WHERE slug=?", (candidate,)))
    if existing and existing["role"].lower() == "human": raise BadRequest("cannot execute as a human agent")
    if not existing:
        existing = _one(conn.execute("SELECT * FROM agents WHERE lower(name)=lower(?) AND lower(role)!='human' ORDER BY id LIMIT 1", (name.strip(),)))
    # An explicit slug is an identity request. Automatic selection may reuse a
    # specialist whose described capabilities substantially match the request.
    if not existing and not slug and description.strip():
        wanted = set(_search_terms(description)) - {"research", "and", "the", "of", "for", "with", "analysis"}
        best, score = None, 0
        for agent in _rows(conn.execute("SELECT * FROM agents WHERE role=? ORDER BY id", (role,))):
            offered = set(_search_terms(agent['name'] + ' ' + agent['description']))
            match = len(wanted & offered) / max(1, len(wanted))
            if match > score: best, score = agent, match
        if score >= .7: existing = best
    if existing: return {**existing, "reused": True}
    stamp = now()
    conn.execute("INSERT INTO agents(slug,name,role,description,instructions,created_at,last_seen) VALUES(?,?,?,?,?,?,?)",
                 (candidate, name.strip(), role, description, instructions, stamp, stamp))
    log_activity(conn, candidate, 'agent.register', 'agent:' + candidate, name.strip() + ' joined the team')
    if commit: conn.commit()
    return {**_one(conn.execute('SELECT * FROM agents WHERE slug=?', (candidate,))), "reused": False}


def orchestrator_agent(conn, *, commit=True) -> dict:
    existing = _one(conn.execute("SELECT * FROM agents WHERE role='orchestrator' ORDER BY id LIMIT 1"))
    if existing: return existing
    reserved = _one(conn.execute("SELECT * FROM agents WHERE slug='orchestrator'"))
    if reserved and reserved['role'].lower() != 'human':
        conn.execute("UPDATE agents SET role='orchestrator' WHERE slug='orchestrator'")
        log_activity(conn, 'orchestrator', 'agent.role', 'agent:orchestrator', 'Assigned the orchestrator role')
        if commit: conn.commit()
        return {**reserved, 'role': 'orchestrator'}
    return ensure_agent(conn, 'Rowan', 'orchestrator',
                        'Build teams, reuse specialists, delegate work, review evidence and synthesize results.',
                        slug='team-orchestrator' if reserved else 'orchestrator', commit=commit)


def resolve_ticket_agent(conn, ticket: dict, *, commit=True) -> dict:
    assigned = (ticket.get('assignee') or '').strip()
    if assigned:
        existing = _one(conn.execute('SELECT * FROM agents WHERE slug=?', (assigned,)))
        agent = existing or ensure_agent(conn, assigned.replace('-', ' ').title(),
                     'orchestrator' if ticket['type'] == 'job' else 'expert', ticket['title'], slug=assigned, commit=False)
        if agent['role'].lower() == 'human': raise BadRequest('cannot execute as a human agent')
    elif ticket['type'] == 'job':
        agent = orchestrator_agent(conn, commit=False)
    else:
        agent = ensure_agent(conn, ticket['title'], 'expert', ticket['title'],
                             commit=False)
    if assigned != agent['slug']:
        conn.execute('UPDATE tickets SET assignee=? WHERE id=?', (agent['slug'], ticket['id']))
        log_activity(conn, agent['slug'], 'ticket.assign', f"ticket:{ticket['id']}", 'Assigned to ' + agent['name'])
    if commit: conn.commit()
    return agent


def _skill_audit(conn, slug: str) -> dict | None:
    return _one(conn.execute("SELECT actor,kind,created_at FROM activity WHERE ref=? AND kind IN ('skill.create','skill.put','skill.enable','skill.delete') ORDER BY id DESC LIMIT 1", ('skill:' + slug,)))


def _skill_usage(conn, slug: str) -> list[dict]:
    return _rows(conn.execute('SELECT agent_slug,last_accessed_at,access_count FROM skill_accesses WHERE slug=? ORDER BY last_accessed_at DESC,agent_slug', (slug,)))


def _skill_record(conn, skill: dict) -> dict:
    audit = _skill_audit(conn, skill['slug'])
    usage = _skill_usage(conn, skill['slug'])
    skill['updated_by'] = audit['actor'] if audit else 'repository'
    skill['updated_at'] = audit['created_at'] if audit else skill['updated_at']
    skill['loaded_by'] = [row['agent_slug'] for row in usage]
    skill['last_used_at'] = usage[0]['last_accessed_at'] if usage else ''
    skill['access_count'] = sum(row['access_count'] for row in usage)
    skill['context_tokens'] = max(1, (len(skill['slug']) + len(skill['description']) + 3) // 4 + 5)
    return skill


def list_skills(conn, *, enabled_only: bool = False) -> list[dict]:
    skills = skill_store.list_skills()
    if enabled_only:
        skills = [skill for skill in skills if skill['enabled']]
    return [_skill_record(conn, skill) for skill in skills]


def get_skill(conn, slug: str) -> dict:
    try:
        return _skill_record(conn, skill_store.get_skill(slug))
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def get_skill_file(conn, slug: str, relative: str) -> dict:
    get_skill(conn, slug)
    try:
        return skill_store.get_file(slug, relative)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def require_skill_manager(conn, actor: str) -> None:
    if actor == ADMIN_ACTOR:
        return
    agent = conn.execute('SELECT role FROM agents WHERE slug=?', (actor,)).fetchone()
    if not agent or agent['role'] != 'orchestrator':
        raise BadRequest('Only the human operator or orchestrator can edit skills')


def _record_skill_write(conn, slug: str, actor: str, title: str, *, created: bool) -> None:
    kind = 'skill.create' if created else 'skill.put'
    summary = ('Created' if created else 'Updated') + f' skill folder {slug}: {title}'
    log_activity(conn, actor, kind, 'skill:' + slug, summary)
    touch_agent(conn, actor)
    conn.commit()


def save_skill(conn, slug: str, title: str, body: str, actor: str, version=None, description: str = '') -> dict:
    require_skill_manager(conn, actor)
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', slug):
        raise BadRequest('invalid skill slug')
    if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip():
        raise BadRequest('skill title and instructions are required')
    if len(body.encode('utf-8')) > skill_store.MAX_FILE_BYTES:
        raise BadRequest('skill instructions are limited to 1 MiB')
    try:
        skill, created = skill_store.save_skill(slug, title, description, body, version)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise BadRequest(str(exc)) from exc
    _record_skill_write(conn, slug, actor, skill['title'], created=created)
    return get_skill(conn, slug)


def write_skill_file(conn, slug: str, relative: str, content: str, actor: str, expected_version=None) -> dict:
    require_skill_manager(conn, actor)
    get_skill(conn, slug)
    try:
        saved = skill_store.write_file(slug, relative, content, expected_version)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise BadRequest(str(exc)) from exc
    _record_skill_write(conn, slug, actor, get_skill(conn, slug)['title'], created=False)
    return saved


def import_skill(conn, slug: str, files: list[dict], actor: str) -> dict:
    require_skill_manager(conn, actor)
    try:
        skill = skill_store.import_skill(slug, files)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise BadRequest(str(exc)) from exc
    _record_skill_write(conn, slug, actor, skill['title'], created=True)
    return get_skill(conn, slug)


def set_skill_enabled(conn, slug: str, enabled: bool, actor: str) -> dict:
    require_skill_manager(conn, actor)
    try:
        skill = skill_store.set_enabled(slug, enabled)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise BadRequest(str(exc)) from exc
    log_activity(conn, actor, 'skill.enable', 'skill:' + slug,
                 ('Enabled' if enabled else 'Disabled') + f' skill folder {slug}')
    touch_agent(conn, actor)
    conn.commit()
    return get_skill(conn, slug)


def delete_skill(conn, slug: str, actor: str) -> dict:
    require_skill_manager(conn, actor)
    try:
        skill = skill_store.get_skill(slug)
        skill_store.delete_skill(slug)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise BadRequest(str(exc)) from exc
    log_activity(conn, actor, 'skill.delete', 'skill:' + slug, f"Deleted skill folder {slug}")
    conn.execute('DELETE FROM skill_accesses WHERE slug=?', (slug,))
    touch_agent(conn, actor)
    conn.commit()
    return {'slug': slug, 'title': skill['title'], 'deleted': True}


def record_skill_access(conn, slug: str, actor: str) -> None:
    if not actor:
        return
    stamp = now()
    row = conn.execute('SELECT access_count FROM skill_accesses WHERE slug=? AND agent_slug=?', (slug, actor)).fetchone()
    if row:
        conn.execute('UPDATE skill_accesses SET last_accessed_at=?,access_count=? WHERE slug=? AND agent_slug=?',
                     (stamp, row['access_count'] + 1, slug, actor))
    else:
        conn.execute('INSERT OR IGNORE INTO skill_accesses(slug,agent_slug,last_accessed_at,access_count) VALUES(?,?,?,1)',
                     (slug, actor, stamp))
    conn.commit()


def children_delivered(conn, tid: int) -> bool:
    return not conn.execute("SELECT 1 FROM tickets WHERE parent_id=? AND (status NOT IN ('review','done','wontfix') OR (status!='wontfix' AND trim(result)='')) LIMIT 1", (tid,)).fetchone()


def children_resolved(conn, tid: int) -> bool:
    return not conn.execute("SELECT 1 FROM tickets WHERE parent_id=? AND status NOT IN ('done','wontfix') LIMIT 1", (tid,)).fetchone()


def shared_skill_context(conn) -> str:
    skills = [skill for skill in list_skills(conn) if skill['enabled']]
    lines = ['ROOT SKILLS (from the repository skills/ folder; available to every agent role). Read relevant instructions with skill_get before acting:']
    lines.extend(f"- {skill['slug']}: {skill['description'] or skill['title']}" for skill in skills)
    lines.append('Every enabled folder skill is available through skill_list and skill_get, including in worker, collaboration, review, and dispatch sessions. Only the human operator and orchestrator can create or edit skills with skill_put; workers should send verified methods to the orchestrator for publication.')
    return '\n'.join(lines)


def seed_shared_skills(conn):
    marker = conn.execute("SELECT value FROM runner_settings WHERE key='skills_filesystem_migrated'").fetchone()
    if marker:
        return
    # Preserve pre-existing database skills by importing them once. From that
    # point on, the root skills/ directory is the source of truth.
    migration_complete = True
    for row in _rows(conn.execute('SELECT slug,title,body FROM shared_skills ORDER BY slug')):
        if skill_store.SLUG_RE.fullmatch(row['slug'] or ''):
            try:
                skill_store.get_skill(row['slug'])
            except FileNotFoundError:
                try:
                    skill_store.save_skill(row['slug'], row['title'], row['title'], row['body'], None)
                except (ValueError, OSError):
                    migration_complete = False
            except ValueError:
                migration_complete = False
    if migration_complete:
        conn.execute("INSERT OR REPLACE INTO runner_settings(key,value) VALUES('skills_filesystem_migrated','1')")
        conn.commit()


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
    if not db.fts4_available(conn, "wiki_fts"):
        return
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
    if db.fts4_available(conn, "wiki_fts"):
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
    if db.fts4_available(conn, "wiki_fts"):
        rows = _rows(conn.execute(
            "SELECT slug, title, tags, snippet(wiki_fts, '[', ']', '…', -1, 24) AS snippet "
            "FROM wiki_fts WHERE wiki_fts MATCH ? LIMIT ?",
            (_fts_query(q), limit)))
    else:
        terms = _search_terms(q)
        where, args = _like_search(("slug", "title", "body", "tags"), terms)
        rows = _rows(conn.execute(
            f"SELECT slug, title, tags, body FROM wiki_pages WHERE {where} LIMIT ?",
            (*args, limit)))
        for row in rows:
            row["snippet"] = _text_snippet(row.pop("body"), terms) or row["title"]
    for r in rows:
        meta = _one(conn.execute(
            "SELECT department, folder, author, updated_at FROM wiki_pages WHERE slug=?", (r["slug"],)))
        r.update(meta or {})
    return rows


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------

def _board_id(value) -> int:
    try:
        if isinstance(value, bool) or str(value) != str(int(value)) or int(value) < 1:
            raise ValueError()
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise BadRequest("board_id must be a positive integer")


def get_ticket_board(conn, board_id: int) -> dict:
    board = _one(conn.execute("SELECT * FROM ticket_boards WHERE id=?", (_board_id(board_id),)))
    if not board:
        raise NotFound(f"ticket board #{board_id} not found")
    board["ticket_count"] = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE board_id=?", (board["id"],)).fetchone()[0]
    return board


def list_ticket_boards(conn) -> list[dict]:
    return _rows(conn.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM tickets t WHERE t.board_id=b.id) AS ticket_count "
        "FROM ticket_boards b ORDER BY b.id=1 DESC, b.name COLLATE NOCASE"))


def save_ticket_board(conn, name: str, actor: str, description: str = "",
                      wiki_slug: str = "", board_id: int | None = None) -> dict:
    if not isinstance(name, str) or not name.strip():
        raise BadRequest("board name required")
    if not isinstance(description, str) or not isinstance(wiki_slug, str):
        raise BadRequest("description and wiki_slug must be text")
    name, wiki_slug = name.strip(), wiki_slug.strip()
    if board_id is not None:
        board_id = get_ticket_board(conn, board_id)["id"]
    if wiki_slug:
        get_wiki(conn, wiki_slug)
    if conn.execute("SELECT id FROM ticket_boards WHERE name=? AND id<>?",
                    (name, board_id or 0)).fetchone():
        raise BadRequest("a board with that name already exists")
    ts = now()
    try:
        if board_id is None:
            cur = conn.execute(
                "INSERT INTO ticket_boards(name, description, wiki_slug, created_at, updated_at) "
                "VALUES (?,?,?,?,?)", (name, description, wiki_slug, ts, ts))
            board_id = cur.lastrowid
            kind = "board.create"
        else:
            conn.execute("UPDATE ticket_boards SET name=?, description=?, wiki_slug=?, updated_at=? WHERE id=?",
                         (name, description, wiki_slug, ts, board_id))
            kind = "board.edit"
        log_activity(conn, actor, kind, f"board:{board_id}", f"saved ticket board “{name}”")
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise BadRequest("a board with that name already exists")
    return get_ticket_board(conn, board_id)


def delete_ticket_board(conn, board_id: int, actor: str) -> None:
    board = get_ticket_board(conn, board_id)
    if board["id"] == 1:
        raise BadRequest("the default board cannot be deleted")
    # Delete only this board's tickets and their attachments; wiki stays shared.
    with conn:
        for table in ("ticket_comments", "ticket_links"):
            conn.execute(f"DELETE FROM {table} WHERE ticket_id IN "
                         "(SELECT id FROM tickets WHERE board_id=?)", (board["id"],))
        conn.execute("DELETE FROM ticket_links WHERE kind='ticket' AND ref IN "
                     "(SELECT CAST(id AS TEXT) FROM tickets WHERE board_id=?)", (board["id"],))
        if db.fts4_available(conn, "ticket_fts"):
            conn.execute("DELETE FROM ticket_fts WHERE docid IN "
                         "(SELECT id FROM tickets WHERE board_id=?)", (board["id"],))
        conn.execute("DELETE FROM tickets WHERE board_id=?", (board["id"],))
        conn.execute("DELETE FROM ticket_boards WHERE id=?", (board["id"],))
        log_activity(conn, actor, "board.delete", f"board:{board['id']}",
                     f"deleted ticket board “{board['name']}” and {board['ticket_count']} tickets")


def _index_ticket(conn, t: dict) -> None:
    if not db.fts4_available(conn, "ticket_fts"):
        return
    conn.execute("DELETE FROM ticket_fts WHERE docid=?", (t["id"],))
    conn.execute(
        "INSERT INTO ticket_fts(docid, title, body, tags, result) VALUES (?,?,?,?,?)",
        (t["id"], t["title"], t["body"], t["tags"], t["result"]),
    )


def list_tickets(conn, status: str | list[str] | None = None, assignee: str | None = None,
                 department: str | None = None, type_: str | None = None,
                 requester: str | None = None,
                 include_closed: bool = False, limit: int = 200,
                 parent_id: int | None = None, board_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM tickets WHERE 1=1"
    args: list[Any] = []
    if board_id is not None:
        board_id = get_ticket_board(conn, board_id)["id"]
        sql += " AND board_id=?"
        args.append(board_id)
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


_TICKET_BRIEF = "id, title, type, status, priority, assignee, requester, parent_id, board_id, updated_at"


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
    elif kind == "url":
        from urllib.parse import urlsplit
        url = urlsplit(ref)
        if url.scheme.lower() not in ("http", "https") or not url.hostname:
            raise BadRequest("URL links must use http or https and include a hostname")
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
                  parent_id: int | None = None, board_id: int | None = None,
                  ready_for_execution: bool = False,
                  agent_template_id: int | None = None, recommended_workers: int | None = None,
                  collaboration_required: bool | None = None, workspace: str = "") -> dict:
    if not title.strip():
        raise BadRequest("title required")
    if type_ not in TICKET_TYPES:
        raise BadRequest(f"type must be one of {TICKET_TYPES}")
    if priority not in PRIORITIES:
        raise BadRequest(f"priority must be one of {PRIORITIES}")
    if parent_id is not None:
        _check_parent(conn, None, parent_id)
        parent_board = _ticket_brief(conn, parent_id)["board_id"]
        if board_id is None:
            board_id = parent_board
        elif _board_id(board_id) != parent_board:
            raise BadRequest("parent and child tickets must belong to the same board")
    board_id = get_ticket_board(conn, 1 if board_id is None else board_id)["id"]
    ready_for_execution = _execution_flag(ready_for_execution)
    agent_template_id = _template_id(conn, agent_template_id)
    recommended_workers = _worker_count(recommended_workers)
    collaboration_required = _execution_flag(type_ == 'job' if collaboration_required is None else collaboration_required)
    if workspace:
        from .workspace import workspace_path
        try: workspace = workspace_path({"workspace": workspace}, create_default=False)
        except ValueError as exc: raise BadRequest(str(exc))
    ts = now()
    cur = conn.execute(
        "INSERT INTO tickets(title, body, type, status, priority, department, assignee, requester, "
        "tags, due_at, parent_id, board_id, ready_for_execution, agent_template_id, created_at, updated_at, recommended_workers, collaboration_required, workspace) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (title.strip(), body or "", type_, "open", priority, department or "", assignee or "",
         requester or "", _tags(tags), due_at, parent_id, board_id, ready_for_execution,
         agent_template_id, ts, ts, recommended_workers, collaboration_required, workspace or ""),
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
               "tags", "result", "due_at", "parent_id", "board_id", "ready_for_execution", "agent_template_id", "recommended_workers", "collaboration_required", "workspace"}
    data = {k: v for k, v in fields.items() if k in allowed and (v is not None or k in ("agent_template_id", "recommended_workers"))}
    if "status" in data and data["status"] not in TICKET_STATUSES:
        raise BadRequest(f"status must be one of {TICKET_STATUSES}")
    if "priority" in data and data["priority"] not in PRIORITIES:
        raise BadRequest(f"priority must be one of {PRIORITIES}")
    if "type" in data and data["type"] not in TICKET_TYPES:
        raise BadRequest(f"type must be one of {TICKET_TYPES}")
    if "ready_for_execution" in data:
        data["ready_for_execution"] = _execution_flag(data["ready_for_execution"])
    if 'collaboration_required' in data: data['collaboration_required'] = _execution_flag(data['collaboration_required'])
    if "agent_template_id" in data:
        data["agent_template_id"] = _template_id(conn, data["agent_template_id"])
    if "workspace" in data:
        if not isinstance(data["workspace"], str): raise BadRequest("workspace must be a folder path")
        if data["workspace"].strip():
            from .workspace import workspace_path
            try: data["workspace"] = workspace_path({"workspace": data["workspace"]}, create_default=False)
            except ValueError as exc: raise BadRequest(str(exc))
    if 'recommended_workers' in data: data['recommended_workers'] = _worker_count(data['recommended_workers'])
    if "parent_id" in data:
        pid = data["parent_id"]
        if pid in ("", "none", "null", 0, "0"):
            data["parent_id"] = None  # explicit detach
        else:
            data["parent_id"] = int(pid)
            _check_parent(conn, tid, data["parent_id"])
    if "board_id" in data:
        data["board_id"] = get_ticket_board(conn, data["board_id"])["id"]
    board_id = data.get("board_id", t["board_id"])
    parent_id = data.get("parent_id", t["parent_id"])
    if parent_id is not None and _ticket_brief(conn, parent_id)["board_id"] != board_id:
        raise BadRequest("parent and child tickets must belong to the same board")
    if board_id != t["board_id"] and conn.execute(
            "SELECT 1 FROM tickets WHERE parent_id=? LIMIT 1", (tid,)).fetchone():
        raise BadRequest("detach child tickets before moving this ticket to another board")
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


def retry_ticket(conn, tid: int, actor: str) -> dict:
    """Requeue a failed worker turn or final review without bypassing ticket gates."""
    ticket = get_ticket(conn, tid)
    if not ticket["ready_for_execution"]:
        raise BadRequest(f"ticket #{tid} is not ready for agent execution")
    assignee = (ticket.get("assignee") or "").lower()
    agent = _one(conn.execute("SELECT role FROM agents WHERE slug=?", (ticket.get("assignee") or "",)))
    if not assignee or assignee.startswith("human") or (agent and agent["role"].lower() == "human") or "human" in (ticket.get("tags") or "").lower():
        raise BadRequest(f"ticket #{tid} is assigned or tagged for a human and cannot be retried by the agent runner")

    stamp = now()
    agent_retry_requested = 0
    if ticket["status"] == "review":
        run = _one(conn.execute(
            "SELECT id FROM agent_runs WHERE ticket_id=? AND status='completed' AND stage!='collaborate' ORDER BY id DESC LIMIT 1",
            (tid,)))
        if not run:
            raise BadRequest(f"ticket #{tid} has no completed agent run awaiting final review")
        review = _one(conn.execute(
            "SELECT * FROM ticket_reviews WHERE ticket_id=? AND source_run_id=? ORDER BY id DESC LIMIT 1",
            (tid, run["id"])))
        if not review or review["status"] != "failed":
            raise BadRequest(f"ticket #{tid} has no failed final review to retry; queued and waiting reviews run automatically")
        conn.execute("UPDATE ticket_reviews SET status='queued',decision='',reason='',ticket_version=?,attempt_count=0,created_at=?,finished_at='' WHERE id=?", (stamp, stamp, review["id"]))
        retry_kind = "final review"
    elif ticket["status"] in ("open", "in_progress"):
        run = _one(conn.execute(
            "SELECT status,stage FROM agent_runs WHERE ticket_id=? AND stage!='collaborate' ORDER BY id DESC LIMIT 1", (tid,)))
        if not run or run["status"] != "failed":
            if run and run["status"] == "waiting":
                raise BadRequest(f"ticket #{tid} is waiting for teammate replies or child results; it will resume automatically when those arrive")
            raise BadRequest(f"ticket #{tid} has no failed agent run to retry")
        retry_kind = "agent run"
        agent_retry_requested = 1
    else:
        if ticket["type"] == "job" and not children_resolved(conn, tid):
            unresolved = conn.execute(
                "SELECT id FROM tickets WHERE parent_id=? AND status NOT IN ('done','wontfix') ORDER BY id", (tid,)).fetchall()
            ids = ", ".join("#" + str(row["id"]) for row in unresolved)
            raise BadRequest(f"job #{tid} is {ticket['status']} and has unfinished child tickets ({ids}); retry a failed child instead")
        raise BadRequest(f"ticket #{tid} is {ticket['status']}; only failed agent runs or final reviews can be retried")

    conn.execute("UPDATE tickets SET updated_at=?,agent_retry_requested=? WHERE id=?", (stamp, agent_retry_requested, tid))
    log_activity(conn, actor, "ticket.retry", f"ticket:{tid}", f"queued {retry_kind} retry for ticket #{tid}")
    touch_agent(conn, actor)
    conn.commit()
    result = get_ticket(conn, tid)
    result["retry_kind"] = retry_kind
    return result


def ticket_retry_state(conn, tid: int) -> dict:
    """Describe whether a failed ticket run or final review can be retried."""
    ticket = get_ticket(conn, tid)
    assignee = (ticket.get("assignee") or "").lower()
    agent = _one(conn.execute("SELECT role FROM agents WHERE slug=?", (ticket.get("assignee") or "",)))
    human = (not assignee or assignee.startswith("human") or
             (agent and agent["role"].lower() == "human") or
             "human" in (ticket.get("tags") or "").lower())
    if not ticket["ready_for_execution"] or human:
        return {"eligible": False, "kind": "", "reason": "Ticket is not ready for automated execution or is reserved for a human.", "error": ""}
    if ticket["status"] == "review":
        run = _one(conn.execute(
            "SELECT id FROM agent_runs WHERE ticket_id=? AND status='completed' AND stage!='collaborate' ORDER BY id DESC LIMIT 1",
            (tid,)))
        review = _one(conn.execute(
            "SELECT status,reason FROM ticket_reviews WHERE ticket_id=? AND source_run_id=? ORDER BY id DESC LIMIT 1",
            (tid, run["id"]))) if run else None
        if review and review["status"] == "failed":
            return {"eligible": True, "kind": "final review", "reason": "The automated final review failed.", "error": review["reason"]}
    elif ticket["status"] in ("open", "in_progress"):
        run = _one(conn.execute(
            "SELECT status,stage,error FROM agent_runs WHERE ticket_id=? AND stage!='collaborate' ORDER BY id DESC LIMIT 1", (tid,)))
        if run and run["status"] == "failed" and run["stage"] != "collaborate":
            if ticket.get("agent_retry_requested"):
                return {"eligible": False, "kind": "agent run", "reason": "A retry is already queued.", "error": run["error"]}
            return {"eligible": True, "kind": "agent run", "reason": "The latest agent run failed.", "error": run["error"]}
        if run and run["status"] == "waiting":
            return {"eligible": False, "kind": "", "reason": "Waiting for teammate replies or child results; the runner will resume when they arrive.", "error": ""}
    return {"eligible": False, "kind": "", "reason": "There is no failed agent run or final review to retry.", "error": ""}


def comment_ticket(conn, tid: int, author: str, body: str) -> dict:
    if not body.strip():
        raise BadRequest("comment body required")
    get_ticket(conn, tid)
    ts = now()
    conn.execute(
        "INSERT INTO ticket_comments(ticket_id, author, kind, body, created_at) "
        "VALUES (?,?,?,?,?)", (tid, author, "comment", body, ts))
    conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, tid))
    log_activity(conn, author, "ticket.comment", f"ticket:{tid}", f"commented on ticket #{tid}")
    touch_agent(conn, author)
    conn.commit()
    return get_ticket(conn, tid)


def update_ticket_comment(conn, tid: int, comment_id: int, actor: str, body: str) -> dict:
    if not body.strip():
        raise BadRequest("comment body required")
    comment = _one(conn.execute(
        "SELECT * FROM ticket_comments WHERE id=? AND ticket_id=? AND kind='comment'",
        (comment_id, tid)))
    if not comment:
        raise NotFound(f"comment #{comment_id} not found on ticket #{tid}")
    if comment["author"] != actor:
        raise BadRequest("only the comment author may edit this comment")
    ts = now()
    conn.execute("UPDATE ticket_comments SET body=?, updated_at=? WHERE id=?",
                 (body, ts, comment_id))
    conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, tid))
    log_activity(conn, actor, "ticket.comment.edit", f"ticket:{tid}",
                 f"edited a comment on ticket #{tid}")
    touch_agent(conn, actor)
    conn.commit()
    return get_ticket(conn, tid)


def delete_ticket_comment(conn, tid: int, comment_id: int, actor: str) -> dict:
    comment = _one(conn.execute(
        "SELECT * FROM ticket_comments WHERE id=? AND ticket_id=? AND kind='comment'",
        (comment_id, tid)))
    if not comment:
        raise NotFound(f"comment #{comment_id} not found on ticket #{tid}")
    if comment["author"] != actor:
        raise BadRequest("only the comment author may delete this comment")
    ts = now()
    conn.execute("DELETE FROM ticket_comments WHERE id=?", (comment_id,))
    conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, tid))
    log_activity(conn, actor, "ticket.comment.delete", f"ticket:{tid}",
                 f"deleted a comment on ticket #{tid}")
    touch_agent(conn, actor)
    conn.commit()
    return get_ticket(conn, tid)


def search_tickets(conn, q: str, limit: int = 20, board_id: int | None = None) -> list[dict]:
    if not q.strip():
        return []
    if db.fts4_available(conn, "ticket_fts"):
        board_clause, args = "", [_fts_query(q)]
        if board_id is not None:
            board_clause = " AND t.board_id=?"
            args.append(get_ticket_board(conn, board_id)["id"])
        ids = [r[0] for r in conn.execute(
            "SELECT f.docid FROM ticket_fts f JOIN tickets t ON t.id=f.docid "
            "WHERE ticket_fts MATCH ?" + board_clause + " LIMIT ?", (*args, limit))]
        if not ids:
            return []
        return _rows(conn.execute(
            f"SELECT id, title, status, priority, type, assignee, department, tags, board_id, updated_at "
            f"FROM tickets WHERE id IN ({','.join('?' * len(ids))})", ids))

    terms = _search_terms(q)
    where, args = _like_search(("t.title", "t.body", "t.tags", "t.result"), terms)
    if board_id is not None:
        where += " AND t.board_id=?"
        args.append(get_ticket_board(conn, board_id)["id"])
    return _rows(conn.execute(
        "SELECT t.id, t.title, t.status, t.priority, t.type, t.assignee, t.department, "
        f"t.tags, t.board_id, t.updated_at FROM tickets t WHERE {where} LIMIT ?",
        (*args, limit)))


# --------------------------------------------------------------------------
# Questions (Q&A)
# --------------------------------------------------------------------------

def _score(conn, target_type: str, target_id: int) -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(value),0) FROM votes WHERE target_type=? AND target_id=?",
        (target_type, target_id)).fetchone()[0])


def _index_question(conn, kind: str, ref_id: int, title: str, body: str, tags: str) -> None:
    if not db.fts4_available(conn, "question_fts"):
        return
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
    ts = now()
    conn.execute(
        "INSERT INTO question_comments(target_type, target_id, author, body, created_at) "
        "VALUES (?,?,?,?,?)", (target_type, target_id, author, body, ts))
    qid = target_id if target_type == "question" else row["question_id"]
    conn.execute("UPDATE questions SET updated_at=? WHERE id=?", (ts, qid))
    log_activity(conn, author, "question.comment", f"question:{qid}", f"commented on {target_type} #{target_id}")
    touch_agent(conn, author)
    conn.commit()
    return get_question(conn, qid, author)


def _question_id_for_comment(conn, comment: dict) -> int:
    if comment["target_type"] == "question":
        return int(comment["target_id"])
    answer = _one(conn.execute("SELECT question_id FROM answers WHERE id=?",
                               (comment["target_id"],)))
    if not answer:
        raise NotFound("comment target not found")
    return int(answer["question_id"])


def update_question_comment(conn, comment_id: int, actor: str, body: str) -> dict:
    if not body.strip():
        raise BadRequest("comment body required")
    comment = _one(conn.execute("SELECT * FROM question_comments WHERE id=?", (comment_id,)))
    if not comment:
        raise NotFound(f"comment #{comment_id} not found")
    if comment["author"] != actor:
        raise BadRequest("only the comment author may edit this comment")
    qid = _question_id_for_comment(conn, comment)
    ts = now()
    conn.execute("UPDATE question_comments SET body=?, updated_at=? WHERE id=?",
                 (body, ts, comment_id))
    conn.execute("UPDATE questions SET updated_at=? WHERE id=?", (ts, qid))
    log_activity(conn, actor, "question.comment.edit", f"question:{qid}",
                 f"edited a comment on question #{qid}")
    touch_agent(conn, actor)
    conn.commit()
    return get_question(conn, qid, actor)


def delete_question_comment(conn, comment_id: int, actor: str) -> dict:
    comment = _one(conn.execute("SELECT * FROM question_comments WHERE id=?", (comment_id,)))
    if not comment:
        raise NotFound(f"comment #{comment_id} not found")
    if comment["author"] != actor:
        raise BadRequest("only the comment author may delete this comment")
    qid = _question_id_for_comment(conn, comment)
    ts = now()
    conn.execute("DELETE FROM question_comments WHERE id=?", (comment_id,))
    conn.execute("UPDATE questions SET updated_at=? WHERE id=?", (ts, qid))
    log_activity(conn, actor, "question.comment.delete", f"question:{qid}",
                 f"deleted a comment on question #{qid}")
    touch_agent(conn, actor)
    conn.commit()
    return get_question(conn, qid, actor)


def search_questions(conn, q: str, limit: int = 20) -> list[dict]:
    if not q.strip():
        return []
    terms = _search_terms(q)
    if db.fts4_available(conn, "question_fts"):
        rows = _rows(conn.execute(
            "SELECT kind, ref_id, title, snippet(question_fts, '[', ']', '…', -1, 24) AS snippet "
            "FROM question_fts WHERE question_fts MATCH ? LIMIT ?", (_fts_query(q), limit)))
    else:
        q_where, q_args = _like_search(("q.title", "q.body", "q.tags"), terms)
        a_where, a_args = _like_search(("q.title", "a.body", "q.tags"), terms)
        rows = _rows(conn.execute(
            "SELECT kind, ref_id, title, body, tags, question_id FROM ("
            "SELECT 'question' AS kind, CAST(q.id AS TEXT) AS ref_id, q.title AS title, "
            f"q.body AS body, q.tags AS tags, q.id AS question_id FROM questions q WHERE {q_where} "
            "UNION ALL "
            "SELECT 'answer' AS kind, CAST(a.id AS TEXT) AS ref_id, q.title AS title, "
            f"a.body AS body, q.tags AS tags, q.id AS question_id "
            "FROM answers a JOIN questions q ON q.id=a.question_id "
            f"WHERE {a_where}) LIMIT ?", (*q_args, *a_args, limit)))
    out = []
    seen: set[int] = set()
    for r in rows:
        if r.get("question_id") is not None:
            qid = int(r.pop("question_id"))
        elif r["kind"] == "question":
            qid = int(r["ref_id"])
        else:
            a = _one(conn.execute("SELECT question_id FROM answers WHERE id=?", (int(r["ref_id"]),)))
            if not a:
                continue
            qid = a["question_id"]
        snippet = r.get("snippet")
        if snippet is None:
            snippet = _text_snippet(r.pop("body", ""), terms) or r["title"]
        if qid in seen:
            continue
        seen.add(qid)
        q_ = _one(conn.execute("SELECT status, author, tags, updated_at FROM questions WHERE id=?", (qid,)))
        if not q_:
            continue
        out.append({"question_id": qid, "kind": r["kind"], "title": r["title"],
                    "snippet": snippet, **q_,
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
