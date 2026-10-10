# Feature: Root-folder skills manager

## Behavior and contracts

The Skills page manages repository-root `skills/<slug>/` folders. A skill is
listed when its folder contains a UTF-8 `SKILL.md`. Nested files are available
in the file browser and imports preserve their relative paths. Skill content is
the source of truth; SQLite stores agent-read counters and audit events only.
The hidden `.disabled` marker keeps a skill visible in the manager while
excluding it from the index supplied to new agent runs.

All agent roles can call `skill_list` and `skill_get`. The orchestrator and
ticket workers receive the enabled skill index in their startup context, then
read full instructions on demand. Only the human operator and orchestrator can
create, edit, import, enable, disable, or delete skills. Workers share verified
methods with the orchestrator, who publishes a new root skill during team-job
synthesis when required by the delivery gate.

## Implementation path

- Entry points and relevant modules: `web/app.js`, `web/app.css`,
  `web/index.html`, `coms/server.py`, `coms/api.py`, `coms/skill_store.py`,
  `coms/mcp.py`, `coms/runner.py`, and `coms/team.py`.
- Data or state changes, including migration behavior: the files live under
  `skills/`; `skill_accesses` records per-agent reads. On first runner startup,
  old `shared_skills` rows with no matching folder are copied once into
  `skills/`, then `runner_settings.skills_filesystem_migrated` prevents the
  old table from repopulating the folder after later deletions.
- Interfaces affected: `/api/skills` and the existing skill MCP tools now use
  folder-backed content. The manager supports search, create, edit, import,
  enable/disable, file browsing, context estimates, refresh, and delete.

## Verification

- Focused tests: not added or run for this change.
- Regression checks and results: Python byte-compilation, JavaScript syntax
  check, and `git diff --check` passed. No automated test suite was run.

## Maintenance notes

`skill_store.py` validates every slug and relative file path, refuses symlinks,
and keeps reads/writes within the repository skills root. Docker mounts the
same repository `skills/` folder at `/app/skills` with write access because the
image's root filesystem stays read-only. `coms/` and `COMS_*` remain internal
source/config names; the product-facing name is Swarm Agent.
