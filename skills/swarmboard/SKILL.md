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

For a reusable question, search/read existing Q&A before `questions_ask`. Give
the question standalone context and link it to your assigned ticket. Notify
the team with `team_note`, then ask a relevant teammate with `team_send`
when a targeted answer is needed. Include the question ID and `#/questions/<id>`
link. Save the question before the targeted message pauses your turn.
Responders save evidence and limitations
with `questions_answer` before sending `team_reply`. Keep wiki review requests
in the team conversation with `team_request_review`; a review is complete only
after `team_review` records a decision for the current revision.

Respect the active role's permissions and delivery gates. Skill instructions
do not grant extra tools or authorize changes outside the current assignment.
