# Feature: Worker tools and evidence

## Behavior and contracts

Every agent retains its backend's native tool support. The runner does not
generate OpenCode permission or tool-deny configuration for research,
collaboration, dispatch, monitoring, or ordinary execution. Scoped MCP writes
still enforce board identity and turn responsibilities. The workspace's actual
filesystem permissions continue to govern native commands.

`team_delegate` defaults to `task`, accepts explicit `type=research`, and copies
the parent job's workspace. Its instructions permit local evidence as well as
public sources. Existing research assignments can also record workspace
evidence; a runner instruction makes this explicit when resuming old tickets.
Reply/review turns use the recipient ticket's workspace and the shared skill
index, without requiring a particular research skill to be installed.

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
and deadlines. `coms/team.py` owns delegation, compact room indexes and file
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
