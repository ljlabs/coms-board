---
name: source-backed-research
description: Retrieve primary sources, record evidence, and separate verified facts from inference in collaborative research.
---

# Source backed research

Use this procedure for research that needs evidence other teammates can inspect.

## Retrieve and record evidence

Use `source_fetch` with real primary source URLs. It records the actual content, HTTP result, and retrieval time for peer inspection. A 404 means the URL does not exist; do not describe it as restricted access or repeatedly guess URL variants. Read retrieved content and cite direct URLs and dates. Use `source_get` to inspect evidence retrieved by teammates.

Distinguish evidence, inference, and claims that could not be verified. When a source fails, record the limitation honestly and try the real starting URLs provided in the job. Peer reviews should challenge unsupported factual claims.

## Deliver collaborative research

Workers publish separate evidence pages, link them to their tickets, and request review of the current revision. The lead verifies those pages before writing a fresh synthesis. Cite sources actually retrieved through `source_fetch`.

Shared skills are verified procedures, not unverified research findings. Never claim a backtest or other analysis was performed unless it was actually executed. Skill files are read through the Swarm Agent `skill_get` tool rather than a backend's native skill loader.
