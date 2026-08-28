---
status: accepted
---

# Policy re-proof replays the policy's own recorded history

One deterministic rule performs Record Inclusion of supported External Party Statements with Applies To not yet known, with no person in the loop (ADR-0042). Before that rule may run, it must pass a test on real cases. The earlier design pinned the pass to the whole repository revision, so every deploy voided it. Re-proof then needed pending cases — which ordinary processing consumes — and the proposed fix was a program of sealed input bundles. The decision ruling is on [#324](https://github.com/bharm16/corridor/issues/324).

## What a rule is

One named, versioned piece of code with a fingerprint: the checks it runs before recording anything (the Cited Passage is on the cited page, the External Organization resolves to a registered one, the date parses at its stated precision, nothing conflicting is on record). "The rule changed" means the fingerprint changed. Nothing else means that.

## Decision

- The pass is void only when the rule's own fingerprint or its schema changes. A deploy that changes neither does not void it.
- Re-proof is a regression replay. Copy the database. In the copy, erase only the rule's own answers — every one is signed with its system actor and policy version. Run the new rule version on the same cases, from the saved extraction outputs. Never re-read source files. Compare the new answers with what actually happened.
- The comparison is the test. The diff is a complete inventory of the change's behavior on real data. Every difference is intended or a bug. The cases a person decided are the answer key: a new version that would record against a person's contrary decision fails, mechanically. Unexplained differences block.
- Zero cases never pass. Synthetic cases never count. A brand-new rule with no history waits for a real case; a person does the small task by hand meanwhile.
- A deliberate human suspension beats every passing test. Only an attributable human act lifts it.

## Rejected

Sealed input bundles. The append-only database already keeps every case the rule ever saw, with exact inputs and signed outcomes. The warehouse would add machinery to save minutes of manual work.

## One sentence for later

If the product ever runs live, with external users changing data daily, the comparison must separate "the surrounding facts changed after the original decision" from "the rule's behavior changed." No machinery for this today. The current database moves only when the maintainer moves it.

## Consequences

Resolves #322's decision gate 2 as build nothing. Ticket #358 shrinks to this replay. The false-write gates of the existing proof stay.
