# Feature: Chat message actions

## Behavior and contracts

The shared `OrchestratorChat` component exposes Edit message on user text
messages in both orchestrator and ticket conversations. Its inline editor keeps
drafts through polling, supports Cancel/Escape, rejects blank text, and reports
save errors without discarding the draft. Save and regenerate replaces the
selected message in place, removes the later transcript, and starts a reply.
Busy/invalid requests are rejected before changing history. Tool side effects,
ticket executions, and board changes are not undone.

Fork chat appears only on the orchestrator page, on settled public assistant
text messages and terminal system errors. Thinking, plans, tool cards, and user
messages are not boundaries. The source must have no active turn. Forking while
another chat uses the orchestrator is allowed; sending must wait for that slot.
The new chat opens in the same project, titled `Fork of: {source title}`. Every
stored entry through the boundary is copied, including tools, thinking, plans,
and diagnostics; later entries are excluded and the source is unchanged.

## Implementation path

- `web/orchestrator-chat.js` owns reusable action controls and the inline editor.
  The orchestrator mount supplies a fork callback; ticket mounts do not.
- `coms/orchestrator_transcript.py` atomically edits/truncates or copies the
  transcript. Edited user IDs stay stable and versions increase for SSE patches.
  Forks remap message IDs, turn IDs, and turn user pointers; a cut inside a turn
  completes that copied turn at the boundary. Legacy rows without turns work.
- `coms/runner.py` validates idle/workspace/session availability, reserves the
  normal response slot, and starts regeneration through existing runner paths.
  Edited and forked chats clear ACP identities. Ticket edits close their old
  connection and clear saved session IDs, then run a follow-up without creating
  another execution. Fresh sessions receive all retained public conversation,
  including tool results, without the former history length limits. Diagnostics
  and private thinking are stored in forks but excluded from public replay.
- HTTP: `PATCH /api/runner/sessions/<sid>/messages/<mid>` and
  `PATCH /api/tickets/<tid>/runner/messages/<mid>` accept `{body: string}` and
  return the normal accepted-response payload.
  `POST /api/runner/sessions/<sid>/messages/<mid>/fork` returns the new session.
  Message IDs are scoped to their chat; no ticket fork endpoint exists.
- Existing SQLite tables are reused. No migration or runtime dependency is added.

## Verification

`tests/test_chat_message_actions.py` covers copy boundaries, payload preservation,
turn/ID remapping, legacy rows, project/workspace/title, source isolation,
stable edits, suffix removal, ACP restoration, full replay, routes, invalid
inputs, busy state, and ticket regeneration without another execution.
Browser regressions cover edit/cancel/reload, composer and editor drafts,
fork/sidebar behavior, failed saves, busy controls, and ticket-only editing.
Interactive review is left to the user as requested; no browser tools are used.

On 2026-10-11, all 16 new backend checks and four new browser checks passed.
The isolated feature snapshot's full Python suite ran 215 checks: 209 passed,
two skipped, and four older thinking-transcript assertions failed. Each failure
also reproduced on unchanged HEAD. The full browser suite passed 20 of 28;
the same eight older polling/tool-panel/skill-control failures reproduced on
the unchanged 24-test baseline (16 passed). No new failure appeared. JavaScript
syntax and Git whitespace checks passed. Concurrent activity-indicator changes
in the shared checkout were excluded from this comparison and feature commit.
After the concurrent change was committed, all eight selected shared-checkout
chat regressions passed, including edit/fork, ticket follow-ups, Stop, send/reload,
and mobile layout.

## Maintenance notes

Never restore the previous ACP session after a transcript edit or fork: its
hidden history includes messages beyond the retained boundary. Keep scope and
boundary validation on the backend as well as the UI. Do not attach delegated
worker jobs to a fork or replay tool side effects. Do not rewrite assistant
answers when editing user input; regenerate through the normal response path.
