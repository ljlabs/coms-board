# Feature: Orchestrator stop and recovery

## Behavior and contracts

While an Orchestrator response is active, the composer exposes **Stop**. It
cancels the active ACP turn and uses a bounded force-close fallback if the
backend does not stop cleanly. The transcript records the canceled turn and
preserves the existing conversation and unsent composer draft so the user can
steer the same session. Stopping an orchestrator response does not cancel
independent child worker runs.

The runner checks idle Orchestrator ACP liveness every 30 seconds and
asynchronously recreates a dead session. The interval is the check cadence; it
does not promise that replacement initialization will finish within 30 seconds.
Model discovery preserves a busy project-workspace ACP session instead of
terminating it to refresh the root-workspace session.

A failed job lead with unfinished children stays waiting instead of ending the
job. Once its persisted waiting activity is older than five minutes, the runner
starts a dedicated monitor run for that lead. It uses normal agent identity and
one separate supervision slot, so full worker capacity cannot starve the
heartbeat. Only one monitor runs at a time; normal worker slot limits stay in
effect. A successful `team_read` is required before the monitor can
finish; that result includes child tickets, live progress, and compact recent
tool history so the lead can identify stalled work or repeated read loops. The
lead can guide existing workers with `team_send` or `team_note`, and answer
existing questions with `team_reply`, scoped to the
assigned job. The `--monitor` MCP surface permits reads and those scoped
coordination actions, but blocks authorship, redelegation, and result delivery.
The lead returns to waiting after monitoring, and normal resume/synthesis
continues when child work is ready.

The heartbeat also recovers a stale persisted failed lead whose child work is
unfinished, including one that exhausted normal ticket retries before a server
restart. Paused jobs and jobs assigned or tagged for a human stay paused.

Monitor runs have a 180-second lifetime from ACP startup, enforced by forced
connection cleanup as well as the prompt timeout. Synthesis uses a normal worker
slot if children finish between scheduling and starting the monitor. Existing `agent_runs` and
`agent_runtime` records are reused; no database migration is intended.

## Implementation path

- Entry points and relevant modules: Orchestrator composer and session API;
  `coms/server.py`, `coms/runner.py`, and `coms/acp.py` own stop handling,
  watchdog recovery, and ACP process cleanup. Model discovery must preserve
  busy sessions bound to project workspaces.
- Data or state changes, including migration behavior: canceled turns and
  heartbeat runs use existing transcript, `agent_runs`, and `agent_runtime`
  records. No schema migration is intended.
- Interfaces affected (dashboard, HTTP API, CLI, agent tools, configuration):
  dashboard composer Stop control; `POST /api/runner/sessions/<sid>/stop`;
  monitor-specific MCP tools add child/progress/history context through
  `team_read` and scope `team_send`/`team_note`/`team_reply` to the assigned job.

## Verification

- Twenty new backend checks pass. Coverage exercises Stop, startup/ignored cancellation,
  transcript and worker preservation, timer scoping, EOF cleanup, model
  discovery, dead-connection recovery, stale/failed lead wakeups, full-capacity
  monitor scheduling, and scoped MCP tools.
- The UTF-8 Python suite runs 172 tests with four existing thinking-transcript assertion
  failures and one skip, reproduced on unchanged HEAD (152 tests).
- Browser suite: 22 tests, 15 passed and 7 failed. The unchanged 21-test baseline
  has the same seven polling/tool-rendering failures. Focused Stop, Send and
  Heartbeat-stage browser checks pass (3/3).
- JavaScript syntax and Git whitespace checks pass. Docker image builds
  successfully. Applying the updated image requires a server restart.

## Investigation evidence

The stored Orchestrator turns on 10 October 2026 match the reported
`rc=None` and `rc=-15` errors. EOF could mark a connection closed before the
process exited, causing the old cleanup method to skip termination and reaping.
Refreshing models could also replace a busy project connection with a root
workspace connection and terminate its process. Both paths have regression
coverage. The retained logs do not identify the original sender of SIGTERM;
these fixes address reproducible failure paths rather than proving which path
caused each historical exit.

## Maintenance notes

ACP process cleanup must still terminate and reap a process when stdout reaches
EOF even if `poll()` reports `None`; EOF alone does not prove the child has
exited. Keep monitor stage identity distinct in run/progress views, preserve
the waiting lead state after monitoring, require a successful `team_read`, and
retain the 180-second timeout and monitor tool restrictions.
