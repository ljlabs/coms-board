# Coding-agent instruction setup

The workspace policies live in [`AGENTS.md`](../AGENTS.md), and the shared
development workflow lives in [`devDocs/README.md`](README.md). Keep tool entry
points small and refer to these files so the workflow stays consistent.

## Files loaded by the tools

| Tool | Entry point in this repository | How it receives the workflow |
| --- | --- | --- |
| Codex and tools supporting AGENTS.md | [`AGENTS.md`](../AGENTS.md) | Requires reading this guide, the system overview, and relevant feature notes before editing. |
| Claude Code | [`CLAUDE.md`](../CLAUDE.md) | Imports `AGENTS.md` and `devDocs/README.md` with `@path` directives. |
| Kilo | [`AGENTS.md`](../AGENTS.md) and [`kilo.jsonc`](../kilo.jsonc) | The config explicitly loads `.kilo/agents.md` and `devDocs/README.md`. |

These entry points follow the documented mechanisms for
[Codex instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
[Claude imports](https://code.claude.com/docs/en/memory#share-one-file-with-other-coding-tools),
and [Kilo instruction configuration](https://kilo.ai/docs/customize/custom-instructions).

The requested compatibility files are also maintained:

- [`.agents/agent.md`](../.agents/agent.md)
- [`.Codex/agent.md`](../.Codex/agent.md)
- [`.kilo/agents.md`](../.kilo/agents.md)
- [`.claude/agents.md`](../.claude/agents.md)

These paths provide pointers for integrations that explicitly load them. Use
the native entry points above for automatic discovery. All paths mentioned by
the workflow are relative to the repository root.

## Maintenance and verification

When the workflow changes, edit `devDocs/README.md`; update the system overview
and relevant feature notes when behavior or responsibilities change. Update
`AGENTS.md` for workspace policies. Keep the adapters and configuration paths
consistent with those shared files.

For a new coding tool, confirm its instruction-file convention in its official
documentation, add the appropriate entry point, and register it in the table.
Check Markdown links, Claude imports, and Kilo's configured paths after edits.
Start a fresh tool session and inspect its loaded guidance when verifying that
the tool actually received the instructions.

Only `.Codex/agent.md` is eligible for version control under `.Codex/`; its
plans and other local files remain ignored. Guidance here applies to agents
changing this repository. The root `SKILL.md` describes using the board product.
