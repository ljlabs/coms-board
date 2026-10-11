# Regression suite cleanup

## Symptom
Repeated full runs showed four Python transcript failures and nine browser failures. ACP thought chunks appeared as assistant chat messages; browser checks also disagreed with the current hidden Stop control, native dialogs, SSE fallback, and tool panel.

## Failed approaches
Earlier runs recorded unchanged-HEAD reproductions as baseline failures without checking whether expectations still matched the feature notes and current markup. That left real privacy/UI behavior and stale tests unresolved.

## Fix
Keep ACP thoughts in opt-in diagnostic logs and suppress current and legacy thought entries from public chat responses. Hide Stop with an explicit CSS rule, keep disabled built-in edit controls discoverable, and align browser fixtures/assertions with SSE fallback, native dialog state, and current tool markup. Full Python and browser suites now pass.

## Signal
Before treating repeated failures as accepted baseline, compare assertions to current feature docs and inspect whether tests are intercepting the actual live transport (SSE versus polling).
