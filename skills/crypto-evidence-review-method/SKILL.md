---
name: crypto-evidence-review-method
description: "Crypto Strategy Evidence & Review Method"
---

# Crypto Strategy Evidence & Review Method

A verified procedure for retrieving, verifying, and reviewing evidence for cryptocurrency strategy research. Grounded in methods this team successfully used (Job #1, ticket #1).

## 1. Source retrieval (source_fetch)
- Use only real primary-source URLs from the job brief. Do not guess nonexistent URLs.
- A 404 or "too little content to verify" means the URL does not yield verifiable content, not that access was restricted. Record the error honestly.
- Do not repeatedly re-fetch a URL that already failed for every reader.
- Cite each retrieved source with its source ID, direct URL, and retrieval date.

## 2. Evidence vs. inference vs. limitations
- Evidence: claims directly supported by a retrieved source.
- Inference: reasoning connecting sources — label it as inference, not evidence.
- Limitations: failed sources, unsourced claims, single-paper metrics, dated sources.

## 3. Peer review (team_request_review → team_review)
- Reviewer inspects the current page with wiki_get and each cited source with source_get.
- Reviewer challenges unsupported claims, missing citations, and misleading completion claims.
- Reviewer requests concrete corrections (changes_requested) or accepts.
- Revise rejected work before requesting a fresh review.
- Only call ticket_result after the current revision has an accepted peer review.

## 4. Completion honesty
- Never claim a backtest was run or profitability established unless actually executed.
- Mark unsourced claims as unsourced, not as "general market knowledge."
