# Feature: Orchestrator stop and recovery

## Behavior and contracts

While an Orchestrator response is active, the composer exposes **Stop**. It
cancels the active ACP turn and uses a bounded force-close fallback if the
backend does not stop cleanly. The transcript records the canceled turn and
preserves the existing conversation and unsent composer draft so the user can
steer the same session. Stopping an orchestrator response does not cancel
independent child worker runs.

To stop a whole team, the operator, Orchestrator, or assigned job lead calls
`team_stop(job_id)` (HTTP: `POST /api/teams/<job_id>/stop`). It atomically sets
the job and descendants to `ready_for_execution=0`, holds unfinished tickets
as `blocked`, and cancels queued/running/waiting runs, pending deliveries and
reviews. Completed results, artifacts, transcripts and unrelated jobs remain
available. Descendants follow both `parent_id` and `source_ticket_id`, with
cycle protection. Repeating the request is safe.

The runner checks held connections on each scheduling tick, including ACP
startup, team replies and final review, and closes them. Startup and prompt
boundaries recheck the hold. Cancelled status is terminal for that run: late
notifications and cleanup cannot change it to waiting or failed. Heartbeats,
automatic retries and reply delivery cannot restart held work, including after
a server restart. A held ancestor also blocks executing a child reenabled on
its own. Creating new work from a held execution context is rejected; creation
and stop serialize through SQLite's write lock so descendants cannot escape
the stop traversal. The MCP tool permits reads of stopped work but rejects
new writes from those workers.

The Orchestrator's instructions require `team_stop` when asked to stop or kill
a team and require checking `stopped=true`. Process termination and textual
STOP notes do not change execution eligibility. Resuming requires an explicit
operator action to reopen and enable the desired job/worker tickets; cancelled
reply and review requests require fresh requests. No schema migration is used.

Chat activity remains visible while descendant workers run, wait, or queue,
including descendants of descendants. This activity is distinct from an active
ACP response: an idle chat accepts steering while its workers continue. The
shared circular indicator and provenance contract are documented in
[`agent-activity.md`](agent-activity.md).

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
lead can guide existing workers with `team_note`, and answer
addressed Questions records with `questions_answer` before `team_reply`, scoped to the
assigned job. The `--monitor` MCP surface permits reads and those scoped
coordination actions, but blocks authorship, redelegation, and result delivery.
The lead returns to waiting after monitoring while child work is still
unfinished, and normal resume/synthesis continues when it is ready; when all
child work is ready, the monitor finishes completed so synthesis can run.

The heartbeat also recovers a stale persisted failed lead whose child work is
unfinished, including one that exhausted normal ticket retries before a server
restart. Paused jobs and jobs assigned or tagged for a human stay paused.

Monitor runs have a 180-second lifetime from ACP startup, enforced by forced
connection cleanup as well as the prompt timeout. Synthesis uses a normal worker
slot if children finish between scheduling and starting the monitor. Existing `agent_runs` and
`agent_runtime` records are reused; no database migration is intended.

Peer reviews also have one reserved response slot, tracked separately from the
heartbeat slot and normal worker capacity. An orchestrator review uses an
isolated collaboration session with shared/role guidance and the author's
assignment; it can run while the main chat or another orchestrator execution is
active. Each identity still receives at most one collaboration turn at a time,
and ordinary replies retain busy guards. Response prompts keep their existing
180-second timeout, bounded retries and durable stop checks. See
[team collaboration](team-collaboration.md).

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
  `team_read` and scope notes/Questions answers/replies to the assigned job.

Team convergence checks run every 15 seconds before scheduling/recovery. The
durable `team_progress` table supports a 30-minute no-progress hold and one-hour
lifetime hold, with environment overrides. Distinct successful source URLs,
accepted revisions and delivered results advance progress; chatter and retries
do not. Unassigned, unready children and low temporary space also hold jobs
visibly. Holds use recursive stop and remain disabled across restarts. Main chat
creates only the root job; runner dispatch owns staffing to avoid duplication.

OpenCode inference uses a bounded priority queue shared by chat, workers,
responses and final reviews. ACP children extract into private temporary
directories; `close` removes only its owned directory after exit. Startup rejects
less than 16 MiB free, and idle orchestrator recovery avoids respawning under low
space. These controls complement per-turn watchdogs. See
[bounded team execution](bounded-team-execution.md) for migrations, review
budgets, artifact freeze and configuration defaults.

## Verification

`tests/test_team_stop.py` adds 16 regressions for recursive/provenance holds,
cycle handling, permissions, idempotence, HTTP/MCP access, run/delivery/review
cancellation, startup and cleanup races, ancestor holds and restart persistence.
The focused recovery/tool/activity run passed 52 checks with one platform skip.
An earlier full-suite snapshot had four thinking-transcript failures and nine
browser failures. ACP thought chunks now stay in opt-in diagnostics, legacy
thought rows are hidden from public chat responses, and browser coverage follows
the current UI and SSE fallback behavior. That full checkout snapshot passed
all 257 Python tests (two platform skips) and all 31 browser tests.

On 11 October 2026, the live audit job #10 and its twelve worker tickets were
backed up and held. Its waiting runs and pending deliveries were cancelled.
The subsequent database inspection showed no active audit runs or deliveries
and no other running jobs before deployment.

- Twenty new backend checks pass. Coverage exercises Stop, startup/ignored cancellation,
  transcript and worker preservation, timer scoping, EOF cleanup, model
  discovery, dead-connection recovery, stale/failed lead wakeups, full-capacity
  monitor scheduling, and scoped MCP tools.
- JavaScript syntax and Git whitespace checks pass. The current test run used
  isolated temporary databases; it did not change the running Docker service.

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
