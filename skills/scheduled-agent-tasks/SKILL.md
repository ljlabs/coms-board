---
name: scheduled-agent-tasks
description: Create and manage recurring agent prompts with a selected agent, valid workspace, recurrence and timezone.
builtin: true
trigger_words: ["schedule", "scheduled", "recurring", "cron", "daily", "weekly"]
---

# Scheduled agent tasks

Use `schedule_list` and `schedule_get` to find an existing task before creating
a duplicate. Confirm the requested recurrence and timezone from the user's
instructions and board context. Select a registered non-human agent using
`agents_list` and use the existing valid workspace from the current context
or the user's request.

Use `schedule_create` for a new recurring prompt. Specify a clear name, the
full actionable prompt, agent_slug, workspace, timezone and schedule_type.
For interval use interval_every and interval_unit; for weekly use weekly_days
and weekly_time; for cron use a five-field cron_expression. Use
`schedule_update` to change an existing task while retaining fields the user
did not ask to change.

Use `schedule_pause`, `schedule_resume`, `schedule_run_now`, or `schedule_delete`
only for the requested lifecycle action. Read the runs returned by
`schedule_get` for execution history and inspect linked tickets for results.
Each firing queues a ticket for the selected agent; it does not guarantee
execution has finished. Report the
saved schedule ID and next run returned by the tool.
