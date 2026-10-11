---
name: ticket-management
description: Create actionable board tickets, choose agents and templates, track progress, and deliver verified results.
builtin: true
trigger_words: ["ticket", "tickets", "task", "tasks", "backlog", "acceptance criteria"]
---

# Ticket management

Search existing board records before opening work. Read `agent_templates` and
`agents_list` before creating an execution ticket. Write a concrete title and
body with the requested outcome, scope, dependencies and acceptance criteria.
Choose p0/p1/p2/p3 from urgency and impact. Use `ticket_create` with `ready=true`
when the user asked for actionable work to proceed. Planning drafts, blocked
dependencies and work reserved for a human stay unready.

Use stories for related jobs, jobs for coordinated work and child tasks for
focused assignments. Children inherit their parent's board and workspace.
Set explicit assignees and use `ticket_link` for supporting wiki pages,
questions, tickets or URLs. Use `ticket_comment` for progress and blockers.

Inspect and execute the assigned work with native tools. Publish durable
findings on the board. Call `ticket_result` with delivered IDs, links and actual
verification only after the assignment and its delivery gates are satisfied.
If waiting for a teammate or peer review, finish the turn without delivering;
the runner resumes the ticket when the response arrives. Never claim a failed
tool call completed the work.
