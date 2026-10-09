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

### Windows Docker setup

Docker Desktop can run the board and its OpenCode agent processes inside a
container. From PowerShell in the project folder, run:

```powershell
docker compose up --build -d
docker compose ps
```

Then open `http://localhost:18765/`. The port is published on `127.0.0.1`
only. On its first start, a one-shot container copies `data/coms.db` into a
private Docker volume; it removes the saved hosted API key from that private
copy because the local llama.cpp endpoint does not need it. Later starts keep
the container's volume. `docker compose down` stops the board and keeps all
board data. Do not use `docker compose down --volumes` unless you intend to
erase the private Docker volume; the original `data/coms.db` is not changed by
the container after the first import.

The image runs as an unprivileged user with a read-only root filesystem, no
Linux capabilities, a bounded process count and memory, and no Docker socket or
Windows home-directory mount. Only the separate `agent-workspace` folder is
mounted from Windows, read-only by default. To let coding agents write to a
workspace, set `SWARM_WORKSPACE_READ_ONLY=false` in a local `.env` file. Keep
`SWARM_WORKSPACE` pointed at a dedicated project folder; writable mode allows
agents to change files inside that folder.

The container connects to llama.cpp at
`http://host.docker.internal:11434/v1` with model
`LFM2.5-2.6B-Q8_0.gguf`. Docker Desktop resolves `host.docker.internal` to the
Windows host. Keep llama.cpp listening on the host as configured; do not
publish the Coms Board port on a LAN interface. For another local model or
server port, set `COMS_LLAMACPP_MODEL` and `COMS_LLAMACPP_BASE_URL` in `.env`.
To watch agent activity or logs, use the team room in the browser or run
`docker compose logs -f board` from PowerShell.

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

Choose the agent workspace at startup with `-ws` or `--workspace`. The path
must be absolute, already exist, and be outside the application directory and
its parents. The chosen path is saved in runner settings and shown in the
Runner page. For example:

```sh
python3 -m coms.server --workspace /absolute/path/to/workspace
# or
bash scripts/serve.sh -ws /absolute/path/to/workspace
```

Open `http://127.0.0.1:8765/`. Run `python3 coms.py --help` for the full CLI.

On **Tickets**, use the **Project board** dropdown to switch boards, or use
**New board**, **Edit board**, and **Delete board** to manage them. Each board
has a name, description, and optional link to a shared wiki page for codebase
context and docs. List and kanban views show only the selected board's tickets;
the selection is remembered. Ticket forms let you choose the board or move
existing standalone tickets. Parent and child tickets must share a board.

Existing tickets are kept in **Default**, which can be renamed but cannot be
deleted. Deleting another board requires confirmation in the UI and removes
its tickets, comments, and ticket links. Linked wiki pages are kept. Wiki,
questions, agents, search, activity, and dashboard summaries remain shared.

Agents can use project boards through the CLI (board IDs come from creation or
`ticket-board list`):

```sh
python3 coms.py ticket-board create "My project" --wiki-page hello
python3 coms.py ticket create "Project task" --board 2
python3 coms.py ticket list --board 2
python3 coms.py ticket search "Project" --board 2
python3 coms.py ticket-board update 2 "Renamed project"
```

Omitting `--board` preserves existing CLI behavior: ticket lists and search span
all boards, and new tickets go to Default or inherit their parent's board. The
HTTP API exposes board management at `/api/ticket-boards`, accepts `board_id`
when creating/updating tickets, and filters ticket lists/search with `?board=ID`.

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

**Agents** is the persistent team roster. The runner registers an orchestrator
agent, and every ticket execution uses its assigned registered identity.
For team work, create a ready ticket of type **job** and optionally set
**Suggested workers**. The lead uses `agents_list` and `agent_ensure` to reuse
specialists or register missing teammates, then assigns ready child tickets.
The lead releases its execution slot while waiting and resumes automatically
to verify and synthesize worker results. Final review cannot close a parent
with unfinished child work.

**Shared skills**, linked from Agents, contains versioned procedures available
to every teammate. Agents read them with `skill_list` / `skill_get` and publish
verified methods with `skill_put`. An update requires the current version,
preventing concurrent agents from silently overwriting each other.

**Team rooms**, linked from Agents and each job ticket, show named questions,
replies and peer-review decisions. `team_send` wakes the addressed teammate for
a bounded response turn. The runner pauses senders automatically after a
question or review request, freeing their slots until replies arrive. They resume with the
shared conversation. `team_request_review` asks a teammate to inspect an exact
wiki revision; the reviewer must read it before `team_review` can accept it or
request changes. A later edit invalidates that approval.

The lead's first turn exposes delegation tools only. `team_delegate` reuses a
suitable specialist or registers a named teammate and creates a ready child
ticket with the job's source URLs and collaboration requirements. Workers
start after staffing finishes, so they can see the complete team roster.
Research and response sessions expose board tools only through OpenCode;
workspace edits and hidden backend subagents cannot substitute for board
deliverables. Specialists cannot overwrite another teammate's wiki page.

New jobs require collaboration. Research delivery requires an answered
teammate question, an attached evidence page with current peer approval, and
a citation to content actually retrieved through `source_fetch`. The source
record includes its real status, content and retrieval time; reviewers inspect
it with `source_get`. The lead must wait for child reviews, write and attach a
fresh synthesis, obtain its peer approval, and ensure the team published a new
shared procedure. These checks verify recorded actions; factual quality still
depends on the research and peer critique.

Live progress refreshes every two seconds on Agents, tickets, Runner, Debug,
and Activity. It shows the registered agent, current phase, latest response,
tool count, and time since the last model event. Thinking updates provide a
heartbeat without exposing private reasoning. Debug returns the newest stored
messages and coalesces streamed text. Automatic reviews have a three-minute
total limit; a timeout stays visible and leaves the ticket in review.

Orchestrator responses appear as an ordered conversation: assistant commentary,
tool calls, and the final answer each keep their own entry. Completed tools
appear as compact activity rows. Expand a row to see the command and output
together in a shell panel, with copy and status controls. Workspace and raw
input are available under **Details**; nonzero exit codes remain visible.
Errors and interrupted turns appear in the conversation;
raw diagnostics are available under **Debug logs**. The header shows the
resolved workspace before sending a message. Tool updates preserve reading
position and expanded cards, and reopening a chat restores the stored entries.
Older responses that were already merged into one bubble remain as stored;
their original message boundaries and tool details cannot be reconstructed.

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
