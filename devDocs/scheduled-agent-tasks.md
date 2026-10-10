# Feature: Scheduled agent tasks

## Behavior and contracts

People and agents can create recurring prompts for a registered non-human
agent. Each task stores its timezone and existing workspace folder, and offers
cron, interval, or weekly day/time recurrence. Pause, resume, run now, edit,
delete, and execution history are available from the dashboard and agent tools.

Every firing creates a ready task ticket assigned to the configured agent. The
normal runner processes it, so ticket results, comments, and transcripts remain
the durable execution record. Missed firings coalesce to one queued run after a
server restart; the next recurrence is calculated from the current time.

## Implementation path

- Entry points and relevant modules: Schedule dashboard in `web/app.js`; HTTP
  routes in `coms/server.py`; MCP tools in `coms/mcp.py`; recurrence and task
  operations in `coms/schedules.py`; due dispatch and ticket execution in
  `coms/runner.py`.
- Data or state changes: `scheduled_tasks` stores definitions and the next run;
  `scheduled_task_runs` stores each firing and ticket link. `tickets.workspace`
  selects a task-specific agent directory; an empty value preserves the global
  runner workspace for existing tickets. `coms/db.py` migrates existing ticket
  databases.
- Interfaces affected: `/api/scheduled-tasks` and its task actions/history
  routes, MCP `schedule_*` tools, the Schedule dashboard, and the main
  Coms Board `SKILL.md`.

## Verification

- Focused tests: not added or run under the session instruction to avoid tests
  unless requested.
- Regression checks: Python module compilation, JavaScript syntax check, and
  `git diff --check` passed. The automated Python and browser suites were not
  run.

## Maintenance notes

The runner's existing database lease ensures only one process dispatches due
tasks for a database. Recurrence uses `zoneinfo` and the standard library.
When changing task fields or run states, update both the HTTP/MCP surfaces and
the run lifecycle transitions in the runner.
