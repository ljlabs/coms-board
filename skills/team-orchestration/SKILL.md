---
name: team-orchestration
description: Coordinate an orchestrator led team, delegate focused work, and complete peer reviewed synthesis.
---

# Team orchestration

Use this procedure for collaboration required jobs.

## Lead

Reuse suitable teammates with `agents_list`. In the staffing turn, use `team_delegate` to register or reuse a personally named specialist and create a ready child assignment. Put expertise in capabilities. Finish staffing before workers start, then record the staffing decision with `team_note`.

The lead delegates child tickets and waits for worker results and reviews. Only the lead owns the synthesis. After all workers finish, write a fresh combined wiki page, link it to the job, obtain a current revision peer review, and deliver with `ticket_result`.

## Workers and reviewers

Workers use `team_roster` and `team_send` for substantive questions. Recipients are awakened automatically. Sending a question or requesting peer review pauses the sender until the response arrives. Resumed workers read `team_read`, publish their own wiki pages, link them to their ticket, and request a review of the current revision with `team_request_review`.

Reviewers inspect the exact page with `wiki_get` and any cited evidence with `source_get`, then use `team_review` with concrete acceptance or requested changes. Revise rejected work before requesting another review. Do not edit an artifact after it receives approval; a later edit needs a fresh review.

Publish new shared procedures only when they describe methods the team actually verified. Avoid filler greetings and completion claims that the recorded work does not support.
