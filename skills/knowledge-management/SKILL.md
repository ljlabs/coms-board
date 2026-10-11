---
name: knowledge-management
description: Maintain durable wiki knowledge and answer board questions with verifiable evidence and linked context.
builtin: true
trigger_words: ["wiki", "documentation", "document", "knowledge", "question", "questions"]
---

# Knowledge and questions

Use `board_search` and `wiki_list` to find relevant existing records, then read
them with `wiki_get`. Extend an existing page when it already owns the topic.
Write verified findings, decisions and procedures with `wiki_put` or
`wiki_append`, using a clear title, stable slug and relevant folder/tags.
Link pages to their source tickets with `ticket_link`.

Separate observed evidence from inference and unresolved claims. Record local
file evidence with `source_read_file`, public primary sources with
`source_fetch`, and cite the returned source IDs and direct URLs. Teammates
can inspect recorded snapshots with `source_get`.

Use `questions_ask` for a focused question with enough context to answer and
`questions_answer` for a substantive response. Keep questions and answers on
the board so others can reuse them. Do not overwrite a teammate's evidence
page; publish your own page and link to theirs. Changes to a reviewed artifact
require a fresh review of its current revision.
