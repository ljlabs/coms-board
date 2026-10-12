# Feature: Bounded team execution

## Behavior and contracts

Main chat creates the ready root job and ends. Runner dispatch is the single
staffing owner and uses `team_delegate` once per specialty before workers start.
MCP rejects child staffing/delegation from unassigned chat. Delegation serializes
its checks and creation: an identical scope returns its prior ticket, and a
differently titled scope for an agent with an active child is rejected. Completed
scopes are reused; a new title needs `followup_reason` naming an unmet acceptance
criterion, and each teammate/job allows one follow-up. The reason is recorded in
the child assignment. MCP assigned team turns cannot create raw tickets, including
orphan copies that omit `parent_id`. Chat cannot create parented work. The lead
owns synthesis; workers perform their own assignments.

Team requests use `type=job`. MCP `ticket_create` normalizes a request with
`recommended_workers` to a collaborative job, even if a model specifies another
type. Shared API/HTTP/CLI reject team size on non-job creation/type changes;
legacy malformed parents can still be paused/reviewed without rewriting history.

Questions are optional and address actual information gaps. Search/read existing
Q&A, create or reuse a standalone question, link it to the assigned ticket, and
announce its ID/link with `team_note` before targeted `team_send`. The agent tool
requires `question_id`, with optional `ticket_id` for an unassigned caller. The
shared domain validates existence, ticket ownership, job membership and the link.
Equivalent pending/responding/answered notifications reuse the message and pause
only their originating ticket. Coordination uses notes. Structured responders
save an answer on their assigned Questions record before replying; legacy
messages remain readable/respondable. HTTP adds optional question/ticket fields;
CLI Questions/link operations are unchanged.

Each active ticket owns its linked artifact even if another assignment uses the
same agent. Assigned wiki writes preserve the page while review is pending and
after acceptance. Identical writes retain its revision. Manual writes, moves or
deletion retire obsolete requested/failed reviews and cancel delivery/run without
starting ACP. Historical accepted decisions remain visible with `current=false`.
Held/completed jobs do not freeze a resubmitted assignment. Page writes, review
checks/decisions, delegation and notifications serialize through SQLite's write
lock; rejected operations release their lock.

Workers normally request the orchestrator; leads need an independent reviewer.
Reviewers read the exact page with `wiki_get` and cited successful evidence with
`source_get` before `team_review`. Review acceptance, worker delivery and focused
review packets share `team.source_is_cited`: nonempty recorded original/final
URLs or explicit `source 45` / `source #45` references qualify. IDs match exactly,
case insensitively; bare numbers do not qualify. Successful retrieval, same-job
ownership, current-run reads and exact artifact revision remain required.
Empty URLs cannot qualify uncited evidence. Equivalent review requests reuse
the message/attempt budget. Each revision allows two reviewers (normally one
plus an alternate), with six requests
total per ticket/page across revisions. Each delivery retains its three-attempt
backoff. Exhaustion requires a blocker instead of repeated fresh requests.

Once the unchanged linked evidence page is accepted and no real request remains
pending, call `ticket_result`, verify success and end. `api.deliver_ticket`
serializes readiness/evidence validation and result/status writes. Success moves
open/in-progress work to review immediately; MCP provenance rejects subsequent
execution work from reviewed/ended tickets. A successful delivery event cancels
the model turn. Backend errors after recorded delivery still complete the run.
No answered question or new
skill is required. Leads still wait for completed child reviews, write a fresh
synthesis and obtain independent current-revision approval. An unassigned,
unready unfinished child causes a visible hold before ACP startup.

Child readiness and synthesis routing apply to every parent type, including
legacy research/task parents. Waiting parents remain quiet until children are
ready. Successful result delivery takes precedence over the dispatch child-wait
branch. Legacy waiting runs with a recorded result revalidate and enter review
without replay; superseded evidence resumes synthesis for correction. Final
review continues to require resolved children.
Final-review scheduling excludes held parent/provenance ancestry before the
candidate limit and rechecks it before queuing or incrementing attempts, so held
reviews cannot hide eligible work. A parent hold preserves child review budgets;
existing exhausted reviews can be restored through the explicit final-review
retry operation.
An explicit retry stores `queued` with zero attempts. The scheduler dispatches
that state as attempt one, while ordinary queued/running attempts remain reserved
against duplicate dispatch. It does not run execution again.

## Scheduling, context and budgets

Peer responses retain one reserved slot, alongside the heartbeat slot and normal
worker capacity. Identity/busy guards and isolated orchestrator sessions remain.
OpenCode prompt turns also share a cancellable inference queue
(`COMS_MODEL_PARALLEL_TURNS`, default 2, range 1–16). Priority is reviews, then
chat/replies, workers and monitors. Running turns are not preempted. Queue
acquisition has its own timeout, separate from the ACP prompt deadline.

Review packets retain orchestrator role guidance where applicable, reviewed
criteria (4,000 characters with `ticket_get` for full content), cited-source
metadata and shared skill index. They omit unrelated room history and full
triggered skills; procedures remain available through `skill_get`. Other turns
inject a roughly 10,000-byte team index; `team_read` retains its 31,900-byte budget.
Final review uses bounded discussion/transcript previews and tools for full
criteria/artifacts, without recursively copying prior prompts. Its decision is
parsed only from the final assistant response: tool activity or an explicit ACP
message ID change separates prior commentary, which remains in the transcript.
The final response must still be a valid JSON object with `close` or
`human_review`; prose, malformed JSON and missing decisions fail visibly.
Response/final review retains 180-second deadlines; execution/synthesis retains
900 seconds.

The runner checks durable progress and temporary space every 15 seconds before
dispatch/recovery. `team_progress` stores a fixed start, last progress and high
water counts of distinct successful source URLs, accepted artifact revisions and
nonempty results. Chatter, wiki edits, repeated sources and retries do not advance
them. Active agent jobs hold after 1,800 seconds without progress or 3,600 seconds
total. Environment/Compose overrides `COMS_TEAM_NO_PROGRESS_SECONDS` and
`COMS_TEAM_LIFETIME_SECONDS` have a 60-second minimum. Explicitly enabling a held
job resets the budget; descendants still require explicit reopening/enabling.
Human-tagged/assigned jobs are excluded.

Holds use recursive team stop, cancel work and add a concrete comment. They
persist across runner restart. Low temporary space (below 16 MiB) also holds
agent teams and prevents ACP startup. Each subprocess owns a `coms-acp-*`
directory with child `TMPDIR`/`TMP`/`TEMP` set to it. Cleanup removes only that
directory after process exit. Docker recreation clears legacy tmpfs leakage
while preserving board and agent-state volumes. See
[orchestrator resilience](orchestrator-resilience.md).

## Implementation and migration

- `coms/runner.py`: shared/role/turn prompts, focused reviews, single dispatch,
  reserved response slot, inference queue integration and recurring guards.
- `coms/team.py`: artifact ownership, structured question/review contracts,
  delivery gates, stale retirement, budget accounting and durable holds.
- `coms/db.py`: nullable `team_messages.ticket_id` and `question_id` migrations
  preserve legacy messages. `team_progress` is created for fresh/old databases.
  The write decorator provides atomic checks/writes with rollback.
- `coms/api.py`, `coms/mcp.py`, `coms/server.py`: provenance-aware wiki writes,
  explicit-resume budget reset, required agent question arguments and additive
  HTTP fields. Monitors permit notes and answers to questions addressed to that
  lead in its job, but cannot send questions or expand work.
- `coms/turn_queue.py`, `coms/acp.py`, `compose.yaml`: bounded inference turns,
  owned temporary directories, low-space checks and environment defaults.
- Built-in workflow skills, root `SKILL.md` and `README.md` match completion
  rules. Skill folders remain authoritative; database copies are imported once.

## Verification and investigation

`tests/test_team_convergence.py` covers staffing, structured Q&A and migration,
artifact freeze/ownership, stale retirement, review budgets across revisions,
concurrent duplicate requests, optional questions/skills, durable progress holds,
draft dependencies, low space, focused packets and owned temporary cleanup.
`tests/test_turn_queue.py` covers review priority and queued cancellation without
capacity leakage. Existing collaboration, monitor, worker, stop, skill and
orchestrator regressions preserve tool/evidence/lifecycle contracts. No dashboard
changes require browser tests.

`tests/test_parent_completion.py` reproduces the ticket #10 replay: main chat
created `research`, collaboration disabled, but team size three. Repeated execute
turns created three sets of the same destinations despite successful results.
Cases cover legacy parent waiting/synthesis, delivery before waiting, stored
result recovery, terminal publication, child gates, MCP/API team shape, structured
staffing including orphan bypass, bounded follow-ups and model cancellation/errors.
Scheduler ticks verify held parents do not queue child final reviews or consume
their attempts, while unrelated eligible reviews still run at capacity one.
The real retry API plus a scheduler tick verifies an exhausted final review is
dispatched after an explicit retry instead of remaining queued indefinitely.

The 12 October job #1 audit found duplicate staffing scopes, an unassigned draft
dependency, stale accepted/pending revisions, no delivered result across dozens
of runs and review retry budgets reset by new requests. Most communication was
coordination, while a real research question was correctly saved and answered on
the board. Native extractions filled the 128 MiB Docker tmpfs; early review
timeouts predated exhaustion. The operator stopped the job durably and will
resubmit after deployment; its records are preserved.

Validation on 12 October 2026: the full Python suite ran 319 tests successfully
with two platform skips, including seventeen completion/replay/hold regression cases.
The earlier collaboration/lifecycle run passed 102 checks with one skip.
Compose configuration and staged whitespace checks passed.
Windows regression runs used `PYTHONUTF8=1` and normal temporary-directory access
for existing SQLite fixtures. Unit checks use isolated databases and mock ACP;
they do not establish the local model's future research quality.

The job #24 investigation found successful source reads rejected because the
page cited source IDs instead of URLs. Final reviews for two other children
emitted valid closing JSON after progress commentary, but concatenating all
messages made that JSON fail validation. `tests/test_review_evidence.py` covers
both stalls, focused source metadata, exact ID matching, failed/foreign/unread
evidence rejection, streamed final messages and invalid/missing decisions.
Existing exhausted attempts remain historical records; deploying a fix does
not automatically reopen reviews or bypass the explicit retry controls.
The focused review/collaboration/convergence run passed 49 tests; the full
Python suite passed 336 tests with two platform skips. No dashboard changes
required browser tests.
