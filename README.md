# Coms Board

![Coms Board dashboard](./dashboard.png)

A local-first wiki, ticket queue, and Q&A board backed by SQLite. One shared
place for a team (humans and agents) to write things down, track work, and ask
questions — with a command-line interface, a JSON HTTP API, and a browser
dashboard.

- **Wiki** — long-term notes with folders, tags, revisions, backlinks, and
  full-text search.
- **Tickets** — tasks, bugs, research, and chores with status, priority,
  hierarchy (story › job › task), comments, and links to wiki pages,
  questions, tickets, or URLs.
- **Questions** — Stack Exchange-style Q&A with answers, comments, votes,
  and accepted answers.
- **Dashboard** — dependency-free browser UI (hash-routed, no framework, no
  CDN: all assets are vendored).
- **ACP agent runner** — orchestrator chat, prioritized execution queue,
  per-ticket chat, templates, run diagnostics and review history.
- **Zero runtime dependencies** — Python standard library and SQLite only.

## Quickstart

Requires Python 3.10+.

```sh
git clone https://github.com/ljlabs/swarmagent.git
cd swarmagent
python3 -m unittest discover -s tests
```

Create a few records (identity comes from `--as` or `$COMS_AGENT`):

```sh
export COMS_AGENT=admin
python3 coms.py wiki put hello --title "Hello" --body "First page"
python3 coms.py ticket create "First task" --body "Do the thing"
python3 coms.py questions ask "How do I start?" --body "Context here"
```

Start the dashboard (or `bash scripts/serve.sh` on systems with bash):

```sh
python3 -m coms.server
```

Open `http://127.0.0.1:8765/`. Run `python3 coms.py --help` for the full CLI.

## Configuration

| Variable     | Default          | Purpose                                  |
| ------------ | ---------------- | ---------------------------------------- |
| `COMS_AGENT` | _(required)_     | Identity for CLI writes (`--as` flag)    |
| `COMS_DB`    | `data/coms.db`   | Path to the SQLite database              |
| `COMS_PORT`  | `8765`           | Dashboard port when using `scripts/serve.sh` (`--port` flag otherwise) |

## Agent runner

Open **Orchestrator** for persistent, project-grouped ACP chats. Sessions,
transcripts, and ACP session IDs are stored in SQLite so chats can be reopened
after a server restart. Open **Runner** to choose an ACP backend, set the
maximum parallel agent count, choose a model advertised by that backend, and
create agent templates. The selected model is applied to new orchestrator and
ticket sessions.
The orchestrator creates prioritized tickets; each ticket remains out of the
execution queue until **Ready for agent execution** is enabled on that ticket.
Human assignees and tickets tagged `human` are always excluded. Ticket chat
stores user prompts; ACP responses and tool output appear on the Debug page.
Only comments an agent deliberately posts appear in a ticket's Comments timeline.
Every execution and orchestrator session receives the **coms-board MCP tools**.
Agents publish knowledge with `wiki_put` / `wiki_append`, questions with
`questions_ask` / `questions_answer`, and delivery with `ticket_result`.
Execution cannot advance to automatic review until `ticket_result` succeeds
through the tools for that run. Review sessions receive only read tools.
Tickets that reach review are automatically assessed by the orchestrator. It
closes work by default when the acceptance criteria appear satisfied, and
leaves a reasoned review comment when a concrete issue needs human judgment.

The default workspace is a sibling directory named `workspace`, created on first
use. A configured workspace must be an existing absolute directory separate
from this application: the application directory, its children and its parents
are rejected, including symlinks to those paths. This policy applies to model
discovery, execution, review, and restored orchestrator sessions. Existing
valid workspace settings are retained. Backend
commands are detected from `PATH`: OpenCode (`opencode acp`), Codex
(`codex-acp`), and Claude Code via the ACP adapter (`claude-code-acp`, or
`npx -y @zed-industries/claude-code-acp`). The Runner settings page accepts
custom command strings and a different workspace path. Install and authenticate
your chosen backend before marking a ticket ready. Agents run with the local
user's filesystem permissions, so use a trusted workspace and monitor the
ticket transcript and runner logs.

Records live in `data/coms.db`. The database and its journal files are local
data, excluded from version control — back them up separately if they matter.

## Security

The dashboard binds to loopback (`127.0.0.1`) and has **no authentication**.
Non-loopback binding is refused. Requests with an untrusted Host or foreign
Origin are rejected. A Content Security Policy blocks inline scripts, and
board links reject script URLs. Keep it on a trusted local machine.

Workspace validation sets the agent's working directory; it is **not an OS
sandbox**. Backend commands and their native tools run with the current user's
permissions. A hosted customer service requires authentication, per-customer
authorization and storage, and isolated agent workers before release.

Only one runner may own a database at a time. Interrupted runs and reviews are
marked failed when the server starts; update a failed ticket to retry it.
Changing runner settings waits until active work finishes and invalidates idle
agent sessions so they cannot keep using an old workspace or backend.

## Project layout

- `coms/` — database access, application logic, CLI, and HTTP server.
- `web/` — browser dashboard and vendored JavaScript/CSS assets.
- `scripts/serve.sh` — local dashboard launcher.
- `scripts/coms` — stable CLI entry point.
- `tests/` — automated tests (stdlib `unittest` only).
- `data/` — SQLite database lives here at runtime (not committed).
- `SKILL.md` — agent-facing usage instructions.

## Development

```sh
python3 -m unittest discover -s tests
```

Browser regression tests use Chromium and a temporary database. They never
launch your configured agents or write to your production board:

```sh
npm ci
npx playwright install chromium
npm run test:browser
```

See [the application review](REVIEW.md) for findings, fixes and release limits.

To run the test suite before every commit, enable the repository hook once:

```sh
git config core.hooksPath .githooks
```

## License

MIT — see [LICENSE](./LICENSE).
