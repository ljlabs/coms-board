# Development guide for agents

This folder documents how the swarm application works and how to change it
safely. Read this guide and any relevant feature document before editing code.
Use the repository `README.md` for setup and user-facing behavior, and inspect
the implementation and tests to confirm that the documentation still matches
the code.

Start with [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) for the architecture and
request paths. Use [`FEATURE_TEMPLATE.md`](FEATURE_TEMPLATE.md) when a change
adds durable behavior that needs its own design note.

## Documentation index

- [System overview](SYSTEM_OVERVIEW.md): components and request paths.
- [Agent instruction setup](AGENT_INSTRUCTIONS.md): which files tools load and
  how to maintain the shared guidance.
- [Scheduled agent tasks](scheduled-agent-tasks.md): recurrence and execution
  contracts.
- [Ticket runner chat](ticket-runner-chat.md): shared conversation rendering,
  stage labels, and follow-ups after a restart.
- [Ticket board moves](ticket-board-moves.md): drag-and-drop status changes and
  keyboard/touch controls.
- [Shared skills manager](skills-management.md): skill-folder storage and agent
  access.
- [Feature template](FEATURE_TEMPLATE.md): starting point for a new feature note.

## Change workflow

Follow these steps for every feature or bug fix. Keep the code, its tests, and
the documentation updates for one cohesive change together in the same commit.

1. **Review the current design.** Read this guide and relevant `devDocs`
   feature notes first. Trace the existing behavior through the relevant code,
   tests, and public interfaces (dashboard, HTTP API, CLI, or agent tools).
   Check `README.md` and `SKILL.md` when user-facing or agent-facing behavior is
   involved. If no feature note exists yet, derive the design from the code and
   add a note when the change introduces durable behavior or a new subsystem.
   Define acceptance cases before implementation and inspect `git status` for
   existing work in the shared checkout.
2. **Make a focused change that fits the system.** Reuse the surrounding
   module's patterns and existing helpers. Preserve existing interfaces and
   defaults unless the feature requires a documented change. Keep unrelated
   refactors out of the feature change. This application uses Python's standard
   library and SQLite for its backend and a dependency-free browser UI; avoid
   adding runtime dependencies without a clear need.
3. **Add tests for the behavior.** Add or update focused unit tests in `tests/`
   for the new behavior, including relevant invalid input, boundary, and error
   cases. Use the existing `unittest` style and isolated temporary databases or
   mocks where appropriate. Do not make tests depend on a developer's real
   database, credentials, or running agent backend. Exercise the feature's
   observable behavior and preserve the regression case for a bug fix.
4. **Check for regressions.** Run the full Python suite:

   ```sh
   python3 -m unittest discover -s tests
   ```

   On Windows, use `py -3 -m unittest discover -s tests` if `python3` is not
   available. Run focused tests during implementation, then the full suite
   before committing a behavior change.

   For dashboard or browser interaction changes, also run the browser suite
   described in the root `README.md` (`npm ci`, install Playwright Chromium if
   needed, then `npm run test:browser`). Fix failures caused by the change; if a
   check cannot be run, record that clearly rather than reporting it as passed.
5. **Record and maintain the design docs.** Update the relevant feature note
   whenever behavior, interfaces, data shape, or implementation flow changes.
   Add a feature note for new durable behavior using
   [`FEATURE_TEMPLATE.md`](FEATURE_TEMPLATE.md). Keep it concise and describe
   contracts and relationships that help the next agent make a correct change,
   not a line-by-line copy of the source. Update other applicable docs too:
   `README.md` for setup and user-facing behavior, and `SKILL.md` for agent
   instructions. Add new feature notes to the documentation index above.
6. **Review the commit as a unit.** Before committing, inspect the complete
   staged diff and complete the checklist below. Stage the intended files or
   hunks explicitly when other work is present in the checkout.

For changes limited to documentation or instruction configuration, validate
local links, configured file paths, and whitespace. These changes need no new
application unit tests. An enabled repository hook may still run the Python
suite at commit time.

## Before each commit

- [ ] The implementation follows the existing design and meets its acceptance
  cases.
- [ ] Focused feature tests and applicable regression checks have been run;
  any failures or unavailable checks are recorded accurately.
- [ ] Feature notes reflect the current behavior, interfaces, data, and code
  flow; affected setup and agent instructions are updated too.
- [ ] `git diff --cached` contains the intended code, tests, and docs, and
  `git diff --cached --check` passes.

Use the commit summary to record what changed, why, which checks ran and their
results, and the relevant documentation pages. Keep feature notes as living
design documents; Git history records the sequence of changes.

## Codebase map

Use this map to find the first relevant code and tests, then follow imports and
callers to understand the full behavior.

| Area | Starting points |
| --- | --- |
| SQLite schema, connections, and migrations | `coms/db.py`, `tests/test_db.py` |
| Application operations and HTTP API | `coms/api.py`, `coms/server.py`, `tests/test_api.py`, `tests/test_server.py` |
| Command-line interface | `coms/cli.py`, `tests/test_cli.py` |
| Agent execution, tools, orchestration, and team collaboration | `coms/runner.py`, `coms/team.py`, `coms/acp.py`, `coms/mcp.py`, related `tests/test_*.py` |
| Orchestrator transcripts and live progress | `coms/orchestrator_transcript.py`, `coms/progress.py`, `tests/test_orchestrator_transcript.py` |
| Shared skill folders | `coms/skill_store.py`, `coms/api.py`, `devDocs/skills-management.md` |
| Workspace rules and scheduled work | `coms/workspace.py`, `coms/schedules.py`, related tests |
| Browser dashboard and assets | `web/`, `tests/browser.test.cjs`, `tests/test_questions.py` |
| Agent-facing product instructions | `SKILL.md` |

The test file names are a starting point, not an exhaustive ownership map. Find
all callers and tests for a behavior before changing a shared contract.
