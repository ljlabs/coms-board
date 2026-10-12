---
name: swarmboard
description: Use SwarmBoard to find board context and record plans, progress, decisions and results through the shared board tools.
builtin: true
trigger_words: ["swarmboard", "swarm board", "swarm agent", "board context"]
---

# SwarmBoard

The board is the team's shared record: wiki pages hold durable knowledge,
tickets hold actionable work, and questions hold requests for answers.

Start with `board_search` for the topic and read the relevant records before
creating duplicates. Use the attached Swarm Agent MCP tools for board reads
and writes. Inspect code and run commands using native workspace tools; record
their relevant findings on the board rather than leaving deliverables in chat.

Choose a ticket for work with acceptance criteria, a wiki page for a reusable
decision or procedure, and a question when an answer is needed. Link the
records with `ticket_link`. Report the actual IDs and verification returned by
successful tools. If a tool fails, describe the observed blocker accurately.

Questions are optional: do your assignment yourself and ask only for an unresolved information gap. For a reusable question, search/read existing Q&A before `questions_ask`. Give
the question standalone context and link it to your assigned ticket. Notify
the team with `team_note`, then ask a relevant teammate with `team_send(question_id=the ID)`
when a targeted answer is needed. Include the question ID and `#/questions/<id>`
link. Save the question before the targeted message pauses your turn.
Responders save evidence and limitations
with `questions_answer` before sending `team_reply`. Keep wiki review requests
in the team conversation with `team_request_review`; a review is complete only
after `team_review` records a decision for the current revision.

Keep a page unchanged while it is reviewed. Once your linked page is accepted,
call `ticket_result`, verify success and end. No question or new skill is required
to finish. Use `team_note` for scheduling and blockers; do not restart exhausted
reviews or create extra research scopes. Main chat creates the root job; only
the runner's staffing turn delegates children.

Respect the active role's permissions and delivery gates. Skill instructions
do not grant extra tools or authorize changes outside the current assignment.
