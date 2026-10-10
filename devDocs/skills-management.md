# Feature: Root-folder skills manager

## Behavior and contracts

The Skills page manages repository-root `skills/<slug>/` folders. A skill is
listed when its folder contains a UTF-8 `SKILL.md`. Nested files are available
in the file browser and imports preserve their relative paths. Skill content is
the source of truth; SQLite stores agent-read counters and audit events only.
The hidden `.disabled` marker keeps a skill visible in the manager while
excluding it from the index supplied to new agent runs.

The file panel lists empty directories as well as files. Select a folder,
then use **File** or **Folder** to create an entry inside it. **Rename** keeps
the parent directory; **Move** selects a destination folder. Files and folders
can also be dragged onto another folder or the **Skill root** drop target.
Moving a folder preserves its subtree, including empty folders and binary
files. Renaming or moving the file being edited preserves its unsaved text.
Switching files or refreshing prompts before discarding an unsaved edit.

All agent roles can call `skill_list`, `skill_get`, and `skill_file_get`,
including in collaboration and dispatch sessions. The orchestrator and
ticket workers receive the enabled skill index in their startup context, then
read full instructions on demand. Only the human operator and orchestrator can
create, edit, import, enable, disable, or delete skills. Workers share verified
methods with the orchestrator, who publishes a new root skill during team-job
synthesis when required by the delivery gate.
Read-only sessions can read skills and supporting files without updating
SQLite usage counters.

`SKILL.md` cannot be renamed, moved, or replaced by another entry. Create and
move operations refuse case-insensitive name collisions, traversal, hidden
and Windows-reserved names, symlinks, missing destination folders, and moves
of a folder into itself. Each operation runs under the existing per-skill lock.
`get_skill` returns `directories` and a `tree_version` calculated from the
visible tree and file metadata. Create and move requests must supply that
revision and fail if another edit has changed it. File saves continue using
the existing content version. New entries respect the 1 MiB file, 8 MiB skill,
100 file, and 100 directory limits.

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
  `PATCH /api/skills/<slug>` accepts `{action: "create", path, kind,
  tree_version, content?}` or `{action: "move", source, destination,
  tree_version}`. Responses include the updated skill, entry `path`, and
  `kind`. The matching manager MCP tools are `skill_entry_create` and
  `skill_entry_move`. `skill_file_get` reads a supporting file and
  `skill_file_write` updates it. These tools use the same authorization and
  validation as the page.

## Verification

- Focused tests: `tests/test_skill_files.py` checks tree persistence, moves,
  binary content, collisions, manifest protection, stale revisions, invalid
  paths, limits, symlinks, HTTP routing, and agent access. Fixtures keep skills
  and SQLite records in temporary directories.
- Headless browser tests exercise file creation, rename, folder and root
  drops, persistence after reload, and unsaved draft retention. They use the
  real HTTP routes and never open a visible web preview.
- Focused checks pass on Windows and Linux, including all 14 filesystem and
  agent-access tests on Linux and both skill editor browser tests. The full
  suites have four transcript failures in Python and seven chat/tool failures
  in the browser; each also reproduces on the prior commit. Detailed results
  are recorded in this change's commit description.

## Maintenance notes

`skill_store.py` validates every slug and relative file path, refuses symlinks,
and keeps reads/writes within the repository skills root. Docker mounts the
same repository `skills/` folder at `/app/skills` with write access because the
image's root filesystem stays read-only. `coms/` and `COMS_*` remain internal
source/config names; the product-facing name is Swarm Agent.
