# System overview

This is a local-first collaboration board for humans and coding agents. It
stores wiki pages, tickets, questions, agent and runner state in SQLite. The
same core operations are exposed through a browser dashboard, an HTTP API, a
CLI, and tools provided to ACP agents. See the root [`README.md`](../README.md)
for setup, user-facing behavior, configuration, and security limits.

## Main components

| Component | Responsibility |
| --- | --- |
| `coms/db.py` | SQLite schema, connection setup, and migrations for existing databases. |
| `coms/api.py` | Shared application operations and validation over a database connection. |
| `coms/server.py` | Local HTTP server, JSON API routes, static dashboard assets, and live orchestrator events. |
| `coms/cli.py` | Command-line interface; invokes the same application operations as the HTTP API (the read-only `agent templates` list queries the database directly). |
| `web/` | Browser dashboard. It calls the server API and presents wiki, tickets, Q&A, runner, and orchestrator features. |
| `coms/runner.py` | Agent queue, ACP sessions, ticket execution and review, orchestrator sessions, retries, and scheduled-task dispatch. |
| `coms/acp.py` | ACP process connection and protocol handling for configured agent backends. |
| `coms/turn_queue.py` | Bounded OpenCode inference queue with review priority and cancellation. |
| `coms/orchestrator_transcript.py` | Stored orchestrator turns, message ordering, and conversation event publication. |
| `coms/progress.py` | Live agent progress summaries consumed by dashboard views. |
| `coms/mcp.py` | Newline-delimited MCP tool interface used by ACP agents to read and update board records. |
| `coms/team.py` | Team roster, delegation, messages, peer reviews, and recorded research sources. |
| `coms/skill_store.py` | Safe file operations for shared `skills/<slug>/SKILL.md` folders. |
| `coms/workspace.py` | Workspace selection, folder browsing, and validation for agent workspaces. |
| `coms/schedules.py` | Recurrence validation and scheduled-task/run operations. |

Runtime records live in `data/coms.db` by default. Tests should use a temporary
database rather than changing that developer data.

## Request and execution paths

### Human-facing operations

The HTTP server serves static files from `web/`. The dashboard sends JSON
requests to `coms/server.py`. Route handlers pass requests to operations in
`coms/api.py` and related domain modules, except a few settings/list views
(runner settings, team job list, agent templates) that query or validate
inline in `coms/server.py`. The CLI in `coms/cli.py` calls those
application operations directly. Keep validation and business rules for writes
in the shared backend path so that the dashboard, API, and CLI agree on behavior.

When changing a feature, trace all of its entry points. A new operation may
need a domain implementation, an HTTP route, a CLI command, a dashboard control,
or more than one of these. Preserve existing API fields, command names, and
defaults unless the change intentionally updates that contract.

### Agent work

The runner selects ready work, validates its workspace, and starts or resumes
the configured ACP backend. The backend receives board tools through
`coms/mcp.py`; those tools call shared application and collaboration operations
to read context and record deliverables. The runner stores progress and
transcripts, handles retries, and gates review or completion on required board
actions. Team replies and reviews can pause and resume work. Shared collaboration
guidance routes reusable questions/answers to Q&A before team notifications;
peer reviews receive a reserved response slot and isolated orchestrator role
context. Main chat creates the root job; runner dispatch owns staffing.
Team size is a job-only field; MCP normalizes team-sized root requests to jobs.
All parents wait for child results and resume in synthesis. Successful agent
result delivery atomically moves active work to review and ends execution.
Delegation reuses completed scopes and bounds explicit criterion-based follow-ups.
Structured question notifications require a linked question and originating
ticket; artifacts have ticket ownership and bounded revision reviews.
Persisted team progress/lifetime budgets hold nonconverging jobs. ACP processes
own temporary directories cleaned after exit. See
[team collaboration](team-collaboration.md). Scheduled prompts
are dispatched as tickets for the runner to execute.

Changes to agent behavior may cross several boundaries: tool schema and
validation, database records, runner state transitions, delivery checks, and
debug or progress views. Test both the operation and the state transition that
consumes its result. Keep tool descriptions and agent instructions aligned with
the actual behavior.

### Shared skills

The repository-root `skills/` directory is the source of shared skill content.
The Skills page and `skill_list`/`skill_get` MCP tools read from that directory;
the runner gives the orchestrator and ticket agents an index of enabled
folders, including the shipped board workflows. Full instructions load through
`skill_get` when needed or automatically when `trigger_words` in a skill's
frontmatter match the current message or assignment. Matching excludes disabled
skills and never scans generated context or replayed history. SQLite keeps
per-agent read counts, edit audit events, and a one-time import marker for
records from the former database-backed skill store.

## Compatibility and quality checks

- Treat the SQLite schema and saved records as a persistent interface. Add a
  migration for schema changes and test migration from a database with the old
  schema, as well as behavior on a fresh database.
- Treat HTTP JSON, CLI flags/output, and MCP tool names and arguments as
  interfaces that existing users or agents may depend on. Update callers,
  tests, and docs when changing them.
- Keep unit tests in `tests/` using the standard-library `unittest` patterns.
  The full regression command is `python3 -m unittest discover -s tests`.
  Dashboard changes also need the browser suite described in the root README.
- The server is intended for local use and workspace path checks do not sandbox
  agent processes. Read the Security section of the root README before changing
  request handling, binding, or workspace access rules.

This overview is a navigation aid, not a replacement for reading the current
implementation and its tests. Update it when module responsibilities or data
flows change.
