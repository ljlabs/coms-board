# Feature: Durable team questions and prompt peer review

## Behavior and contracts

The Questions page holds reusable questions and evidence-backed answers across
tasks. Team messages coordinate attention, handoffs and review decisions. Shared
instructions require searching/reading existing Q&A first, creating or reusing a
standalone question, and linking it to the assigned ticket before `team_send`
pauses the worker. A `team_note` invites team insights with the question ID and
`#/questions/<id>` link. Responders save findings, citations and limitations with
`questions_answer` before `team_reply` releases the sender. Older chat-only
requests remain valid; resumed askers preserve useful findings on the Questions
page. New agent notifications require a linked `question_id` and originating
ticket; structured replies require a saved board answer. Questions are optional
and address real gaps. Legacy messages are retained. See
[bounded team execution](bounded-team-execution.md) for guards and budgets.

Collaboration response tools expose `questions_list`, `questions_get` and
`questions_answer`. Writes require an assigned message addressed to the actor,
a question linked to the same job or one of its child tickets, and an active
job. A peer's ticket link qualifies. Questions remain board-wide records; no
schema change to Questions is needed. Team messages add nullable question/ticket
fields, and HTTP team sends accept these optional fields. Response turns cannot create
questions, edit wiki pages, delegate or deliver ticket results. Existing
monitor/dispatch tool restrictions remain in force.

Workers normally select the registered orchestrator as their wiki reviewer;
explicit specialist reviewers remain supported. Leads select another qualified
teammate for their own synthesis. Authors finish and link the page before
requesting review, keep it unchanged while waiting, and deliver with
`ticket_result` immediately after accepted current-revision review and any
real pending question is resolved. Questions and skills are optional.
Reviewers inspect `wiki_get` and cited `source_get` evidence and record
`team_review` with concrete findings. Existing revision, evidence, authorship
and self-review checks remain unchanged. Honest access limitations do not
justify invented verification or unsupported completion claims.

The scheduler considers peer reviews before ordinary replies and ticket work.
One reserved peer-response slot is available even at full worker capacity,
in addition to the existing separate heartbeat slot. Other responses use the
normal worker limit; each recipient still has only one collaboration turn at a
time. Specialist reviewers wait while their own execution is active. An
orchestrator review can run concurrently with its execution/final-review work
or main chat because it uses an isolated ACP connection and scoped tools.
Ordinary orchestrator replies retain the busy-session guards.

Response prompts carry common collaboration rules, registered identity and
specialty instructions, workspace and shared skill index. Ordinary replies also
receive triggered skills and compact team context; review packets omit these
and include only assigned criteria/cited-source metadata. Orchestrator responses
receive the same role prompt as main chat. `ticket_get` supplies criteria beyond
the 4,000-character review preview. This is role context without chat replay.
The response contract overrides general role instructions and confines the turn
to answering or deciding the assigned review.

Response prompts retain their 180-second timeout and three-attempt retry limit
with backoff. These bounds do not guarantee an immediate or successful backend
decision. Failed attempts remain visible; after exhaustion, authors may notify
the lead and request one qualified alternate, or record a concrete blocker.
Equivalent requests reuse attempt counts; each revision allows two reviewers,
with six requests per ticket/page across revisions. Manual artifact revisions
cancel obsolete pending reviews without ACP work.
Stops and server recovery preserve durable cancellation/resumption rules.

## Implementation path

- `coms/runner.py`: `SHARED_COLLABORATION_PROMPT` precedes worker, follow-up,
  staffing, heartbeat, orchestrator and team-response role/turn guidance.
  `orchestrator_system_prompt` supplies chat and isolated reviewer role context.
  `_schedule_team_replies` reserves/tracks one peer-response slot; `_worker`
  releases it on every outcome. `_reply_team` uses a separate session, includes
  the reviewed assignment, and records/retries actual tool decisions.
- `coms/team.py`: delegation and resumed context teach durable question routing.
  `ticket_links` identify job questions; delivery needs actual current-revision
  wiki/evidence approval but no compulsory question or new skill.
- `coms/mcp.py`: tool descriptions match the workflow, and collaboration answer
  writes are scoped to questions linked to the message's active job.
- Built-in `team-orchestration`, `swarmboard` and `source-backed-research` skills,
  root `SKILL.md` and `README.md` carry the same workflow. Skill folders remain
  the source of truth; editing a database skill copy is insufficient.
- Dashboard, HTTP and CLI use existing Q&A, ticket-link and review operations.
  UI is unchanged. Team-message migrations and required agent `question_id`
  arguments are documented in bounded team execution.

## Verification

`tests/test_collaboration_guidance.py` uses isolated temporary SQLite records and
mock ACP connections. It covers question persistence/sender release,
foreign/unlinked/unassigned/stopped answer rejection, prompt/context injection,
full-capacity/busy-orchestrator review scheduling, single-recipient limits,
reserved-slot cleanup and visible bounded retries. Existing worker/team/stop/
monitor/skill/orchestrator suites preserve evidence and lifecycle coverage.
On 12 October 2026, the focused run passed 101 tests with one platform skip.
After four additional guard cases, the full Python suite passed all 271 tests
with two platform skips. Windows runs used normal temporary-directory access
because existing fixtures cannot open SQLite files under sandbox `AC/Temp`.
No dashboard/browser changes required a browser-suite run.

Written-skill dry runs compare old/new procedures for a research question, a
board-linked answer and a timed-out review. They evaluate instructions, not
actual ACP backend performance. Updated skills explicitly cover the ordering
absent from the old guidance. No live board, agent backend or deployed service
is used by these checks.

## Maintenance notes

Keep shared prompts and built-in skills consistent. `team_send` pauses the
sender, so question creation/linking must precede it. `questions_answer` alone
does not release a waiting worker; the response must also call `team_reply`.
Review priority must preserve bounded capacity, cleanup, stop checks,
single-recipient execution and exact-revision evidence inspection. Do not reuse
the active orchestrator chat connection for a peer response.
