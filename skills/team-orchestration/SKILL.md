---
name: team-orchestration
description: Coordinate an orchestrator led team, delegate focused work, and complete peer reviewed synthesis.
builtin: true
trigger_words: ["team", "delegate", "delegation", "teammate", "peer review", "synthesis"]
---

# Team orchestration

Use this procedure for collaboration required jobs.

## Lead

Reuse suitable teammates with `agents_list`. In the staffing turn, use `team_delegate` to register or reuse a personally named specialist and create a ready child assignment. Put expertise in capabilities. Finish staffing before workers start, then record the staffing decision with `team_note`.

Delegation defaults to `task` for workspace work and inherits the job workspace.
Set `type=research` for public-source research. Every agent keeps native tools;
use them to inspect code and run requested commands. For local audits, record
relevant file lines with `source_read_file` and cite the returned URL/source ID.
Use `source_fetch` for relevant public HTTPS evidence. Reviewers inspect both
kinds with `source_get` and can check current files with native tools.

`team_read` returns a bounded index with pending requests. Use `message_id` and
`offset` for message details, `source_after_id` to page sources, and `source_get`
for full evidence. Repeatedly polling unchanged output does not advance work;
perform the assignment or record a concrete blocker and finish the turn.

The lead delegates child tickets and waits for worker results and reviews. Only the lead owns the synthesis. After all workers finish, write a fresh combined wiki page, link it to the job, obtain a current revision peer review, and deliver with `ticket_result`.

Treat pending reviews as urgent: the author cannot deliver until a real
`team_review` decision arrives. Workers normally request the registered
orchestrator from `team_roster`; an explicitly assigned qualified peer is also
valid. Review your workers promptly in the assigned response turn. The runner
prioritizes reviews and reserves one response slot even when workers are full.
An orchestrator review uses a separate ACP session with the same orchestrator
role guidance plus the reviewed assignment, workspace, team and skill context;
it can run while the main orchestrator chat is busy. Decide the assigned review
and end that turn before resuming ordinary work. The lead's own synthesis must
be reviewed by a different teammate. A failed response attempt stays visible
and retries are bounded; inspect its error instead of claiming approval or
repeatedly creating duplicate requests.

When `team_read` shows the delivery exhausted its retries, the author may request
a fresh current-revision review from another qualified registered teammate.
Notify the lead of the failure and alternate reviewer. If none is available,
record the concrete blocker on the ticket; do not claim approval or bypass it.

If the human asks to stop or kill the team, call `team_stop(job_id)` and verify
`stopped=true`. This holds all descendants and cancels runs, replies and reviews
without deleting results. Process kills and STOP notes do not disable the
runner queue. Do not recreate or reenable stopped work without a human request.

## Workers and reviewers

Put reusable questions on the Questions page first, so later tasks can find the
answers. Search with `board_search`, read a matching question with
`questions_get`, or use `questions_ask` for a standalone question with context,
the ticket it unblocks and the evidence needed. Link it to your ticket with
`ticket_link(kind=question, ref=the question ID)`. Post its ID/link in `team_note`
to invite insights, then use `team_roster` and `team_send` to ask a relevant
teammate to answer there. Save and link the record before `team_send`, which
pauses your turn; recipients are awakened automatically. Keep staffing,
handoffs and review requests in the team conversation.

For example, ask which less familiar Monte Carlo sights would help a travel
guide on the Questions page, link it to your research ticket, then notify the
Riviera team with `#/questions/<id>` and ask the relevant specialist to
contribute evidence. The record should make sense without the active chat.

Responders read `questions_get`, save findings, source citations and limitations
with `questions_answer`, then call `team_reply` with the ID/link and a short
summary to release the sender. Response turns can answer questions linked to
their job or any of its child tickets, including a peer's ticket, but cannot
create questions. Verify the ticket link with `ticket_get` and reuse existing
answers. For older chat-only questions, reply normally; the resumed asker saves reusable findings
on a board question. Resumed workers read `team_read`, publish their own wiki
pages, link them to their ticket, and request a review of the current revision
with `team_request_review`. Sending a question or requesting peer review pauses
the sender until a response arrives.

Reviewers inspect the exact page with `wiki_get` and any cited evidence with `source_get`, then use `team_review` with concrete acceptance or requested changes. Revise rejected work before requesting another review. Do not edit an artifact after it receives approval; a later edit needs a fresh review.

After the reply arrives and the unchanged linked page has an accepted review,
call `ticket_result` on your own ticket with the artifact/question IDs and actual
verification. Check that the tool succeeded, then end the worker turn. Pending
replies or reviews require ending the turn without `ticket_result`; the runner
resumes it automatically.

Publish new shared procedures only when they describe methods the team actually verified. Avoid filler greetings and completion claims that the recorded work does not support.
