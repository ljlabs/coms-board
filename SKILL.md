---
name: coms-board
description: The single source of truth for knowledge work — plans, status, decisions, findings, questions and answers — kept as a wiki, ticket queue and Q&A board in one local SQLite database with a CLI and browser dashboard. Use it whenever you plan or track work, record what you learned or decided, ask or answer a question, coordinate with other agents or sub-agents working at the same time, or need to know what is already known. Code lives in repos and research happens in your own tools; their plans and outcomes go here.
---

# Coms Board

Coms Board is the shared record of a team's knowledge work: a wiki, a ticket queue and a
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
- Which board: the database the CLI opens is the board (`data/coms.db` next to `coms.py`, or
  `--db` / `COMS_DB`). If the project you are working in ships its own copy of this skill, use
  that copy for that project's work.

## Working with other agents

Agents often work at the same time: sub-agents dispatched for one job, owners of sibling
tickets, or separate sessions. Help each other through the board while you work, not only when
you finish. A job with several workers usually has one shared question for their findings; if
it has none, use the job ticket's comments instead.

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
