# Feature: Worker tools and evidence

## Behavior and contracts

Every agent retains its backend's native tool support. For OpenCode, the runner
selects the built-in `build` agent and explicitly allows `read`, `grep`, `glob`,
`bash`, `edit`, `webfetch`, and `websearch` in both global and build-agent
permissions. Inline settings override inherited project/global tool denials,
including legacy `tools=false` settings. This common environment applies to
workers, collaboration, dispatch, monitoring, model discovery and orchestrator
sessions. Scoped MCP writes still enforce board identity and turn
responsibilities; workspace filesystem permissions still govern commands.

The runner sets `OPENCODE_ENABLE_EXA=1`: OpenCode otherwise omits native
`websearch` for a custom provider such as `coms-local`, even with permissive
tool settings. Search uses a hosted search service (Exa by default) while model
inference can remain local. It needs outbound network access, without a separate
search API key. `websearch` discovers URLs and `webfetch` reads them; neither
replaces the collaboration evidence records made with `source_fetch`.
Shared board procedures load through MCP `skill_get`, not the backend's native
skill catalog. Failed tool arguments must be corrected or reported as blockers.

### OpenCode local model metadata

Before starting an OpenCode ACP session, `coms/runner.py` reads the configured
OpenAI-compatible `/v1/models` catalog. The server's advertised `id` is the
provider model key and the display name is only presentation. For each entry,
the runner registers a valid live context limit (`meta.n_ctx`, or a supported
context-length field) in OpenCode's `limit.context`. For llama.cpp entries where
the model catalog does not include a live context, it reads
`/props?model=<id>` and uses `default_generation_settings.n_ctx`. It never uses
`meta.n_ctx_train` as the active context because that is the model's training
limit, not necessarily the server's configured window.

The live catalog is authoritative: stale configured model IDs do not get added
alongside IDs the server actually advertises. The model ID override is used
when the API cannot provide a catalog; in that case Swarm Agent leaves context
unset rather than guessing from training metadata. Known contexts are written
to OpenCode with an output cap of 32,000 tokens, reduced to the full context if
the context is smaller. A stale saved OpenCode selection falls back to the
configured live catalog default; other ACP backends keep their normal
selection validation.

### Playwright automation in Docker

The Docker image pins `@playwright/cli` at `0.1.22` and installs its matching
Chromium browser plus Linux dependencies during image build. Agents use the
global `playwright-cli` binary; runtime npm/npx downloads cannot work reliably
because the container root filesystem is read-only. Browser binaries live in
the image at `/ms-playwright`. The CLI config at
`/app/playwright-cli.config.json` sends snapshots and other output to
`/home/board/playwright-output`; `XDG_CACHE_HOME` keeps persistent browser
profiles under `/home/board/.cache`. Both directories are on the existing
`agent-state` volume and survive container recreation. The entrypoint creates
them for old volumes too. The agent workspace may stay read-only, so browser
artifacts should remain under the configured home directory. Compose allocates
512 MiB of shared memory for Chromium. Rebuild the image to update the pinned
CLI and browser; do not `docker compose down --volumes` to update it because that
also erases board and agent state.

Availability is distinct from model behavior: this configuration supplies the
tools but cannot guarantee correct calls, useful progress or job completion.
External services and local inference can still fail. Existing per-turn limits
and durable team stopping remain in effect; there is no job-wide no-progress
cutoff. See [OpenCode's tool docs](https://opencode.ai/docs/tools/#websearch)
and [permission docs](https://opencode.ai/docs/permissions/).

`team_delegate` defaults to `task`, accepts explicit `type=research`, and copies
the parent job's workspace. Its instructions permit local evidence as well as
public sources. Existing research assignments can also record workspace
evidence; a runner instruction makes this explicit when resuming old tickets.
Reply/review turns use the recipient ticket's workspace and the shared skill
index, without requiring a particular research skill to be installed.

Worker/chat/response roles receive common collaboration rules before their
specialty and turn restrictions. Reusable questions are saved and ticket-linked
before team notifications; response agents can answer job-linked questions on
the Questions page before `team_reply`. Peer reviews have scheduling priority
and one reserved response slot. Isolated orchestrator review sessions receive
the main chat's role guidance and the reviewed assignment, even while the chat
is busy. See [team collaboration](team-collaboration.md) for scope and lifecycle
contracts; evidence/revision delivery gates remain unchanged.

`source_read_file` reads UTF-8 text within the assigned ticket workspace (or job
workspace for unassigned team members). It rejects escaping absolute/relative
paths and resolved symlinks, binary data, oversized files and invalid ranges.
Files are limited to 2 MiB; excerpts to 200 lines and 24,000 encoded bytes.
The source record stores a file URI with line anchors, numbered text, SHA256 of
the file, reader identity and retrieval time. Use its returned URI in the wiki
artifact. These records reuse `research_sources` and `source_reads`, so delivery
and current-revision review require the same recorded evidence inspection as
web sources. No database migration or weakened review gate is needed.

MCP `team_read` and injected team context use a bounded index, at most 31,900
encoded bytes, with counts, recent message previews, pending/failed requests,
source metadata, progress and review summaries. A specific `message_id` returns
that job's message in bounded pages using `offset`/`next_offset`. Sources page
with `source_after_id`/`next_source_after_id`; `source_get` returns full snapshots.
The dashboard's full `team.room` response retains its existing shape.

Execution and synthesis prompts have an explicit 900-second total timeout.
Continuous streaming cannot extend it. Monitor and response turns retain their
180-second limits. Failed workers close their connections and release slots;
existing bounded retry and pending-reply recovery rules apply.

## Investigation evidence

Job #10 delegated all 12 local code-audit assignments as `research`, with native
tools denied and instructions requiring HTTPS evidence. The persisted tool
history includes 404 source fetches and hundreds of repeated `team_read` and
`source_get` calls. One worker made 710 team reads. Some room responses exceeded
180 KB and were truncated into backend files that the policy also prevented
agents from reading. Ticket timestamps did not demonstrate useful delivery.

## Implementation and verification

`coms/runner.py` owns native-tool configuration, turn prompts, workspace selection
and deadlines, including OpenCode's exact local model IDs and runtime context
limits. `coms/team.py` owns delegation, compact room indexes and file
source recording. `coms/mcp.py` exposes the new source and pagination contracts.
`tests/test_worker_execution.py` covers enabled tools, delegation, isolated file
evidence, workspace boundaries, exact-revision review, large room/message
pagination and failed-turn cleanup. Existing runner/team/monitor regression
suites cover coordination and recovery.

On 10 October 2026, 51 focused checks passed with one platform skip. The full
suite ran 185 checks with four existing thinking-transcript failures and two
platform skips; all four failures also reproduced on unchanged HEAD `f46abd9`.
Git whitespace checks and the Docker build passed. The deployed service is
healthy; job #10's 12 existing workers and 21 failed response deliveries were
requeued after a database backup. Its deployed team index is approximately
26 KB rather than the earlier truncated 180+ KB responses.

The required-tool regression also checks every connection role and confirms
other backends receive no OpenCode overrides. An isolated OpenCode 1.18.35
session with stale project/agent denials exposed the actual required tool
inventory. The configured `LFM2.5-2.6B-Q8_0.gguf` model completed a native web
search and returned a result URL. This small capability check does not establish
reliability for the earlier 12-worker audit, which remains on hold.

Validation on 11 October 2026: 49 focused checks passed with one platform skip.
The full suite ran 243 checks: 237 passed, two skipped and the same four existing
thinking-transcript cases failed. Those failures reproduced before this change.

OpenCode model-metadata validation on 11 October 2026: 23 focused worker tests
ran with one platform skip. A later privacy and regression cleanup kept ACP
thought chunks out of public transcripts and corrected browser checks to match
the live UI and SSE fallback. The latest full suite passes all 257 Python tests
(two platform skips) and all 31 browser tests.

Docker Playwright validation on 11 October 2026: four focused runtime-contract
tests pass; Compose configuration and image build pass. A non-root, read-only
container opened Chromium, saved snapshots and a screenshot to `/home/board`,
and a newly created container confirmed the output and browser binary persisted.
The running board service was recreated from the image with its named volumes
preserved and passed its health check and a second browser launch.
