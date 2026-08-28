---
status: accepted
---

# A source discrepancy stays with the row and is resolved without erasing its sources

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

ADR-0029 made one matrix enough and left one gate standing: revisions stating a conflict differently still withheld it, so the rows where judgment actually pays were the rows the product would not show. That is backwards for a tool whose job is surfacing — a disagreement between the February and May matrices is the most interesting thing on the screen, not a reason to hide the conflict. Decision: **the newest revision is admitted and the rest merge whether or not they agree.** Agreement lands as corroboration; disagreement lands as a **Source Discrepancy** — every revision's claim recorded as an Assertion citing its own page, with the existing CONTRADICTION Constraint Alert naming the fields. A Source Discrepancy is therefore a query, never a stored flag, exactly as every other Constraint Alert is: nothing was withheld to create it, so nothing must be resolved to undo it, and a disputed row is an ordinary workable row that can take an owner and a next action while the disagreement stands. The newest revision supplies the record's provisional field values, said out loud rather than silently — the CONTRADICTION is the saying.

Two disagreements still withhold a row, and neither is about what a conflict says. Several rows in one revision sharing an identity is ambiguity, not disagreement. Revisions naming different **External Organizations** asks whether these are one conflict at all, and merging them would answer that question by accident — SH 99 already produced one such re-attribution.

A **Discrepancy Resolution** is a Human Record Decision on one field: a reviewer records the conclusion the project will use. Assertions preserve every source field value, so the resolution cannot erase a losing claim. It records the conclusion beside the claims and **how far its judgment reaches**, by naming the newest Assertion that existed when it was made. A later revision's claim carries a higher id, postdates the judgment, and reopens the Source Discrepancy on its own. Nobody has to notice the arrival, and no reviewer is taken to have ruled on sources they never saw. Discrepancy Resolutions are append-only under the same trigger the policy receipts use, and the conclusion projects onto the Constraint's column where one exists — the shape the Promised For projection already takes. This records a field conclusion; it does not settle a contract claim or authorize a change to construction.

## Considered options

**Keep withholding disagreements and improve the queue card.** Rejected: it is the shipped behavior, and it makes the product's best question invisible until someone clears a queue.

**Elect the newest revision silently.** Rejected for the reason ADR-0027 gave — it decides every disagreement in favor of recency. What makes election acceptable here is that the disagreement is visible on the row and recording a conclusion is one action; take the visibility away and this becomes the rejected option again.

**Let a Discrepancy Resolution retract the losing Assertion.** Rejected: Assertions are what the documents said, and a record that edits its sources cannot cite them. It would also make the resolution irreversible in the one direction that matters — a mistaken conclusion must be correctable through a later recorded conclusion, which append-only history permits.

**Resolve the whole record rather than the field.** Rejected: revisions disagree field by field, and forcing one verdict over a whole row makes a reviewer rule on the five fields they did not read.

## Consequences

`dispute_settlements` is the retained append-only table; `contradicted_fields` — the one definition both the engine and the detail view read — excludes fields with an applicable resolution, so the list pill and detail page still cannot disagree. The Constraint page offers **Record conclusion** for each field with a Source Discrepancy, beside the source values. The `revisions_disagree` abstention is no longer emitted and stays named for the receipts that carry it; `revisions_disagree_on_party` joins the vocabulary at v3. Reviewer work shifts from clearing a queue to resolving Source Discrepancies on live rows.