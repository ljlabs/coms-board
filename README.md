# Swarm Agent

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
- **Skills manager** — searchable, editable skill folders shared with the
  orchestrator and every worker agent.
- **Scheduled tasks** — recurring prompts for a selected agent and workspace,
  with cron, interval and weekly schedules plus execution history.
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
Windows home-directory mount. The repository's `skills` folder is mounted so
the Skills page and agents can manage its contents. The separate
`agent-workspace` folder is mounted read-only by default. To let coding agents
write to a workspace, set `SWARM_WORKSPACE_READ_ONLY=false` in a local `.env` file. Keep
`SWARM_WORKSPACE` pointed at a dedicated project folder; writable mode allows
agents to change files inside that folder.

The container connects to llama.cpp at
`http://host.docker.internal:11434/v1` with model
`LFM2.5-2.6B-Q8_0.gguf`. Docker Desktop resolves `host.docker.internal` to the
Windows host. Keep llama.cpp listening on the host as configured; do not
publish the Swarm Agent port on a LAN interface. For another local model or
server port, set `COMS_LLAMACPP_MODEL` and `COMS_LLAMACPP_BASE_URL` in `.env`.
To watch agent activity or logs, use the team room in the browser or run
`docker compose logs -f board` from PowerShell.

When the board runs in Docker, use the PowerShell CLI wrapper so commands read
and update the live database in the private Docker volume:

```powershell
.\scripts\coms.ps1 --as admin --text ticket get 1
.\scripts\coms.ps1 --as admin --text activity --limit 20
```

Running `python .\coms.py` on Windows opens the seed database under `data/`;
that file is not updated after Compose imports it. Use the wrapper for all
board work while Docker is running.

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

In **Board** view, drag a ticket onto any status column to save its new status.
Each card also has a **Move to** selector for keyboard and touch use. The target
column highlights during a drag; cards stay in their original column until the
save succeeds. Selected board and filters are retained, so a ticket moved out
of a status filter disappears from that filtered view. Moves update the ticket
timeline and activity feed using the identity in the dashboard header.

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

The runner retries failed agent runs twice and automated final reviews up to
three times, with a short increasing backoff. Teammate replies and peer reviews
also retry up to three total attempts. If work still fails, open its ticket and
use **Retry agent run** or **Retry final review**, or run
`python3 coms.py --as admin ticket retry <ticket-id>` inside the board
container. Retry preserves prior records and does not bypass child-ticket or
collaboration gates. Tickets waiting for teammates resume automatically when
their replies or child results arrive.

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

While an Orchestrator response is running, use **Stop** to cancel that turn
and send a new direction in the same chat. The canceled turn is recorded and
the composer draft is preserved. This does not stop child workers already
running on delegated tickets. A failed job lead with unfinished children stays
waiting; after five minutes without activity, the runner gives the lead a
180-second monitor turn in a separate supervision slot to inspect child tickets
and progress, then optionally
guide workers through team messages. It cannot create work or deliver results
during that turn. See
[`devDocs/orchestrator-resilience.md`](devDocs/orchestrator-resilience.md).

**Agents** is the persistent team roster. The runner registers an orchestrator
agent, and every ticket execution uses its assigned registered identity.
For team work, create a ready ticket of type **job** and optionally set
**Suggested workers**. The lead uses `agents_list` and `agent_ensure` to reuse
specialists or register missing teammates, then assigns ready child tickets.
The lead releases its execution slot while waiting and resumes automatically
to verify and synthesize worker results. Final review cannot close a parent
with unfinished child work.

**Skills** reads skill folders from the repository-root `skills/` directory.
The manager includes built-in SwarmBoard, ticket, wiki/Q&A, scheduling, team
orchestration and source-backed research workflows. Built-in instruction files
are read-only and cannot be edited or deleted through the page or agent tools;
their enabled state and triggers remain configurable. Each skill can have
comma-separated **Trigger words** such as `research, peer review`. When a word
or phrase matches a message or assignment, the enabled skill's full instructions
are automatically added to that conversation turn. Matching ignores case,
uses whole words and loads each matching skill once. Edit triggers with **Save
triggers**, or clear them for on-demand use. Disabling a skill stops future
automatic loads; existing conversations can retain previously loaded context.
Each skill lives at `skills/<slug>/SKILL.md`; nested reference files are shown
in the file browser. Search, import a skill folder, create or edit a skill,
toggle whether it appears in the agent index, and delete folders from the
manager. The file panel can create files and empty folders, rename entries,
and move files or folders by dragging them onto a folder or **Skill root**.
Select a folder before creating an entry to use it as the destination. The
**Move** button offers the same operation with a folder picker. Moves preserve
unsaved editor text and never overwrite another entry; the root `SKILL.md`
stays in place. The orchestrator and every worker can list and read every enabled
folder through `skill_list`, `skill_get`, and `skill_file_get`. The human operator and
orchestrator can manage skill contents through the page or `skill_put`,
`skill_file_write`, `skill_entry_create`, and `skill_entry_move`; workers share
verified methods with the orchestrator for publication. Existing database
skills are copied into the folder once at
startup, then files become the source of truth.

**Team rooms**, linked from Agents and each job ticket, show named questions,
replies and peer-review decisions. `team_send` wakes the addressed teammate for
a bounded response turn. The runner pauses senders automatically after a
question or review request, freeing their slots until replies arrive. They resume with the
shared conversation. `team_request_review` asks a teammate to inspect an exact
wiki revision; the reviewer must read it before `team_review` can accept it or
request changes. A later edit invalidates that approval.

Open **Schedule** to create recurring prompts for any registered non-human
agent. Use the interval and weekly controls for common schedules, or enter a
five-field cron expression. Each task saves its timezone and working folder;
when it fires, the scheduler creates a ready ticket assigned to that agent and
the existing runner executes it in that folder. The task page shows the next
run and execution history, with links to each ticket transcript. Pause, resume,
run now and delete controls are available in the dashboard.

The lead's first turn exposes delegation tools only. `team_delegate` reuses a
suitable specialist or registers a named teammate and creates a ready child
task with the job's workspace, source URLs and collaboration requirements;
pass `type=research` for public-source research. Workers start after staffing
finishes, so they can see the complete team roster. Every agent retains native
tools for workspace inspection, commands and assigned work; the runner never
disables tool support by role or ticket type. Board deliverables use MCP tools.
Specialists cannot overwrite another teammate's wiki page. Execution turns
have a 15-minute hard deadline, including continuously streaming backends;
failed turns release their connection and use the existing bounded retries.

New jobs require collaboration. Research delivery requires an answered
teammate question, an attached evidence page with current peer approval, and
a citation to content recorded through `source_fetch` (public HTTPS) or
`source_read_file` (workspace text). File records include numbered lines, a
SHA256 hash and retrieval time; cite the returned URL and source ID. Reviewers
inspect either kind with `source_get` and can use native tools to check current
workspace files. `team_read` returns a bounded index; `message_id` and `offset`
read long messages, and `source_after_id` pages the source index. The lead must
wait for child reviews, write and attach a
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
tool calls, and the final answer each keep their own entry. Every user and
assistant text bubble has a copy icon outside its lower right corner for copying
the complete message text. Tool calls start collapsed, including running tools,
with **Output** and **Formatted** selected. Expand a row to switch between **Input** and
**Output**, and between **Formatted** and **Raw** data. Shell input shows its
command and working directory; output stays readable as terminal text. The
workspace and nonzero exit status remain visible in the panel, with a copy
button for the selected view.
Errors and interrupted turns appear in the conversation;
raw diagnostics are available under **Debug logs**. The header shows the
resolved workspace before sending a message. Tool updates preserve reading
position and expanded cards, and reopening a chat restores the stored entries.
Older responses that were already merged into one bubble remain as stored;
their original message boundaries and tool details cannot be reconstructed.

Assistant responses and tool updates stream through a same-origin server-sent
event feed while the backend responds. Reconnecting restores the canonical
stored conversation; polling remains available if streaming is disconnected.
Backend thinking traces appear under **Thinking**, collapsed initially, and stay
separate from the assistant's answer. Expand a section to read its trace while
it arrives. Traces are shown only when the selected backend supplies them;
older thought chunks stored in diagnostic logs also appear in these sections.

The supplied `animations/chat-loading-neobrutalist.json` plays at the bottom right above the
composer while the current chat is sending, thinking, or responding. The player
and web asset are bundled locally; no CDN is needed. Playback stops when the
response finishes or the page is hidden, and reduced-motion preferences use a
still frame. The composer remains available for drafting the next message.

The orchestrator creates prioritized tickets; each ticket remains out of the
execution queue until **Ready for agent execution** is enabled on that ticket.
Human assignees and tickets tagged `human` are always excluded. Ticket chat
stores user prompts; ACP responses and tool output appear on the Debug page.
Only comments an agent deliberately posts appear in a ticket's Comments timeline.
Every execution and orchestrator session receives the **Swarm Agent MCP tools**.
All agent roles can list and read enabled root skills, including dispatch,
collaboration, and review sessions.
Agents publish knowledge with `wiki_put` / `wiki_append`, questions with
`questions_ask` / `questions_answer`, and delivery with `ticket_result`.
Agents can manage recurring jobs with `schedule_list`, `schedule_get`,
`schedule_create`, `schedule_update`, `schedule_pause`, `schedule_resume`,
`schedule_run_now` and `schedule_delete`. The HTTP API is rooted at
`/api/scheduled-tasks`; run history is available at
`/api/scheduled-tasks/{id}/runs`.
Execution cannot advance to automatic review until `ticket_result` succeeds
through the tools for that run. Review sessions receive only read tools.
Tickets that reach review are automatically assessed by the orchestrator. It
closes work by default when the acceptance criteria appear satisfied, and
leaves a reasoned review comment when a concrete issue needs human judgment.

The default workspace is a sibling directory named `workspace`, created on first
use. The Orchestrator chat list has collapsible project groups; create a chat
inside a group, move existing chats between groups, or delete a chat from its
row. Unsorted chats use the root workspace. A project can choose its own
workspace folder or inherit the root workspace. Use **Choose folder** in the
group or chat header to browse directories visible to the server and save that
folder; the Runner settings page continues to accept a path directly. A
configured workspace must be an existing absolute directory separate
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

Each ticket's **Agent runner chat** uses the orchestrator chat component to show
Markdown replies, expandable tool calls, review decisions, and follow-up replies.
Progress entries identify **Execution**, **Review**, or **Conversation**, even when
the same agent handles all three. Choose **View chat** in scheduled execution
history to open the transcript. Ticket follow-ups restore the saved ACP session
after a restart, or continue from board history if the backend cannot restore it.

Use **+ Project** to open the Create project modal, name the project, and add a
source folder with the graphical picker. The name and workspace are saved
together. Without a source folder, the project inherits the root workspace.
The compose icon beside a project name creates a chat directly in that project,
including when its group is collapsed.

The folder picker also has **New folder** and rename/delete icons beside each
folder. Renaming a folder updates root and project workspace paths under it.
Deletion asks for confirmation and only removes empty folders that are not
assigned as a workspace. Rename/delete wait until active agents finish;
application/runtime folders, symbolic links, and mounted roots are protected.
Folder changes require write permission. For the Docker workspace, set
`SWARM_WORKSPACE_READ_ONLY=false` in your local `.env` and recreate the board
container. This also lets agents write files in that shared workspace.

The folder browser can only show folders accessible to the server process. When
running with Docker Compose, the default host folder is mounted at `/workspace`,
so the browser can choose that folder and its subfolders. To use another host
folder, configure its bind mount before starting the container; a browser choice
cannot add a new Docker mount to an already-running container.

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
- `skills/` — shared agent skills, each in its own folder with a `SKILL.md`.
- `scripts/serve.sh` — local dashboard launcher.
- `scripts/coms` — stable CLI entry point.
- `tests/` — automated tests (stdlib `unittest` only).
- `devDocs/` — system overview and the per-change development workflow.
- `data/` — SQLite database lives here at runtime (not committed).
- `SKILL.md` — agent-facing usage instructions.

## Development

Before changing code, read the [development guide](devDocs/README.md) and its
[system overview](devDocs/SYSTEM_OVERVIEW.md).

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
