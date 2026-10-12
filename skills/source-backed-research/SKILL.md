---
name: source-backed-research
description: Retrieve primary sources, record evidence, and separate verified facts from inference in collaborative research.
builtin: true
trigger_words: ["research", "evidence", "sources", "citation", "citations"]
---

# Source backed research

Use this procedure for research that needs evidence other teammates can inspect.

## Retrieve and record evidence

Use `source_fetch` with real primary source URLs. It records the actual content, HTTP result, and retrieval time for peer inspection. A 404 means the URL does not exist; do not describe it as restricted access or repeatedly guess URL variants. Read retrieved content and cite direct URLs and dates. Use `source_get` to inspect evidence retrieved by teammates.

For code, tests and local documents, use native tools to inspect the workspace
and run the assigned commands. Record relevant file excerpts with
`source_read_file`, then cite the returned URL/source ID and line numbers. File
snapshots include a SHA256 hash and retrieval time and are inspected with
`source_get`. Local audits can complete using file evidence; fetch public URLs
only when they are relevant to the findings. All agent roles keep native tools.

Distinguish evidence, inference, and claims that could not be verified. When a source fails, record the limitation honestly and try the real starting URLs provided in the job. Peer reviews should challenge unsupported factual claims.

## Deliver collaborative research

Post reusable research questions on the Questions page before asking teammates
for insights. Search first, create or reuse a standalone question, and link it
to your ticket. Notify the team with its ID/link, then send a targeted request
when a particular specialist's answer is needed. Save source-backed answers
and honest limitations with `questions_answer` before `team_reply`, so future
research can reuse the findings.

Workers publish separate evidence pages, link them to their tickets, and request review of the current revision. The lead verifies those pages before writing a fresh synthesis. Cite sources recorded through `source_read_file` or `source_fetch`.

Workers normally request the registered orchestrator as reviewer; the lead's
synthesis needs an independent teammate. Reviewers promptly inspect `wiki_get`
and cited `source_get` evidence, then record `team_review` with concrete reasons.
Keep the reviewed page unchanged until the decision arrives and request a
fresh review after any revision. Explicit access limitations are acceptable;
invented verification and unsupported completion claims are not.

Shared skills are verified procedures, not unverified research findings. Never claim a backtest or other analysis was performed unless it was actually executed. Skill files are read through the Swarm Agent `skill_get` tool rather than a backend's native skill loader.
