# Verify exact patch context

## Symptom
Repeated documentation patches failed with `Failed to find expected lines`.
One invalid trailing file section also prevented the earlier edits from applying.

## Failed approaches
Recalled wording and incomplete no-change update sections did not match complete
source lines. Retrying a larger patch retained the invalid section.

## Fix
Read the exact surrounding lines with `rg -n -A -B`, then patch only files with
actual additions/deletions and verified complete-line context.

## Signal
Before retrying a context error, compare the whole failing line and remove any
placeholder file sections; the failed multi-file patch applies no earlier edits.
