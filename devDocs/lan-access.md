# Feature: LAN access for the Docker board

## Behavior and contracts

- Default stays loopback-only: Compose publishes
  `${SWARM_BIND:-127.0.0.1}:${SWARM_PORT:-18765}:8765`, and an empty
  `COMS_TRUSTED_HOSTS` changes nothing, so existing setups behave exactly as
  before.
- Opt-in: `SWARM_BIND=0.0.0.0` in `.env` publishes the board on all host
  interfaces; each browser-facing hostname or IP must additionally be listed
  in `COMS_TRUSTED_HOSTS` (comma-separated, no ports), otherwise the server
  answers `403`.
- Trust check (`coms/server.py` `_trusted_request`): the request hostname is
  `localhost`/loopback OR listed in `COMS_TRUSTED_HOSTS`, AND the request
  port equals `COMS_PUBLIC_PORT` (Compose sets it to `SWARM_PORT`). `Origin`
  must still match the `Host`, and `Sec-Fetch-Site: cross-site` is still
  rejected. Hostname matching is case-insensitive; unparseable hosts are
  rejected.
- The container already listened on `0.0.0.0` (`Dockerfile` CMD plus the
  `COMS_IN_DOCKER` bind exception); only port publishing and request trust
  changed.
- There is no authentication: anyone reaching a trusted host and port has
  full board control. Treat LAN access as trusted-network-only and revert
  afterwards.
- Windows host: publishing the port is not enough by itself. Windows
  Defender Firewall shows no prompt for this traffic, so add a manual
  inbound TCP allow for the port (see README for the exact command), then
  verify from the host via `http://<lan-ip>:<port>/` before testing the
  phone. A timeout on the LAN address while `127.0.0.1` answers points at
  the firewall, not the container.

## Implementation path

- Entry points and relevant modules: `coms/server.py` (`_trusted_request`),
  `compose.yaml` (`ports`, `board.environment`), `.env.example`.
- Data or state changes, including migration behavior: none; no migration.
- Interfaces affected (dashboard, HTTP API, CLI, agent tools, configuration):
  configuration (`SWARM_BIND`, `COMS_TRUSTED_HOSTS`); HTTP trust behavior for
  non-loopback `Host` values. Dashboard, CLI, and agent tools are unchanged.

## Verification

- Focused behavior tests and their file paths:
  `tests/test_server.py::test_listed_trusted_host_accepted_others_rejected`
  (listed host allowed; unlisted host, wrong port, and foreign origin
  rejected; unlisted hosts still rejected without the variable).
- Regression commands and results: `python3 -m unittest discover -s tests`
  (244 tests). Five pre-existing problems unrelated to this change, each
  reproduced identically on the unmodified checkout via `git stash`:
  `test_structured_chat_api_keeps_errors_visible_and_logs_opt_in` (extra
  `thinking` chunk),
  `test_orchestrator_keeps_one_codex_process_and_overrides_stale_chat_backend`,
  `test_orchestrator_restores_history_when_backend_cannot_load`,
  `test_private_thoughts_and_unknown_events_are_diagnostics`, and
  `test_schedule_source_and_activity_propagate_to_grandchildren` (error).
  All trust tests, including the pre-existing
  `test_cross_origin_and_untrusted_host_rejected`, pass.
- Checks not run: browser suite (no dashboard change).

## Maintenance notes

Keep this note and the README LAN subsection aligned with
`_trusted_request` and `compose.yaml`. If the trust check ever accepts new
host forms, update both places here. Add the feature note to the index in
`devDocs/README.md` (done).
