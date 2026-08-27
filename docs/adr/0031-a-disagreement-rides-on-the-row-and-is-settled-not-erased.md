---
status: accepted
---

# A disagreement rides on the row, and is settled rather than erased

> **Terminology amended 2026-08-27 by [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Current prose uses the adopted construction terms. Historical quotations and implementation identifiers retain their original spelling; the authority boundaries are unchanged.

ADR-0029 made one matrix enough and left one gate standing: revisions stating a conflict differently still withheld it, so the rows where judgment actually pays were the rows the product would not show. That is backwards for a tool whose job is surfacing — a disagreement between the February and May matrices is the most interesting thing on the screen, not a reason to hide the conflict. Decision: **the newest revision is admitted and the rest merge whether or not they agree.** Agreement lands as corroboration; disagreement lands as a **Dispute** — every revision's claim recorded as an Assertion citing its own page, with the existing CONTRADICTION Exception naming the fields. A Dispute is therefore a query, never a stored flag, exactly as every other Exception is: nothing was withheld to create it, so nothing must be resolved to undo it, and a disputed row is an ordinary workable row that can take an owner and a next action while the disagreement stands. The newest revision supplies the record's provisional field values, said out loud rather than silently — the CONTRADICTION is the saying.

Two disagreements still withhold a row, and neither is about what a conflict says. Several rows in one revision sharing an identity is ambiguity, not disagreement. Revisions naming different **External Parties** asks whether these are one conflict at all, and merging them would answer that question by accident — SH 99 already produced one such re-attribution.

Settling is Adjudication in its narrowest form: a reviewer says what the record concludes for one field. Assertions preserve every claim, so a settlement cannot erase the losing one and does not try — it records the conclusion beside them and records **how far its judgment reaches**, by naming the newest Assertion that existed when it was made. A later revision's claim carries a higher id, postdates the judgment, and reopens the Dispute on its own. Nobody has to notice the arrival, and no reviewer is ever taken to have ruled on evidence they never saw. Settlements are append-only under the same trigger the policy receipts use, and the conclusion projects onto the Constraint's column where one exists — the shape the Promised For projection already takes.

## Considered options

**Keep withholding disagreements and improve the queue card.** Rejected: it is the shipped behavior, and it makes the product's best question invisible until someone clears a queue.

**Elect the newest revision silently.** Rejected for the reason ADR-0027 gave — it adjudicates every disagreement in favor of recency. What makes election acceptable here is that the disagreement is visible on the row and settling it is one gesture; take the visibility away and this becomes the rejected option again.

**Let a settlement retract the losing Assertion.** Rejected: Assertions are what the documents said, and a record that edits its sources cannot cite them. It would also make a settlement irreversible in the one direction that matters — a mistaken settlement should be correctable by settling again, which append-only history gives for free.

**Settle the record rather than the field.** Rejected: revisions disagree field by field, and forcing one verdict over a whole row makes a reviewer rule on the five fields they did not read.

## Consequences

`dispute_settlements` is a new append-only table; `contradicted_fields` — the one definition both the engine and the detail view read — excludes settled fields, so the list pill and the detail page still cannot disagree. The Constraint page grows a settle gesture per disputed field, offered beside the claims rather than on a screen of its own. The `revisions_disagree` abstention is no longer emitted and stays named for the receipts that carry it; `revisions_disagree_on_party` joins the vocabulary at v3. Reviewer work shifts from clearing a queue to settling Disputes on live rows.
