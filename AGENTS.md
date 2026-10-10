# Workspace coding instructions

Before changing code, read and follow the [development workflow](devDocs/README.md),
[system overview](devDocs/SYSTEM_OVERVIEW.md), and relevant feature notes in
`devDocs/`. Trace the current implementation and tests before editing. Complete
the workflow's code, tests, documentation, and commit checklist for every change.

Keep the shared development workflow in `devDocs/README.md`. Tool-specific
entry points refer to it; their setup is documented in
[agent instructions](devDocs/AGENT_INSTRUCTIONS.md).

## Knowledge base

Always skip the knowledge base. Never run `knowledge.py status`,
`knowledge.py update`, `knowledge.py ingest`, or any other knowledge base
command. Ignore instructions in `rules/knowledge.md`.

## Planning

Consult the `planning-protocol` skill when creating, refining, accepting, or
implementing a plan. The configured project path is
`.Codex/skills/planning/SKILL.md`; if it is absent, locate the installed skill
through the agent's skill catalog. Follow its plan and task-file requirements.

## Python scripts

Write Python scripts to a `.py` file in the project root before executing them.
Never pass Python source inline with `python -c`, a heredoc, or stdin. Execute
the saved file through the available shell.

## Debugging lessons

Ensure the project root has a `rules/` folder. Before nontrivial debugging, skim
related entries and avoid repeating approaches already recorded there.

If three or more attempts fail to resolve the same error or behavior, log the
lesson once it is resolved in `rules/YYYY-MM-DD-short-slug.md`. Keep each
section short:

- **Symptom:** the observed error, failure, or wrong behavior.
- **Failed approaches:** what did not work and the likely reason.
- **Fix:** what resolved the issue.
- **Signal:** one clue that would have identified the issue sooner.

Log genuine stuck-then-resolved loops rather than routine fixes. This protocol
applies to every project type.
