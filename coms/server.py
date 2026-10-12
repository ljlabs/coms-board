"""Swarm Agent dashboard server (stdlib only).

    python3 -m coms.server [--host 127.0.0.1] [--port 8765]

Serves the web UI from ../web and a JSON API under /api/. Binds to loopback
by default: there is NO authentication, so do not expose it beyond localhost.
Docker LAN access (e.g. from a phone) is opt-in via COMS_TRUSTED_HOSTS;
see devDocs/lan-access.md.
The acting identity for write calls comes from the X-Coms-Agent header
(the UI sends the admin actor by default, see coms.api.ADMIN_ACTOR) or an
`actor` field in the JSON body.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coms import api, db, progress, team  # noqa: E402
from coms.workspace import DEFAULT_WORKSPACE, workspace_path

WEB = Path(__file__).resolve().parent.parent / "web"

# Font MIME types for the vendored KaTeX fonts. Python's mimetypes has no
# entries for these on some platforms (guess_type -> None), which would serve
# them as application/octet-stream; declare them explicitly.
for _ext, _mime in ((".woff2", "font/woff2"), (".woff", "font/woff"), (".ttf", "font/ttf")):
    if not mimetypes.guess_type(f"x{_ext}")[0]:
        mimetypes.add_type(_mime, _ext)


class Handler(BaseHTTPRequestHandler):
    server_version = "swarm-agent/1.0"

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https: http:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    # ---------------------------------------------------------------- helpers
    def _json(self, status: int, data) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers routinely cancel an in-flight poll during navigation.
            # The response cannot be delivered, so don't turn it into a second
            # (also undeliverable) 500 response in _dispatch.
            return

    def _body(self, max_bytes: int = 2 * 1024 * 1024) -> dict:
        try: n = int(self.headers.get("Content-Length") or 0)
        except ValueError: raise api.BadRequest("invalid Content-Length")
        if n < 0 or n > max_bytes:
            raise api.BadRequest(f"JSON body must be at most {max_bytes // (1024 * 1024)} MiB")
        if not n:
            return {}
        if self.headers.get_content_type() != "application/json":
            raise api.BadRequest("Content-Type must be application/json")
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
            if not isinstance(body, dict): raise api.BadRequest("JSON body must be an object")
            return body
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise api.BadRequest(f"invalid JSON body: {e}")

    def _trusted_request(self):
        # Local services are otherwise vulnerable to DNS rebinding and
        # cross-origin requests from websites opened in the same browser.
        # COMS_TRUSTED_HOSTS (comma-separated hostnames/IPs, no ports) opts
        # additional LAN hosts into the same checks; it is empty by default.
        host = self.headers.get("Host", "")
        try:
            authority = urlsplit("//" + host)
            hostname = (authority.hostname or "").lower()
            try:
                local = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                local = hostname == "localhost"
            public_port = int(os.environ.get("COMS_PUBLIC_PORT", self.server.server_port))
            extra = {h.strip().lower() for h in os.environ.get("COMS_TRUSTED_HOSTS", "").split(",") if h.strip()}
            trusted = (local or hostname in extra) and authority.port == public_port
        except ValueError: trusted = False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host: trusted = False
        if self.headers.get("Sec-Fetch-Site") == "cross-site": trusted = False
        if not trusted:
            self._json(403, {"error": "request must come from this local dashboard"})
        return trusted

    def _actor(self, body: dict | None = None) -> str:
        return (body or {}).get("actor") or self.headers.get("X-Coms-Agent") or api.ADMIN_ACTOR

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        f = (WEB / unquote(rel)).resolve()
        if not f.is_relative_to(WEB.resolve()) or not f.is_file():
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
        if not self._trusted_request(): return
        u = urlsplit(self.path)
        if not u.path.startswith("/api/"):
            return self._static(u.path)
        stream = re.fullmatch(r"/api/runner/sessions/([^/]+)/events", u.path)
        if stream:
            return self._orchestrator_events(stream.group(1))
        self._dispatch("GET", u.path, parse_qs(u.query, keep_blank_values=True), {})

    def _orchestrator_events(self, sid):
        """Stream canonical message patches; reconnects begin with a snapshot."""
        from .runner import RUNNER
        from .orchestrator_transcript import wait_for_change
        revision = wait_for_change()
        detail = RUNNER.orchestrator_detail(sid)
        if detail is None: return self._json(404, {"error": "session not found"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(30)
        versions, previous_meta = {}, None
        last_write = time.monotonic()

        def send(event, payload):
            raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            self.wfile.write(f"event: {event}\ndata: {raw}\n\n".encode())
            self.wfile.flush()

        try:
            first = True
            while True:
                if detail is None:
                    send('session-deleted', {'id': sid})
                    break
                messages = detail['messages']
                current = {entry['id']: entry['version'] for entry in messages}
                meta = {key: value for key, value in detail.items() if key not in ('messages', 'logs')}
                meta_signature = json.dumps(meta, sort_keys=True)
                changed = [entry for entry in messages if versions.get(entry['id']) != entry['version']]
                removed = sorted(versions.keys() - current.keys())
                if first:
                    send('snapshot', detail)
                elif changed or removed or meta_signature != previous_meta:
                    send('update', {**meta, 'messages': changed, 'removed': removed})
                elif time.monotonic() - last_write >= 10:
                    self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
                else:
                    revision = wait_for_change(revision)
                    detail = RUNNER.orchestrator_detail(sid)
                    continue
                versions, previous_meta = current, meta_signature
                first = False
                last_write = time.monotonic()
                # Coalesce fast token bursts while keeping delivery interactive.
                time.sleep(0.04)
                revision = wait_for_change(revision)
                detail = RUNNER.orchestrator_detail(sid)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass
        except Exception:
            traceback.print_exc()
        finally:
            conn = getattr(db._local, 'conn', None)
            if conn: conn.close(); db._local.__dict__.clear()

    def do_POST(self):
        if not self._trusted_request(): return
        u = urlsplit(self.path)
        try:
            body = self._body(12 * 1024 * 1024 if u.path == "/api/skills" else 2 * 1024 * 1024)
        except api.BadRequest as e:
            return self._json(400, {"error": str(e)})
        except (ValueError, TypeError, KeyError) as e:
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
        except (ValueError, TypeError, KeyError) as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover
            traceback.print_exc()
            return self._json(500, {"error": "Internal server error; see server logs."})
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
                                                          "codebases", "wiki_pages", "agent_file", "instructions")})


def r_agent(conn, m, mm, q, b, actor):
    if m == "GET":
        a = api.get_agent(conn, mm.group(1))
        a["activity"] = api.activity(conn, 30, mm.group(1))
        a["tickets"] = api.list_tickets(conn, assignee=mm.group(1))
        a['progress'] = progress.snapshot(conn, agent=mm.group(1))
        return a


def r_skills(conn, m, mm, q, b, actor):
    slug = mm.group(1) if mm.lastindex else None
    if m == 'GET':
        if not slug: return api.list_skills(conn)
        return api.get_skill_file(conn, slug, q['file']) if q.get('file') else api.get_skill(conn, slug)
    if m in ('POST', 'PUT'):
        if b.get('action') == 'import':
            return api.import_skill(conn, b.get('slug', ''), b.get('files', []), actor)
        return api.save_skill(conn, slug or b.get('slug', ''), b.get('title', ''), b.get('body', ''), actor,
                              b.get('version'), b.get('description', ''), b.get('trigger_words'))
    if m == 'PATCH' and slug:
        if 'trigger_words' in b:
            return api.set_skill_trigger_words(conn, slug, b['trigger_words'], actor, b.get('version'))
        if b.get('action') == 'create':
            return api.create_skill_entry(conn, slug, b.get('path', ''), b.get('kind', ''), actor,
                                          b.get('tree_version'), b.get('content', ''))
        if b.get('action') == 'move':
            return api.move_skill_entry(conn, slug, b.get('source', ''), b.get('destination', ''), actor,
                                        b.get('tree_version'))
        if 'enabled' in b:
            return api.set_skill_enabled(conn, slug, b['enabled'], actor)
        if 'file' in b and 'content' in b:
            return api.write_skill_file(conn, slug, b['file'], b['content'], actor, b.get('version'))
    if m == 'DELETE' and slug:
        return api.delete_skill(conn, slug, actor)


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
        ticket = api.create_ticket(conn, b.get("title", ""), b.get("body", ""), actor,
                                 b.get("type", "task"), b.get("priority", "p2"), b.get("department", ""),
                                 b.get("assignee", ""), b.get("tags"), b.get("due_at"),
                                 parent_id=int(pid) if pid not in (None, "") else None,
                                 board_id=b.get("board_id"),
                                 ready_for_execution=b.get("ready_for_execution", False),
                                 agent_template_id=b.get("agent_template_id"), recommended_workers=b.get('recommended_workers'), collaboration_required=b.get('collaboration_required'), workspace=b.get('workspace', ''))
        if ticket['ready_for_execution'] and not ticket['assignee']:
            api.resolve_ticket_agent(conn, ticket)
            ticket = api.get_ticket(conn, ticket['id'])
        return ticket


def r_ticket(conn, m, mm, q, b, actor):
    tid = int(mm.group(1))
    if m == "GET":
        ticket = api.get_ticket(conn, tid)
        job = team.job_for_ticket(conn, tid)
        ticket['team_job_id'] = job['id'] if job and job.get('collaboration_required') else None
        return ticket
    if m in ("PATCH", "PUT", "POST"):
        return api.update_ticket(conn, tid, actor, **{k: b.get(k) for k in (
            "title", "body", "type", "status", "priority", "department", "assignee",
            "tags", "result", "due_at", "parent_id", "board_id", "ready_for_execution", "agent_template_id", "recommended_workers", "collaboration_required", "workspace") if k in b})


def r_scheduled_tasks(conn, m, mm, q, b, actor):
    from . import schedules
    if m == "GET": return schedules.list_tasks(conn, include_deleted=q.get("all") == "1")
    if m == "POST":
        try: return schedules.create_task(conn, b, actor)
        except ValueError as exc: raise api.BadRequest(str(exc))


def r_scheduled_task(conn, m, mm, q, b, actor):
    from . import schedules
    task_id = int(mm.group(1))
    if m == "GET": return schedules.get_task(conn, task_id)
    if m in ("PATCH", "PUT", "POST"):
        try: return schedules.update_task(conn, task_id, b, actor)
        except ValueError as exc: raise api.BadRequest(str(exc))
    if m == "DELETE": return schedules.delete_task(conn, task_id, actor)


def r_scheduled_runs(conn, m, mm, q, b, actor):
    from . import schedules
    return schedules.list_runs(conn, int(mm.group(1)), q.get("limit", 100)) if m == "GET" else None


def r_scheduled_task_action(conn, m, mm, q, b, actor):
    from . import schedules
    task_id, action = int(mm.group(1)), mm.group(2)
    if m != "POST": return None
    try:
        if action in ("pause", "resume"):
            return schedules.set_status(conn, task_id, "paused" if action == "pause" else "active", actor)
        if action == "run-now": return schedules.run_now(conn, task_id, actor)
    except ValueError as exc:
        raise api.BadRequest(str(exc))
    return None


def r_ticket_retry(conn, m, mm, q, b, actor):
    if m == "POST":
        return api.retry_ticket(conn, int(mm.group(1)), actor)


def r_teams(conn, m, mm, q, b, actor):
    if m == 'GET':
        return [dict(row) for row in conn.execute("SELECT t.id,t.title,t.status,t.assignee,(SELECT count(*) FROM team_messages WHERE job_id=t.id) AS message_count,(SELECT count(*) FROM team_deliveries d JOIN team_messages m ON m.id=d.message_id WHERE m.job_id=t.id AND d.status IN ('pending','responding')) AS pending_count FROM tickets t WHERE t.type='job' AND t.collaboration_required=1 ORDER BY t.id DESC")]


def r_team(conn, m, mm, q, b, actor):
    job_id = int(mm.group(1))
    if m == 'GET': return team.room(conn, job_id)
    if m == 'POST':
        return team.send(conn, job_id, actor, b.get('to', '*'), b.get('body', ''), kind='note' if b.get('to', '*') == '*' else 'question', question_id=b.get('question_id'), ticket_id=b.get('ticket_id'))


def r_team_stop(conn, m, mm, q, b, actor):
    if m == 'POST': return team.stop(conn, int(mm.group(1)), actor)


def r_runner_settings(conn, m, _mm, q, b, actor):
    if m == "GET":
        values = {r[0]: r[1] for r in conn.execute("SELECT key,value FROM runner_settings")}
        if os.environ.get("COMS_IN_DOCKER") == "1":
            model = os.environ.get("COMS_LLAMACPP_MODEL", "LFM2.5-2.6B-Q8_0.gguf").strip()
            docker_workspace=values.get("workspace", "/workspace")
            try: docker_workspace=workspace_path({"workspace":docker_workspace},create_default=False)
            except ValueError: docker_workspace="/workspace"
            values.update(workspace=docker_workspace,
                          opencode_api_base_url=os.environ.get("COMS_LLAMACPP_BASE_URL", "http://host.docker.internal:11434/v1"),
                          opencode_api_model=model,
                          model_opencode="coms-local/" + model,
                          opencode_api_key="")
        values.setdefault("active_backend", "opencode"); values.setdefault("parallel_agents", "2")
        values.setdefault("workspace", str(DEFAULT_WORKSPACE))
        values.setdefault("opencode_api_base_url", "http://localhost:11434/v1")
        values.setdefault("opencode_api_model", "")
        values["opencode_api_key_set"] = bool(values.pop("opencode_api_key", ""))
        try: workspace_path(values); values["workspace_error"] = ""
        except ValueError as exc: values["workspace_error"] = str(exc)
        from .runner import backend_command
        values["backends"] = [{"id": x, "available": bool(__import__('shutil').which(backend_command(x, values)[0]))} for x in ("codex", "claude", "opencode")]
        return values
    if m in ("PUT", "PATCH", "POST"):
        allowed = {"active_backend", "parallel_agents", "workspace", "codex_command", "claude_command", "opencode_command", "model_codex", "model_claude", "model_opencode", "opencode_api_base_url", "opencode_api_key", "opencode_api_model"}
        if "active_backend" in b and b["active_backend"] not in ("codex", "claude", "opencode"):
            raise api.BadRequest("active_backend must be codex, claude, or opencode")
        if "parallel_agents" in b and not 1 <= int(b["parallel_agents"]) <= 16:
            raise api.BadRequest("parallel_agents must be between 1 and 16")
        if "workspace" in b:
            try: b["workspace"] = workspace_path(b)
            except ValueError as exc: raise api.BadRequest(str(exc))
        if "opencode_api_base_url" in b:
            from .runner import normalize_opencode_api_base_url
            try: b["opencode_api_base_url"] = normalize_opencode_api_base_url(b["opencode_api_base_url"])
            except (TypeError, ValueError) as exc: raise api.BadRequest(str(exc))
        for field in ("opencode_api_key", "opencode_api_model"):
            if field in b and not isinstance(b[field], str): raise api.BadRequest("OpenCode API settings must be strings")
        if "opencode_api_model" in b: b["opencode_api_model"] = b["opencode_api_model"].strip()
        if "clear_opencode_api_key" in b and not isinstance(b["clear_opencode_api_key"], bool):
            raise api.BadRequest("clear_opencode_api_key must be a boolean")
        from .runner import RUNNER, backend_command
        previous = {r[0]: r[1] for r in conn.execute("SELECT key,value FROM runner_settings")}
        previous_backend=previous.get("active_backend","opencode")
        new_backend=b.get("active_backend",previous_backend)
        clear_api_key=b.get("clear_opencode_api_key",False)
        api_key_changed=(clear_api_key and bool(previous.get("opencode_api_key"))) or ("opencode_api_key" in b and b["opencode_api_key"]!=previous.get("opencode_api_key","") and bool(b["opencode_api_key"]))
        opencode_settings_changed=(api_key_changed
            or ("opencode_api_base_url" in b and b["opencode_api_base_url"]!=previous.get("opencode_api_base_url","http://localhost:11434/v1"))
            or ("opencode_api_model" in b and b["opencode_api_model"]!=previous.get("opencode_api_model","")))
        runtime_changed=(new_backend!=previous_backend
            or ("workspace" in b and str(previous.get("workspace",str(DEFAULT_WORKSPACE)))!=str(b["workspace"]))
            or ("model_"+new_backend in b and str(previous.get("model_"+new_backend,""))!=str(b["model_"+new_backend]))
            or (new_backend+"_command" in b and str(previous.get(new_backend+"_command",""))!=str(b[new_backend+"_command"]))
            or (new_backend=="opencode" and opencode_settings_changed))
        for backend in ("codex", "claude", "opencode"):
            if backend + "_command" in b:
                if not isinstance(b[backend + "_command"], str): raise api.BadRequest("backend commands must be strings")
                backend_command(backend, b)  # validate shell quoting before saving
        with RUNNER.lock:
            if RUNNER.active or RUNNER.orchestrator_busy:
                raise api.BadRequest("Wait for active agents to finish before changing runner settings.")
            if "workspace" in b:
                try: b["workspace"] = workspace_path(b,create_default=False)
                except ValueError as exc: raise api.BadRequest(str(exc))
            for c in RUNNER.conns.values(): c.close()
            RUNNER.conns.clear()
            for key, value in b.items():
                if key not in allowed: continue
                if key == "opencode_api_key" and not value: continue
                conn.execute("INSERT OR REPLACE INTO runner_settings(key,value) VALUES(?,?)", (key, str(value)))
            if clear_api_key: conn.execute("DELETE FROM runner_settings WHERE key='opencode_api_key'")
            if runtime_changed:
                current={r[0]:r[1] for r in conn.execute("SELECT key,value FROM runner_settings")}
                active_workspace=workspace_path(current)
                conn.execute("UPDATE orchestrator_sessions SET backend=?,workspace=?,acp_session_id=''",(current.get("active_backend","opencode"),active_workspace))
            conn.commit()
        if (runtime_changed or opencode_settings_changed) and RUNNER.thread and RUNNER.thread.is_alive():
            threading.Thread(target=RUNNER._reset_and_warm_models, daemon=True, name="coms-model-warmup").start()
            if runtime_changed:
                threading.Thread(target=RUNNER._reset_orchestrator, daemon=True, name="coms-orchestrator-warmup").start()
        return {"saved": True}


def r_runner_models(conn, m, _mm, q, b, actor):
    if m == "GET":
        from .runner import RUNNER
        try: return RUNNER.discover_models(q.get("backend", ""))
        except ValueError as e: raise api.BadRequest(str(e))
        except Exception as e: raise api.BadRequest(f"ACP model discovery failed: {e}")


def r_runner_debug(conn, m, _mm, q, b, actor):
    if m != "GET": return None
    where=""; args=[]
    if q.get("ticket"):
        where="WHERE r.ticket_id=?"; args.append(int(q["ticket"]))
    rows=conn.execute("SELECT r.*,t.title AS ticket_title,t.priority,t.status AS ticket_status FROM agent_runs r JOIN tickets t ON t.id=r.ticket_id "+where+" ORDER BY r.id DESC LIMIT 100",args).fetchall()
    runs=[]
    for row in rows:
        item=dict(row)
        # Return the tail, not the first page: old token-by-token transcripts
        # can exceed the limit within a minute and otherwise freeze this view.
        item['message_count'] = conn.execute('SELECT count(*) FROM agent_messages WHERE run_id=?', (row['id'],)).fetchone()[0]
        item['messages']=[dict(x) for x in reversed(conn.execute("SELECT id,role,kind,body,created_at,updated_at FROM agent_messages WHERE run_id=? AND kind!='acp-agent_thought_chunk' AND NOT (kind='review-log' AND body LIKE '%agent_thought_chunk%') ORDER BY id DESC LIMIT 2000",(row['id'],)).fetchall())]
        item['messages_truncated'] = item['message_count'] > len(item['messages'])
        item['reviews']=[dict(x) for x in conn.execute("SELECT id,status,decision,reason,backend,created_at,finished_at FROM ticket_reviews WHERE ticket_id=? AND source_run_id=? ORDER BY id",(row['ticket_id'],row['id']))]
        runs.append(item)
    misc="WHERE run_id IS NULL"; margs=[]
    if q.get("ticket"):
        misc += " AND ticket_id=?"; margs.append(int(q['ticket']))
    loose=[dict(x) for x in conn.execute("SELECT id,ticket_id,role,kind,body,created_at FROM agent_messages "+misc+" ORDER BY id DESC LIMIT 500",margs)]
    reviews_where="WHERE tr.ticket_id=?" if q.get("ticket") else ""
    reviews_args=[int(q['ticket'])] if q.get('ticket') else []
    reviews=[dict(x) for x in conn.execute("SELECT tr.*,t.title AS ticket_title FROM ticket_reviews tr JOIN tickets t ON t.id=tr.ticket_id "+reviews_where+" ORDER BY tr.id DESC LIMIT 200",reviews_args)]
    return {"runs":runs,"unattached_messages":loose,"reviews":reviews}


def r_runner_status(conn, m, _mm, q, b, actor):
    if m != 'GET': return None
    from .runner import RUNNER
    if q.get('activity') == '1':
        data = progress.activity(conn)
        return {kind: data[kind] for kind in ('tickets', 'sessions', 'schedules')}
    state = progress.snapshot(conn, ticket=q.get('ticket'), agent=q.get('agent'))
    settings = RUNNER.settings()
    state['capacity'] = int(settings.get('parallel_agents', '2'))
    state['runner_running'] = bool(RUNNER.thread and RUNNER.thread.is_alive())
    return state


def r_templates(conn, m, _mm, q, b, actor):
    if m == "GET": return [dict(r) for r in conn.execute("SELECT * FROM agent_templates ORDER BY name")]
    if m == "POST":
        name = (b.get("name") or "").strip()
        if not name: raise api.BadRequest("template name required")
        ts = api.now()
        if b.get("backend", "") not in ("", "codex", "claude", "opencode"): raise api.BadRequest("invalid template backend")
        values=(b.get("prompt", ""),b.get("backend", ""),int(_bool(b.get("enabled", True))),ts,name)
        exists=conn.execute("SELECT id FROM agent_templates WHERE name=?",(name,)).fetchone()
        if exists:
            conn.execute("UPDATE agent_templates SET prompt=?,backend=?,enabled=?,updated_at=? WHERE name=?",values)
        else:
            conn.execute("INSERT INTO agent_templates(prompt,backend,enabled,updated_at,name,created_at) VALUES(?,?,?,?,?,?)",values+(ts,))
        conn.commit()
        return dict(conn.execute("SELECT * FROM agent_templates WHERE name=?",(name,)).fetchone())


def r_runner_ticket(conn, m, mm, q, b, actor):
    tid = int(mm.group(1)); api.get_ticket(conn, tid)
    if m == "GET":
        from .runner import RUNNER
        from .ticket_chat import conversation
        return conversation(conn, tid, RUNNER, include_logs=_bool(q.get('include_logs', False)))
    if m == "POST":
        from .runner import RUNNER
        try: return RUNNER.chat(tid, b.get("body", ""))
        except ValueError as e: raise api.BadRequest(str(e))


def r_orchestrator(conn, m, _mm, q, b, actor):
    from .runner import RUNNER
    if m == "GET": return {"projects": RUNNER.orchestrator_projects(), "sessions": RUNNER.orchestrator_sessions()}
    if m == "POST":
        try: return RUNNER.orchestrate(b.get("body", ""), b.get("session_id", "default"))
        except ValueError as e: raise api.BadRequest(str(e))


def r_orchestrator_projects(conn, m, _mm, q, b, actor):
    from .runner import RUNNER
    if m == "GET": return RUNNER.orchestrator_projects()
    if m == "POST":
        try: return RUNNER.create_orchestrator_project(b.get("name", ""), b.get("workspace", ""))
        except ValueError as e: raise api.BadRequest(str(e))


def r_orchestrator_project(conn, m, mm, q, b, actor):
    from .runner import RUNNER
    project_id=int(mm.group(1))
    if m in ("PATCH", "PUT", "POST"):
        try: return RUNNER.update_orchestrator_project(project_id,b.get("workspace"))
        except ValueError as e: raise api.BadRequest(str(e))
    if m=="DELETE":
        if not RUNNER.delete_orchestrator_project(project_id): raise api.NotFound("project not found")
        return {"deleted":project_id}


def r_workspace_folders(_conn, m, _mm, q, b, _actor):
    from .runner import RUNNER
    try:
        if m == "GET": return RUNNER.workspace_folders(q.get("path"))
        if m == "POST": return RUNNER.manage_workspace_folder('create',b.get("path"),b.get("name"))
        if m == "PATCH": return RUNNER.manage_workspace_folder('rename',b.get("path"),b.get("name"))
        if m == "DELETE":
            if b.get("confirm") is not True: raise api.BadRequest("confirm folder deletion")
            return RUNNER.manage_workspace_folder('delete',b.get("path"))
    except ValueError as e: raise api.BadRequest(str(e))


def r_orchestrator_sessions(conn, m, _mm, q, b, actor):
    from .runner import RUNNER
    if m == "GET": return RUNNER.orchestrator_sessions()
    if m == "POST":
        try: return RUNNER.create_orchestrator_session(b.get("title", "New chat"), b.get("project_id"))
        except ValueError as e: raise api.BadRequest(str(e))


def r_orchestrator_stop(conn, m, mm, _q, _b, _actor):
    if m != "POST": return None
    from .runner import RUNNER
    try: return RUNNER.stop_orchestrator(mm.group(1))
    except ValueError as e: raise api.BadRequest(str(e))


def r_orchestrator_session(conn, m, mm, q, b, actor):
    from .runner import RUNNER
    sid = mm.group(1)
    if m == "GET":
        detail = RUNNER.orchestrator_detail(sid,include_logs=q.get('include_logs')=='1')
        if not detail: raise api.NotFound("chat session not found")
        return detail
    if m in ("PATCH", "PUT", "POST"):
        try: return RUNNER.update_orchestrator_session(sid, b.get("project_id"))
        except ValueError as e: raise api.BadRequest(str(e))
    if m == "DELETE":
        try: deleted=RUNNER.delete_orchestrator_session(sid)
        except ValueError as e: raise api.BadRequest(str(e))
        if not deleted: raise api.NotFound("chat session not found")
        return {"deleted": sid}


def r_orchestrator_messages(conn, m, mm, q, b, actor):
    if m == "POST":
        from .runner import RUNNER
        try: return RUNNER.orchestrate(b.get("body", ""), mm.group(1))
        except ValueError as e: raise api.BadRequest(str(e))


def r_orchestrator_message(conn, m, mm, q, b, actor):
    if m == "PATCH":
        from .runner import RUNNER
        try: return RUNNER.edit_orchestrator_message(mm.group(1),int(mm.group(2)),b.get('body',''))
        except ValueError as e: raise api.BadRequest(str(e))


def r_orchestrator_fork(conn, m, mm, q, b, actor):
    if m == "POST":
        from .runner import RUNNER
        try: return RUNNER.fork_orchestrator_session(mm.group(1),int(mm.group(2)))
        except ValueError as e: raise api.BadRequest(str(e))


def r_runner_ticket_message(conn, m, mm, q, b, actor):
    if m == "PATCH":
        from .runner import RUNNER
        tid=int(mm.group(1)); api.get_ticket(conn,tid)
        try: return RUNNER.edit_ticket_message(tid,int(mm.group(2)),b.get('body',''))
        except ValueError as e: raise api.BadRequest(str(e))


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
    (r"/api/teams", r_teams),
    (r"/api/teams/(\d+)/stop", r_team_stop),
    (r"/api/teams/(\d+)", r_team),
    (r"/api/skills/([a-z0-9-]+)", r_skills),
    (r"/api/skills", r_skills),
    (r"/api/runner/projects/(\d+)", r_orchestrator_project),
    (r"/api/runner/projects", r_orchestrator_projects),
    (r"/api/runner/workspace-folders", r_workspace_folders),
    (r"/api/runner/models", r_runner_models),
    (r"/api/runner/debug", r_runner_debug),
    (r"/api/runner/status", r_runner_status),
    (r"/api/runner/sessions", r_orchestrator_sessions),
    (r"/api/runner/sessions/([^/]+)/stop", r_orchestrator_stop),
    (r"/api/runner/sessions/([^/]+)/messages", r_orchestrator_messages),
    (r"/api/runner/sessions/([^/]+)/messages/(\d+)/fork", r_orchestrator_fork),
    (r"/api/runner/sessions/([^/]+)/messages/(\d+)", r_orchestrator_message),
    (r"/api/runner/sessions/([^/]+)", r_orchestrator_session),
    (r"/api/runner/orchestrator", r_orchestrator),
    (r"/api/runner/settings", r_runner_settings),
    (r"/api/runner/templates", r_templates),
    (r"/api/scheduled-tasks/(\d+)/runs", r_scheduled_runs),
    (r"/api/scheduled-tasks/(\d+)/(pause|resume|run-now)", r_scheduled_task_action),
    (r"/api/scheduled-tasks/(\d+)", r_scheduled_task),
    (r"/api/scheduled-tasks", r_scheduled_tasks),
    (r"/api/tickets/(\d+)/runner", r_runner_ticket),
    (r"/api/tickets/(\d+)/runner/messages/(\d+)", r_runner_ticket_message),
    (r"/api/tickets/(\d+)/retry", r_ticket_retry),
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
    p.add_argument("-ws", "--workspace", metavar="PATH",
                   help="set and save the absolute agent workspace directory")
    a = p.parse_args(argv)
    docker_bind = os.environ.get("COMS_IN_DOCKER") == "1" and a.host == "0.0.0.0"
    if a.host != "localhost" and not docker_bind:
        try: local = ipaddress.ip_address(a.host).is_loopback
        except ValueError: local = False
        if not local: p.error("This unauthenticated dashboard must bind to a loopback address.")
    db.connect()  # ensure schema exists before first request
    from .runner import RUNNER  # start the durable ready-ticket poller
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    try: RUNNER.start(workspace=a.workspace)
    except (RuntimeError, ValueError) as exc:
        srv.server_close(); p.error(str(exc))
    workspace=RUNNER.settings().get('workspace')
    print(f"Swarm Agent dashboard: http://{a.host}:{a.port}/   db={db.db_path()}   workspace={workspace or 'default'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNER.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
