# Local model team workflow

## Symptom
The local 2.6B model created orphan drafts without registering teammates, kept working while replies were queued, and attempted native file edits during a board response turn.

## Failed approaches
More detailed collaboration prompts did not reliably enforce registration, parent IDs or readiness. Structured delegation fixed staffing, but instructions to yield still left recipients busy. MCP filtering alone left OpenCode native tools available.

## Fix
Use a delegation-only first turn with team_delegate creating registered, ready child assignments. Pause execution automatically after durable questions or review requests. Constrain research/response sessions to board tools and prevent specialists from overwriting another author's wiki page. Require actual replies, retrieved evidence and current-revision peer approval before delivery.

## Signal
Inspect tool arguments, delivery queues and artifact authors; names and confident summaries alone do not demonstrate collaboration.
