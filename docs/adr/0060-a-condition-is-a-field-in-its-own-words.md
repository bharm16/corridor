---
status: accepted
---

# A condition is a field in its own words

An approval letter can carry a catch: "approved, but only after final inspection passes." ADR-0056 had a person choose how to record such a letter. Reviewed 2026-08-28 (night) and corrected: the person's click guarded the wrong direction. This ADR replaces ADR-0056's hedged-letter branch and states the general rule for conditions of any kind.

## The direction rule

**Not-ready is the default state. It costs zero human effort to stay not-ready. Clicks are only ever spent moving toward Ready.**

Recording a letter as conditional keeps the Constraint not-ready — the fail-closed direction — so it needs no human act. The only clicks that remain are upgrades toward Ready: confirming a clean letter (per ADR-0056, retirable by replay), and an optional override to count a hedge as immaterial. Available, never required.

## What happens on a conditional letter — automatically

1. The approval field records as conditional, with the letter's condition sentence quoted as its basis.
2. The condition becomes a tracked entry on that Constraint's checklist.
3. The Constraint is not Ready, because a required field is empty — the same reason anything is ever not ready.
4. The list shows the ask in the company's own words: "waiting on: 'final inspection.'"

## Conditions are dynamic — the words are the tracking key

The system never needs to understand a condition to track it. It quotes it, blocks Ready with it, surfaces it, and accepts evidence against it. Three tiers:

1. **The condition names something already tracked.** "Once we receive the executed agreement" links to the existing agreement field; "after Xcel completes their work" links across rows to that Constraint's completion. Matched by identifying language, an exact rule when exactly one target survives. A linked condition clears itself when its target clears.
2. **The condition names something untracked.** "Pending our board's Q3 review" becomes a generic open condition holding the exact quoted words. It clears when later evidence matches its language — proposed with the quote, cited confirm where judgment is needed — or when a person records what they learned (a verbal statement clears it attributably).
3. **Nothing ever arrives.** The condition sits open, blocking Ready, on someone's list, quoted verbatim. That is the correct outcome, not a failure: the chase request is written in the company's own words with their letter one tap away.

A misread condition errs safe: someone chases a condition that was not real — visible and annoying, never a false Ready.

## Consequences

Replaces ADR-0056's hedged-letter branch (the clean-letter confirm and the rest of ADR-0056 stand). Extends ADR-0052's checklist with condition entries and cross-row links. Implemented by its own ticket on top of #347 (the fields model) and #370 (the shared identifying-language matcher).
