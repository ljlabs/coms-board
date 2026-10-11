# Feature: Root-folder skills manager

## Behavior and contracts

The Skills page manages repository-root `skills/<slug>/` folders. A skill is
listed when its folder name is a valid slug, neither the folder nor its
`SKILL.md` is a symlink, and `SKILL.md` is a UTF-8 file within the size limit. Nested files are available
in the file browser and imports preserve their relative paths. Skill content is
the source of truth; SQLite stores agent-read counters and audit events only.
The hidden `.disabled` marker keeps a skill visible in the manager while
excluding it from the index supplied to new agent runs.

Six built-in workflows are shipped as read-only skill folders:
`swarmboard`, `ticket-management`, `knowledge-management`,
`scheduled-agent-tasks`, `team-orchestration`, and `source-backed-research`.
Their `builtin: true` frontmatter labels them in the manager. The bundled slug
catalog also protects these files if their frontmatter is removed outside the
application. Storage rejects content writes, creation, moves and deletion even
for the operator/orchestrator; API and MCP reuse those checks. The page disables
file-edit controls and dragging. Enabled state and triggers remain configurable.
Built-in trigger overrides live in a hidden `.trigger-words.json`, leaving
bundled `SKILL.md` bytes unchanged. The effective content version includes this
override, so concurrent trigger updates still reject stale revisions. Custom
skill triggers are saved in their manifest. Files remain the source of truth.

`trigger_words` frontmatter stores a JSON array of words or phrases (an indented
YAML list is also accepted). The manager edits this field separately and can set
it during creation. Up to 32 entries of 1–80 characters are accepted; control
characters are rejected, whitespace is normalized and case-insensitive
duplicates are removed. An empty or absent list means on-demand loading only.
`PATCH /api/skills/<slug>` accepts `{trigger_words: [...], version}` using the
skill's current effective content version (for built-ins with trigger
overrides this includes the override, not just the manifest). Creation and `skill_put` accept optional
`trigger_words`; direct manifest edits and imports validate the same metadata.

Each orchestrator message, ticket assignment/review, ticket follow-up and team
response is matched against enabled skills at prompt construction time. Matching
ignores case, accepts whitespace between phrase words, and requires word
boundaries rather than substrings. Each matching skill's complete `SKILL.md`
is included once and counted in `skill_accesses` for the receiving agent.
Supporting files still load through `skill_file_get`. Matching uses only the
current user text, assignment title/body, or teammate message; generated indices
and restored history never activate new loads. Resumed ACP sessions can retain
previously supplied context; disabling a skill prevents future injection.
Injected instructions remain subject to the turn's role and tool permissions.

The file panel lists empty directories as well as files. Select a folder,
then use **File** or **Folder** to create an entry inside it. **Rename** keeps
the parent directory; **Move** selects a destination folder. Files and folders
can also be dragged onto another folder or the **Skill root** drop target.
Moving a folder preserves its subtree, including empty folders and binary
files. Renaming or moving the file being edited preserves its unsaved text.
Switching files or skills, refreshing, or starting a new entry asks for
confirmation before discarding an unsaved edit.

All agent roles can call `skill_list`, `skill_get`, and `skill_file_get`,
including in collaboration and dispatch sessions. The orchestrator and
ticket workers receive the enabled skill index in their startup context, then
read full instructions on demand or receive them through matching trigger words.
Only the human operator and orchestrator can create, edit, import or delete
custom skills, or configure trigger/enabled settings. Workers share verified
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
- `tests/test_skill_triggers.py` covers built-in metadata, validated and versioned
  trigger changes, boundary/phrase matching, disabled skills, usage tracking,
  read-only enforcement and settings isolation, invalid imports, and injection
  into orchestrator, worker, teammate and resumed ticket conversations.
- Headless browser tests exercise file creation, rename, folder and root
  drops, persistence after reload, and unsaved draft retention. They use the
  real HTTP routes and never open a visible web preview.
- Earlier Windows checks passed the focused Skills Python and browser cases but
  recorded failures in unrelated transcript and chat checks. The latest full
  checkout run passes all 257 Python tests (two platform skips) and all 31
  browser tests, including the built-in skill read-only controls.

## Maintenance notes

`skill_store.py` validates every slug and relative file path, refuses symlinks,
and keeps reads/writes within the repository skills root. Docker mounts the
same repository `skills/` folder at `/app/skills` with write access because the
image's root filesystem stays read-only. `coms/` and `COMS_*` remain internal
source/config names; the product-facing name is Swarm Agent.
