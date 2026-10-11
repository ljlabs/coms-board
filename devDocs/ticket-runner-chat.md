# Feature: Ticket runner chat

## Behavior and contracts

The ticket page mounts `OrchestratorChat.mount` with a ticket ID and container.
It uses the same transcript renderer, Markdown, tool panels, copy actions,
composer, activity status, selection protection, and scroll handling as the
orchestrator page. Ticket conversations poll persisted records; orchestrator
conversations retain their existing streaming transport and project controls.
User messages also expose Edit message with Save and regenerate. Editing keeps
the selected message ID, drops later transcript entries, closes the old ticket
connection, and clears saved session IDs before replying from retained public
history. Execution/review records and board/tool side effects remain intact;
regeneration is a conversation follow-up. Fork controls belong only to the
orchestrator page. See [chat message actions](chat-message-actions.md).

Execution responses, review decisions, and follow-ups are shown in chronological
order. Message IDs remain stable across polls and reloads. Raw diagnostics are
available in the opt-in Debug logs section. Progress entries identify their
stage and source run, so an execution and its review are visibly distinct even
when the same agent handles both. Schedule history has a View chat link.

Follow-ups reuse a live ticket session or restore the latest completed execution
session after a restart. Session replay is suppressed because local rows are
canonical. If loading is unsupported or fails, a new session receives the ticket
and recent conversation history. A follow-up reserves a backend slot, refuses
concurrent execution/review, and never creates another execution run. Errors are
persisted visibly and the slot is released.

## Implementation path

- `coms/ticket_chat.py` adapts `agent_messages` to the shared message contract.
  `/api/tickets/<id>/runner` returns responses, tool payloads, labels, availability,
  busy state, runs, and progress. `?include_logs=1` opts into diagnostics.
- `coms/runner.py` restores sessions and records replies through `progress.Recorder`.
- `coms/progress.py` derives stage from the runtime key and associated run.
- `web/orchestrator-chat.js` owns both conversation shells and their behavior.
  `web/app.js` mounts it on tickets and links scheduled executions to the chat.
- Existing SQLite tables are reused; no schema migration is needed.

## Verification

- `tests/test_ticket_chat.py`: stored answers, tool payloads, review formatting,
  diagnostics, progress stages, and follow-ups after loss of the live connection.
- Ticket browser regressions cover shared rendering, replies and reloads,
  scroll/draft preservation, and deep links.
- On 2026-10-10, all six ticket backend checks and six selected browser checks
  passed, including the three ticket checks and orchestrator send/mobile/delete
  regressions. JavaScript syntax and whitespace checks passed. The Docker board
  was rebuilt and verified healthy; ticket #9's stored reply was verified in UI.
- The broad Python run (UTF-8 enabled) had four failures in existing thinking
  transcript assertions, and the broad browser run had seven failures in older
  polling/dialog/tool expectations. All eleven failures reproduced on unchanged
  HEAD `e20a8ca`. Tests use temporary databases and a fake ACP process.
- The latest full checkout run passes all 257 Python tests (two platform skips)
  and all 31 browser tests, including ticket polling, drafts, and shared chat
  rendering.

## Maintenance notes

Keep ticket message normalization compatible with the shared conversation
contract. Review responses remain part of the transcript and are labeled
separately from execution. Preserve raw historical records rather than rewriting
old assistant messages when correcting a delivered result.
