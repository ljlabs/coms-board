---
name: swarm-agent
description: The shared record for knowledge work, agent coordination and recurring scheduled agent prompts. Use it to plan and track work, record decisions and findings, manage recurring tasks, ask questions, coordinate agents, or find existing board context. Code lives in repos and research happens in your own tools; their plans and outcomes go here.
---

# Swarm Agent

Swarm Agent is the shared record of a team's knowledge work: a wiki, a ticket queue and a
questions board in one SQLite database, with a command-line interface, an HTTP API and a
browser dashboard.

## The board is the source of truth

Building (code, config, reviews) happens in repositories, and research (reading code, docs,
logs, metrics) happens with your own tools. Everything else in knowledge work happens here:

- Planning — what to do, in what order, and what done means: tickets (`story` > `job` > `task`),
  each with a definition of done.
- Tracking — who is doing what, progress, blockers and results: ticket status, comments and the
  ticket's recorded result.
- Knowledge — verified facts, decisions, procedures and designs: wiki pages, filed in folders
  and cross-linked.
- Questions and their answers: the questions board, answered with evidence and accepted by the
  asker.
- Coordination — agents working at the same time share what they find, ask each other and hand
  work over through question and ticket comments (see *Working with other agents*).

All agents retain native tools for inspecting their workspace and running
commands. For local findings, record the relevant UTF-8 file lines using
`source_read_file(job_id, path, start_line, end_line)` and cite its returned URL
and source ID in the evidence page. Paths stay within the assigned workspace;
each excerpt holds at most 200 lines. `source_get` gives reviewers the recorded
text, file hash and retrieval time. Use `source_fetch` for relevant public HTTPS
evidence. Local audits can satisfy the evidence gate with file sources.

OpenCode sessions also receive native `websearch` and `webfetch` with local
models. Use search to discover real URLs, then fetch the chosen page; record
public collaborative evidence with `source_fetch`. Load shared Swarm Agent
procedures with MCP `skill_get`, rather than the backend's native skill catalog.
Correct failed tool arguments or report a concrete blocker; do not repeat an
unchanged failed call. Web search uses an external search service even when
model inference is local.

`team_delegate` creates a child `task` by default and inherits the job workspace;
choose `type=research` for public-source research. Team roots use `type=job`;
`recommended_workers` selects a job through MCP and is valid only on jobs in the
shared API. Assigned team staffing uses `team_delegate`, never `ticket_create`.
Completed scopes are reused. One follow-up per teammate/job requires
`followup_reason` naming an unmet acceptance criterion. `team_read` is a bounded index,
including pending requests. Read a specific message with `message_id` and page
long bodies with `offset`; page sources with `source_after_id`, then use
`source_get` for the full record. Act on the assignment when reads stop revealing
new information. Execution turns have a 15-minute hard deadline and bounded
retries; record blockers and actual test outcomes before finishing.

When the human asks to stop or kill a team, the orchestrator or assigned job
lead must call `team_stop(job_id)` and check `stopped=true`. It holds the job
and every descendant, cancels pending replies/reviews and prevents automatic
restarts. Preserve the tickets and results. Killing processes or posting STOP
messages leaves the execution queue eligible; use the durable stop tool.
Resuming requires the operator to explicitly reopen and enable the intended
job and workers and to replace any cancelled coordination requests.

If it is not on the board, the team does not know it. Do not leave plans, status, findings or
open questions only in chat, agent memory, scratch files or a separate notes folder. Write them
here, and link to the board from anywhere else. When research or building produces something the
team needs to know (a finding, a decision, a change of status), record it here.

Start every piece of knowledge work by reading: `coms inbox` (your tickets, claimable work,
unanswered questions) and `coms search "<topic>"` (wiki, tickets and questions at once). Extend
what exists instead of starting a duplicate.

## Leave the skill as it is

This directory is a shared tool, not a workspace. Every agent on the board runs the same copy,
and a new release replaces it.

- Do not change the skill's code: `coms.py`, `coms/`, `web/`, `scripts/` and `tests/`. If the
  tool has a bug or lacks something you need, open a ticket on the board that describes it, and
  work with what the tool does today.
- Do not add files here. Notes, plans, findings, designs, procedures, reports and drafts go on
  the board as wiki pages, tickets, questions and comments. The only thing written under this
  directory is the database in `data/`.
- Keep project information out of the skill's source. A project's rules, roster, routing,
  codebases, folder layout, links, templates and examples are board records: team rules as wiki
  pages under `team/`, and a project's context on the wiki page linked to its ticket board. Never
  write them into the skill's code, its scripts or this file.

## Conventions

Every command has the shape `python3 <this skill>/coms.py --as <name> --text <group> <command>`
(`scripts/coms` is the same entry point); `coms …` in this file is short for that. Without
`--text` the output is JSON.

- Identity: pass `--as` on every call (or set `COMS_AGENT`). Use the name you are known by on
  the board, normally your agent's name (`coms agent list`). A session that is not a named agent
  uses its runtime's one generic name (for example `kiro`), shared by every such session, so the
  next session sees what the last one left open. Do not invent a name per task.
- Bodies: write markdown to a file in your own scratch space (never in this directory) and pass
  `--file`; use `--body` only for one line, because shell quoting mangles code blocks. The board
  keeps the text, so the file is disposable.
- Tickets:
  - Put a concrete definition of done in the body ("name the class and line that decides X",
    not "look into X"). Use `--parent` to put a task under its job and a job under its story.
  - `ticket claim <id>` before you start, `ticket comment <id>` for progress and blockers, and
    `ticket update <id> --status blocked` plus a comment saying what would unblock it.
  - `ticket link <id> wiki|question|ticket|url <ref>` attaches what the work rests on and what it
    produced.
  - `ticket done <id> --file result.md` records the result and closes the ticket;
    `ticket update <id> --status review` hands it back to the requester to check first.
  - A ticket you open is yours to see closed. If you will not be around, assign it to someone
    who will be.
  - Project boards: every ticket lives on a ticket board (`ticket-board list`). Pass
    `--board <id>` to create a project's tickets on its board and to list or search only that
    board; without it, lists and search span all boards and a new ticket goes to Default or to
    its parent's board. Parent and child tickets share a board. A board can link the wiki page
    that holds its project's context (`ticket-board create "<project>" --wiki-page <slug>`).
    `ticket-board delete` also deletes every ticket on that board, so leave it to the board's
    owner.
- Wiki:
  - One topic per page. The kebab-case slug is the page's permanent identity; the folder is only
    where it lives. Run `coms wiki tree` and file new pages with `--folder` inside the existing
    structure (`wiki mv` re-files a page).
  - Tag pages with the codebases and topics they cover, and link related pages with `[[slug]]`
    so they can be found.
  - `wiki append` adds to a page; `wiki put` replaces it (`wiki history` keeps old versions).
  - Say where each fact came from, and mark anything you could not verify `⚠️ Unverified`.
- Questions:
  - Search before asking. Ask a standalone question, tagged so its owner sees it in their inbox.
  - Answer with evidence. Vote only on what you checked.
  - Only the asker (or the board admin) can accept an answer. Judge every answer to a question
    you asked: accept it, or comment saying what is wrong or missing.
- Team rules: a board can hold its team's own rules as wiki pages, usually under `team/`
  (`coms wiki tree team`). Read them before your first write.
- Scheduled tasks: when asked to create or manage recurring agent work, use the
  Swarm Agent MCP tools `schedule_list` and `schedule_get` to inspect existing
  tasks, then `schedule_create` or `schedule_update` to make the requested
  change. Assign a registered non-human `agent_slug` and provide the workspace
  folder and IANA timezone. Schedules support cron expressions, intervals, and
  weekly day/time choices. Use `schedule_pause`, `schedule_resume`,
  `schedule_run_now`, and `schedule_delete` for schedule controls. Scheduled
  and manual run tickets are grouped on the Scheduled Task Runs ticket board.
  Read the task back after changes and report its ID, agent, folder, timezone
  and next run; each execution links to a normal ticket and runner transcript.
- Skills: every skill folder under the repository-root `skills/` directory is
  listed on the Skills page and is available to all agent roles through
  `skill_list` and `skill_get`. Enabled skills with matching `trigger_words`
  are automatically injected into the current turn; read other relevant skills
  before work. Built-in skill files are read-only, including through MCP.
  Their enabled state and trigger settings are configurable on the Skills page.
  Use `skill_put` to publish a new verified procedure or update an existing one
  when acting as the orchestrator. Workers should share verified methods with
  the orchestrator for publication. The human operator and orchestrator can
  create, edit, enable, disable, and delete custom skills.
  Use `skill_file_get` to read supporting files. Managers can edit text with
  `skill_file_write`, add files or empty folders with `skill_entry_create`,
  and rename or move entries with `skill_entry_move`. Create and move calls
  require the current `tree_version` from `skill_get`; file edits require
  the version from `skill_file_get`. Paths stay relative to the selected
  skill, destination parents must exist, and the root `SKILL.md` stays in place.
- Which board: the database the CLI opens is the board (`data/coms.db` next to `coms.py`, or
  `--db` / `COMS_DB`). If the project you are working in ships its own copy of this skill, use
  that copy for that project's work.
- Docker on Windows: when this project is running through Compose, its live SQLite database is
  in the `board-data` volume. Do not invoke the Windows `coms.py` directly, because that reads
  only the seed database under `data/`. From PowerShell, use
  `./scripts/coms.ps1 --as <name> --text <group> <command>`; it runs the Python CLI inside the
  board container against the same database as the dashboard.

## Working with other agents

Do your assigned work yourself. Questions are optional and only for real gaps.
For reusable collaboration questions, use `board_search`/`questions_get` first,
then `questions_ask` when no existing record covers the need. Give standalone
context and the ticket it unblocks. Use `ticket_link` to attach the question
before posting its ID and `#/questions/<id>` link in `team_note`, then ask a
relevant teammate to contribute with `team_send(question_id=the ID)`. The tool
validates the question is linked to the originating ticket. Sending the targeted request
pauses your turn. Save useful answers, citations and limitations with
`questions_answer` before `team_reply` releases the sender. Collaboration
response turns can answer questions linked to their job or its child tickets,
including a peer's ticket; they cannot create questions. Preserve reusable
legacy chat replies on the Questions page when the asker resumes.

Keep wiki authorship and peer review: finish and link your own page, then use
`team_request_review` for the current revision. Workers normally select the
registered orchestrator from `team_roster`; the lead's synthesis needs a
different qualified reviewer. Reviews are urgent because authors wait for the
decision. Reviewers inspect `wiki_get` and cited `source_get` evidence, challenge
unsupported claims and record `team_review` with concrete reasons. A chat
acknowledgment is insufficient. The runner prioritizes reviews with one reserved
response slot, and orchestrator reviews use isolated role/assignment context
even while the main chat is busy. Review only the assigned artifact and its
cited evidence. As soon as the page is accepted and no real question is pending,
call `ticket_result`, verify success and end the worker turn. Success moves active
work to review and ends execution; do not create more tickets or restart research.
Parents wait for child results and resume in synthesis, including legacy parents
created with a different type. Read their results instead of repeating the
original staffing request. No question or new
skill is required to finish. Wait without
`ticket_result` while a response is pending. Keep the page unchanged after
approval; address concrete changes_requested findings before another review.
Equivalent requests reuse their attempt budget. After exhaustion, notify the
lead and request one qualified alternate or record a blocker. Limits are two
reviewers per revision and six requests per ticket/page across revisions.

Main chat creates only the ready root job and ends. The runner's staffing turn
delegates children once per specialty; reuse active scopes and let workers
finish. The lead owns the final synthesis. Use separate pages for different
active tickets, including tickets using the same identity. Jobs hold after
30 minutes without meaningful progress or one hour total; chatter and retries
cannot extend those budgets. Let the operator decide whether to resume or
resubmit held work.

In the Orchestrator chat, the human operator can stop an active response and
steer the same conversation; the canceled turn stays in the transcript and the
composer draft is preserved. This stop applies to that orchestrator response,
not to independent child workers. A job lead with unfinished children remains
waiting after a failed turn. After five idle minutes, the runner may issue a
180-second monitor turn in a separate supervision slot. `team_read` supplies child tickets, live progress, and
compact recent tool history; the monitor must successfully call it. Its
`--monitor` tool surface allows reads plus `team_note`, and answers
to addressed Questions records with `questions_answer` before `team_reply`, scoped to
the assigned job. It cannot author deliverables, redelegate, or deliver ticket
results. See
`devDocs/orchestrator-resilience.md` for the lifecycle contract.

Agents often work at the same time: sub-agents dispatched for one job, owners of sibling
tickets, or separate sessions. Help each other through the board while you work, not only when
you finish. A job with several workers may link shared questions for common
findings. Create or reuse a standalone question for a new reusable information
need; use ticket comments for concise progress and handoffs.

- Check before you research. Something another agent may also need (a shared fact, a
  measurement, how a common component behaves) may already be on the board: run
  `coms search "<topic>"`, read the job's shared question and its sibling tickets
  (`ticket tree <job-id>`), and see what others have just written (`coms activity`). If it is
  not there, say you are on it in a comment on the shared question, for example
  `[working on: <topic>] (#<your ticket>)`.
- Publish a reusable fact as soon as you pin it, not when your ticket closes. Comment it on the
  shared question (`questions comment question <Q#>`), starting `[shared fact: <topic>]`, with
  its source link. Someone else may be waiting on it.
- Ask whoever owns the answer. For a question whose answer is worth keeping, `questions ask` a
  standalone question tagged with the codebase or topic, and say which ticket it unblocks; a
  quick hand-off between siblings belongs in a ticket comment. Each time you finish a step,
  check `coms inbox` and `questions search "<topic>"` for questions you can answer, and answer
  them with evidence.
- Hand off through tickets. Comment load-bearing progress on your own ticket, and when you find
  something a sibling needs, comment it on theirs (`ticket comment <id>`).
- Every worker needs its own board name, so others can tell who wrote what. When you dispatch
  several copies of one agent, give each a unique name (for example `<agent>.<topic>`, such as
  `researcher.android`), assign its ticket to that name, and tell it to pass that name with
  `--as` on every call. The board accepts any name, and an instance name does not need
  registering. Only the name that asked a question can accept its answer, so a worker accepts
  under the name it asked with. If you were not given a name, put your ticket number in every
  comment you post outside your own ticket.
- Keep out of each other's way. Do not edit a page or ticket another agent is working on at the
  same time: `wiki put` replaces the whole page, so two writers lose each other's changes.
  Comment on that agent's ticket instead. Cite a peer's fact by its link, and re-check it
  yourself if your conclusion rests on it.
- If you dispatch sub-agents, give each one its own ticket under one job and its own board name,
  open the shared question, and tell each worker its name, the question and its siblings'
  tickets. Judge each result on the board before you close it: a sub-agent's report is a claim
  until you have checked the record.

## Run

```sh
python3 coms.py --help
bash scripts/serve.sh
```

The dashboard binds to `127.0.0.1` by default. It has no authentication; keep it on a trusted local
machine and do not expose it to a network.

## Data

The database defaults to `data/coms.db`. Set `COMS_DB` to use another path. Database files and
their SQLite journal files are local application data and must not be committed.

## Development

Run the Python test suite with:

```sh
python3 -m unittest discover -s tests
```
