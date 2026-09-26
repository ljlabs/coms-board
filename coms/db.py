"""SQLite storage for the coms-board (agent wiki + ticket queue + Q&A).

Single-file database, stdlib only. Designed to run on old SQLite builds
(3.7.x: no FTS5, no JSON1, no UPSERT, no window functions), so the schema
sticks to plain tables + FTS4 for full-text search.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "coms.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  department  TEXT NOT NULL DEFAULT '',
  role        TEXT NOT NULL DEFAULT 'expert',      -- lead | expert | generalist | human
  description TEXT NOT NULL DEFAULT '',
  codebases   TEXT NOT NULL DEFAULT '',            -- comma separated package names
  wiki_pages  TEXT NOT NULL DEFAULT '',            -- comma separated wiki slugs to read first
  agent_file  TEXT NOT NULL DEFAULT '',            -- optional local configuration file path
  created_at  TEXT NOT NULL,
  last_seen   TEXT
);

-- Long-term agent notes ("how to do X"), searchable, out of context until asked for.
CREATE TABLE IF NOT EXISTS wiki_pages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT NOT NULL UNIQUE,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL DEFAULT '',
  tags        TEXT NOT NULL DEFAULT '',            -- comma separated
  department  TEXT NOT NULL DEFAULT '',
  folder      TEXT NOT NULL DEFAULT '',            -- slash-separated location path; '' = unfiled
  author      TEXT NOT NULL DEFAULT '',
  updated_by  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_revisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id     INTEGER NOT NULL,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  tags        TEXT NOT NULL DEFAULT '',
  author      TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wiki_rev_page ON wiki_revisions(page_id);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts4(slug, title, body, tags);

CREATE TABLE IF NOT EXISTS tickets (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL DEFAULT '',
  type        TEXT NOT NULL DEFAULT 'task',        -- research | task | bug | question | chore
  status      TEXT NOT NULL DEFAULT 'open',        -- open | in_progress | blocked | review | done | wontfix
  priority    TEXT NOT NULL DEFAULT 'p2',          -- p0 | p1 | p2 | p3
  department  TEXT NOT NULL DEFAULT '',
  assignee    TEXT NOT NULL DEFAULT '',
  requester   TEXT NOT NULL DEFAULT '',
  tags        TEXT NOT NULL DEFAULT '',
  result      TEXT NOT NULL DEFAULT '',            -- markdown: what was delivered
  due_at      TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_tickets_assignee ON tickets(assignee);

CREATE TABLE IF NOT EXISTS ticket_comments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id   INTEGER NOT NULL,
  author      TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT 'comment',     -- comment | status | assign
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tc_ticket ON ticket_comments(ticket_id);

CREATE VIRTUAL TABLE IF NOT EXISTS ticket_fts USING fts4(title, body, tags, result);

-- Structured attachments: context a ticket carries so a re-pickup needs no
-- archaeology. kind: wiki (page slug) | question (id) | ticket (id) | url.
CREATE TABLE IF NOT EXISTS ticket_links (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id   INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  ref         TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  created_by  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  UNIQUE(ticket_id, kind, ref)
);
CREATE INDEX IF NOT EXISTS ix_tl_ticket ON ticket_links(ticket_id);

-- Stack-Exchange style questions.
CREATE TABLE IF NOT EXISTS questions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  title              TEXT NOT NULL,
  body               TEXT NOT NULL DEFAULT '',
  author             TEXT NOT NULL DEFAULT '',
  tags               TEXT NOT NULL DEFAULT '',
  department         TEXT NOT NULL DEFAULT '',
  status             TEXT NOT NULL DEFAULT 'open', -- open | answered | closed
  accepted_answer_id INTEGER,
  views              INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id INTEGER NOT NULL,
  author      TEXT NOT NULL DEFAULT '',
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_answers_q ON answers(question_id);

CREATE TABLE IF NOT EXISTS votes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,                       -- question | answer
  target_id   INTEGER NOT NULL,
  voter       TEXT NOT NULL,
  value       INTEGER NOT NULL,                    -- +1 | -1
  created_at  TEXT NOT NULL,
  UNIQUE(target_type, target_id, voter)
);

CREATE TABLE IF NOT EXISTS question_comments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,                       -- question | answer
  target_id   INTEGER NOT NULL,
  author      TEXT NOT NULL DEFAULT '',
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS question_fts USING fts4(kind, ref_id, title, body, tags);

-- Cross-cutting activity feed for the dashboard.
CREATE TABLE IF NOT EXISTS activity (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor       TEXT NOT NULL DEFAULT '',
   kind        TEXT NOT NULL,                       -- wiki.create | ticket.status | question.answer | ...
  ref         TEXT NOT NULL DEFAULT '',            -- e.g. wiki:slug, ticket:12, question:3
  summary     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_activity_created ON activity(created_at);
"""


def db_path() -> Path:
    return Path(os.environ.get("COMS_DB", str(DEFAULT_DB)))


# Columns added after the initial schema shipped. connect() (write mode)
# applies these with ALTER TABLE ... ADD COLUMN, which works on SQLite 3.7.
MIGRATIONS = [
    ("tickets", "parent_id", "INTEGER"),  # ticket hierarchy: story > job > task
    ("wiki_pages", "folder", "TEXT NOT NULL DEFAULT ''"),  # wiki folders: slash path, '' = unfiled
]


# Tables renamed after the initial schema shipped (board -> questions).
# connect() (write mode) copies each legacy table into its successor, then
# drops the original. Copy-based (not ALTER TABLE ... RENAME) because the FTS4
# virtual table's shadow tables don't survive a plain rename on old SQLite
# builds. INSERT OR IGNORE + DROP makes this idempotent.
TABLE_RENAMES = [
    ("board_comments", "question_comments", None),
    ("board_fts", "question_fts",
     ("kind", "ref_id", "title", "body", "tags")),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    for old, new, cols in TABLE_RENAMES:
        if old in tables and new in tables:
            if cols is None:
                conn.execute(f"INSERT OR IGNORE INTO {new} SELECT * FROM {old}")
            else:
                collist = ", ".join(cols)
                conn.execute(f"INSERT OR IGNORE INTO {new}({collist}) "
                             f"SELECT {collist} FROM {old}")
            conn.execute(f"DROP TABLE {old}")


def _deny_writes(conn: sqlite3.Connection) -> None:
    """Belt-and-braces: refuse any write statement on a fallback connection."""
    allowed = {getattr(sqlite3, n, None) for n in
               ("SQLITE_SELECT", "SQLITE_READ", "SQLITE_FUNCTION", "SQLITE_RECURSIVE")}
    allowed.discard(None)
    conn.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in allowed
                        else sqlite3.SQLITE_DENY)


def connect(path: Path | str | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Return a per-thread connection.

    Write mode (default) ensures the schema + migrations. The database uses
    the default rollback journal, NOT WAL: on this host's SQLite (3.7.x) a
    WAL database cannot be read without OS-level write access (readers must
    create/write the -shm/-wal sidecars; heap wal-index read-only support
    arrived in 3.22), which broke `mode=ro`. Rollback journal + a 30s busy
    timeout is plenty for this tool's write volume. A database left in WAL
    by an older version is flipped back to DELETE when possible.

    Read-only mode opens with a `mode=ro` URI and touches NOTHING: no
    pragmas, no schema, no sidecar files — read commands work without write
    permission. If the file is still in WAL and mode=ro cannot serve it, we
    fall back to an ordinary connection with a write-denying authorizer.
    If the database file does not exist yet, read-only falls back to write
    mode so first-run still works.
    """
    p = Path(path) if path else db_path()
    if readonly and not p.exists():
        readonly = False  # nothing to read yet; create the schema instead
    key = (str(p), readonly)
    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "key", None) == key:
        return cached
    if readonly:
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30,
                                   check_same_thread=False)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            # legacy WAL db without sidecars: mode=ro can't open it on 3.7.
            conn = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
            _deny_writes(conn)
        conn.row_factory = sqlite3.Row
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            try:  # flip legacy WAL back; no-op once done. Fails busy → next time.
                conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.OperationalError:
                pass
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    _local.conn = conn
    _local.key = key
    return conn
