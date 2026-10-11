# Feature: Agent sources and delegated activity

## Behavior and contracts

Live agent progress appears on Agents and other diagnostic views; Runner is
limited to backend controls and execution configuration. Each progress record
can identify its spawning agent and link to its originating scheduled task or
Orchestrator chat.

Running, waiting, and queued ready work propagate to every ancestor through
both ticket hierarchy and recorded execution origins. Chat sidebar entries and
scheduled tasks show a circular indicator on their right while their work
remains active, including grandchildren after a lead finishes its own turn.
Inside a chat, the existing Lottie animation appears while awaiting an answer
or delegated work. Waiting for children does not reserve an ACP slot or disable
steering an otherwise idle chat. Stop continues to apply only to a currently
running orchestrator response.

Schedule history and individual execution chats share this activity signal.
Indicators clear when the related work finishes; unrelated chats/schedules
remain idle. Reduced-motion preferences show a static circle in the sidebar
and a fixed frame of the Lottie artwork inside the conversation.

## Implementation path

- `tickets.source_ticket_id` and `tickets.source_session_id` are additive,
  default-empty SQLite migrations. MCP ticket creation records the assigned
  worker ticket or the current running chat turn. Execution origin is preserved
  even when an agent creates work without a hierarchy parent.
- `coms/progress.py` aggregates all work before the progress view's 80-row limit
  and traverses ancestry with cycle protection. Immediate spawning identity
  comes from the ticket requester, with parent lead identity as a legacy
  fallback. Scheduled runs establish the schedule link.
- Legacy chat origins are recovered only from exact successful ticket-creation
  or delegation tool outputs. Missing/malformed/failed output is not evidence
  for assigning work to a chat. Explicit provenance takes precedence; copying
  a transcript into a fork does not reassign the original work.
- Chat lists/details and scheduled task/run APIs expose `working` and
  `activity` counts (`active_count`, `waiting_count`, `queued_count`).
  `/api/runner/status?activity=1` returns activity maps for tickets, chats, and
  schedules. Progress adds `source`, `schedule`, and `orchestrator_session`.
- `web/app.js` renders source links and schedule indicators;
  `web/orchestrator-chat.js` polls activity even with a healthy transcript
  stream, preserves sidebar groups, and keeps response controls tied to the
  agent's own busy turn. It retains the conversation's Lottie animation and
  pauses it when hidden, respects reduced motion, and destroys it on cleanup.
  `web/app.css` supplies the sidebar and schedule circles and Lottie sizing.

A durable `team_stop` cancels live runtime records and holds ticket execution.
Late ACP notifications cannot overwrite a cancelled runtime record. Held
blocked tickets contribute no activity, so their chat/schedule indicators clear.
The response-only Stop control retains its existing semantics; see
[`orchestrator-resilience.md`](orchestrator-resilience.md).

## Verification

Focused backend regressions cover nested work, schedule/chat isolation,
waiting/completion, queued work, source recording without a hierarchy parent,
legacy outputs, cycles, schema migration, and activity beyond 80 progress rows.
JavaScript syntax is checked. Browser regression cases are provided but are
left for user testing under the explicit instruction not to run browser tools
or browser tests.

The 48 focused backend checks pass; the Lottie correction also reran all ten
activity regressions successfully. An earlier 11 October full-suite snapshot
had four existing thinking-transcript assertions failing on unchanged HEAD
`016eac5`. The latest full checkout run passes all 253 Python tests (two
platform skips) and all 31 browser tests, including nested chat activity and
Stop visibility while child work continues.

## Maintenance notes

Do not equate visible delegated activity with an active ACP turn. Preserve
`busy`, `runner_busy`, and Stop semantics. Never infer a source chat solely from
the shared orchestrator agent identity. Backend changes require restarting the
server; coordinate that restart around active agents.
