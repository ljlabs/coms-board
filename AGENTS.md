# AGENTS.md

Always follow the change workflow in `devDocs/README.md` for every change — including its review, test, regression, doc-update, and commit-checklist steps. Before changing code, also read `devDocs/SYSTEM_OVERVIEW.md` and the relevant `devDocs/` feature note, and trace the implementation and its tests. Tool entry points are listed in `devDocs/AGENT_INSTRUCTIONS.md`; `CLAUDE.md` imports this file plus the workflow.

## Stack

- Backend is Python 3.10+ standard library + SQLite only — do not add runtime dependencies without a documented need. No lint or typecheck; GitHub Actions runs the Python and browser regression suites.
- `coms/` — backend (`db.py` schema/migrations, `api.py` shared operations, `server.py` HTTP routes, `cli.py` CLI, `runner.py`/`acp.py`/`mcp.py`/`team.py` agent execution, `workspace.py`, `schedules.py`, `skill_store.py`). Full module map in `devDocs/README.md`.
- `web/` — dependency-free dashboard (no framework/CDN). `skills/<slug>/SKILL.md` is the source of truth for shared skills (DB copy is one-time import only). `tests/` — stdlib `unittest` only. `data/coms.db` — local runtime DB, gitignored, never commit.

## Commands

```sh
python3 -m unittest discover -s tests          # full regression; run before committing behavior changes
python3 -m unittest tests.test_api             # single file during implementation
```

- Windows: use `py -3 ...` if `python3` is missing; set `PYTHONUTF8=1` when reading repo sources.
- Hook (runs suite at commit): `git config core.hooksPath .githooks`.
- Browser suite (dashboard changes only; uses temp DB, never prod data): `npm ci`, `npx playwright install chromium` if needed, then `npm run test:browser`.
- Server: `python3 -m coms.server` (or `bash scripts/serve.sh`); binds `127.0.0.1:8765`, no auth. CLI: `python3 coms.py --as <agent> <group> <cmd>` (`--as` or `$COMS_AGENT` identity is required for writes). Env: `COMS_DB` (default `data/coms.db`), `COMS_PORT`.
- Docker: `docker compose up --build -d` serves `127.0.0.1:18765`. While in Docker use `.\scripts\coms.ps1 --as admin ...` — plain `python .\coms.py` reads the seed `data/coms.db`, not the live volume. Never `docker compose down --volumes` unless erasing board data.

## Conventions

- Keep validation/business rules in the shared backend (`coms/api.py`) so dashboard, HTTP API, and CLI agree. Trace all entry points for a feature (domain op, route, CLI command, dashboard control).
- Treat SQLite schema, HTTP JSON, CLI flags/output, and MCP tool names/args as persistent interfaces: add a migration for schema changes and test migrate-from-old plus fresh-DB; update callers, tests, and docs on contract changes.
- Tests must use isolated temp databases/mocks — never a developer's `data/coms.db`, credentials, or a live agent backend. Put new `TemporaryDirectory` fixtures under the project root when the system temp directory is not writable.
- Every code change must update the relevant `devDocs/` notes in the same commit so the docs stay a complete, current record of the full system and all its features. If no note covers the changed area, add one from `FEATURE_TEMPLATE.md` and index it in `devDocs/README.md`; correct any stale claim the change exposes (numbers, tool names, limits, routes).
- One commit = code + tests + doc updates (plus `README.md`/`SKILL.md` when user- or agent-facing behavior changes). Pre-commit check: `git diff --cached` shows only intended hunks and `git diff --cached --check` passes.

## Gotchas

- Workspace paths (`-ws`/`--workspace`) must be absolute, existing, and outside the app dir and its parents (symlinks to those rejected). Agents run with the server user's filesystem permissions and inherit its environment — validation is not a sandbox. Docker workspace is read-only unless `.env` sets `SWARM_WORKSPACE_READ_ONLY=false`.
- Always skip the knowledge base: never run `knowledge.py`, ignore `rules/knowledge.md`.
- Python scripts: write to a `.py` file in the project root, then execute it. Never `python -c`, heredocs, or stdin.
- Debugging: skim `rules/` before nontrivial debugging. If the same failure survives 3+ attempts, log `rules/YYYY-MM-DD-short-slug.md` once resolved (Symptom / Failed approaches / Fix / Signal, a few sentences each).
- Planning: consult the `planning-protocol` skill (`.Codex/skills/planning/SKILL.md`, else skill catalog) when creating, refining, accepting, or implementing a plan.
