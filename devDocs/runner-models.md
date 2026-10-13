# Feature: Runner model selection

## Behavior and contracts

Runner settings saved in SQLite take precedence over Docker bootstrap defaults.
`COMS_LLAMACPP_BASE_URL` and `COMS_LLAMACPP_MODEL` supply the initial local API
settings and model only when their corresponding database keys are absent.
An explicit empty model stays empty; saved API keys remain private and the HTTP
API reports only `opencode_api_key_set`. Imported workspace paths are still
validated inside the container and fall back to `/workspace` when invalid.

The HTTP API and execution runner use `api.runner_settings(conn)` for the same
effective configuration. Settings writes compare these effective values when
deciding to invalidate sessions. Writes still reject active workers or an active
Orchestrator response.

OpenCode selections use the advertised full `provider/model` ID. Hosted provider
IDs are passed as the inline OpenCode default and applied to each ACP session.
The local `coms-local` provider remains available in the model catalog. A stale
local selection can recover to an advertised local default when the endpoint
changes its model ID. An unavailable hosted selection raises an error instead
of silently selecting a different provider. See [worker execution](worker-execution.md)
for live local catalog and context-window handling.

The dashboard retains the saved selection after discovery and reload, including
an explicit **Use ACP default** choice. The note identifies the current ACP
default when available. If a saved ID is missing from the catalog, the picker
shows a selected unavailable entry and preserves that ID on unrelated saves.
Discovery failures and pending discovery do not overwrite the saved model.

## Implementation path

- `coms/api.py`: shared effective settings, with no schema migration or writes
  during reads.
- `coms/server.py`: `/api/runner/settings` reads, key redaction and writes.
- `coms/runner.py`: effective settings, inline backend environment, ACP model
  validation and discovery cache identity.
- `web/app.js`: Runner model discovery and selection display.

No CLI or MCP model-setting operation is added. Persistent HTTP setting names
and stored model IDs retain their existing format.
The Docker image pins `opencode-ai` to the version recorded in `Dockerfile` so
rebuilds do not silently pick up a different ACP implementation.

## Verification

`tests/test_runner_settings.py` covers Docker defaults, saved selections and
keys, explicit empty selections, workspace fallback, HTTP save/reload and the
active-worker guard. `tests/test_worker_execution.py` covers hosted config,
session application, invalid hosted selections and local-only recovery.
`tests/browser.test.cjs` covers hosted save/reload, explicit default display and
preservation of missing saved models.

These tests cover Docker defaults, saved model selections, hosted configuration,
HTTP save/reload, picker behavior, and the active-worker guard. Docker image
builds and installed-backend acceptance remain separate checks from the Python
and browser suites.

## Maintenance notes

Do not reintroduce forced Docker model overrides or broad OpenCode model
fallbacks. The provider in a user's saved selection is part of their intent.
Docker environment defaults and database values must not be resolved separately
by HTTP and execution paths. Model discovery cache keys include the selection.

OpenCode documents the model format and configuration precedence in its
[model configuration guide](https://docs.opencode.ai/docs/models/).
