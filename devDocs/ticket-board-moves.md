# Feature: Ticket board status moves

## Behavior and contracts

The Tickets page's Board view supports dragging a card into any of its six
status columns. Each card also exposes a native **Move to** select for keyboard
and touch users. Dropping onto the original column, cancelling a drag, or
dropping external content does not send a ticket update. This changes ticket
status within the selected project board.

Moves send only `status` to the existing `PATCH /api/tickets/<id>` route with
the dashboard's selected actor. Assignment, execution readiness and results
are retained. The backend still owns validation, timeline and activity records,
closure timestamps, and warnings about open child tickets. A column move has
the same semantics as editing status on the ticket detail page.

Only one move may save at a time. Cards remain in their original column while
saving; a rejected or failed request keeps the original status and displays an
error. Successful moves redraw the columns, update their counts and retain the
selected board, filters and URL. Tickets moved out of a status filter leave
the visible set. Keyboard focus returns to the ticket's select, or the Board
view button if the ticket is no longer visible.

## Implementation path

- `web/app.js`: the ticket index route mounts `kanban` and `wireKanban`.
  `kanbanColumns` renders status targets and draggable cards. Delegated native
  drag/change handlers share one `moveTicket` operation.
- Move state belongs to the current route. The handler checks its route owner
  before work and after asynchronous requests; route cleanup clears drag state.
  A response from an old route cannot redraw the current page.
- A successful save updates the loaded ticket model and redraws only the grid.
  The moved ticket is placed first to remain visible in capped columns. Column
  counts include all matches; overflow notices show the display limit (15 for
  closed columns, 100 for others).
- `web/app.css`: paper cards, hard borders/shadows, cyan drop targets, yellow
  saving feedback, and violet keyboard focus match the app's design.
- `coms/server.py:r_ticket` and `coms/api.py:update_ticket` are reused without
  backend or schema changes. CLI and agent-tool contracts are unchanged.

## Verification

Acceptance cases: status persistence, same-column/cancel/external no-ops,
failed saves and retry, duplicate-save prevention, filter retention, keyboard
focus, capped-column visibility, and route changes during a request.

Existing browser regression coverage in `tests/browser.test.cjs` checks
switching between List and Board views. No automated tests were added or run
for this change because the active instruction requires a user request for
tests. JavaScript syntax, source/diff review, Docker build and deployed asset
checks are the validation steps for this revision. The web preview remains
closed.

## Maintenance notes

Keep status changes in the shared backend operation. Do not replace drag/drop
with a separate persistence mechanism. Preserve a native control for users
who cannot use drag-and-drop, and retain failure feedback without displaying
an unconfirmed column move. The grid does not poll; reopening the page loads
the latest changes made by agents or other dashboard users.
