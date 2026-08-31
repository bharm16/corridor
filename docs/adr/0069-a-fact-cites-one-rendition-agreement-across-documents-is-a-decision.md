---
status: accepted
---

# A fact cites one rendition; agreement across documents is a decision

A fact needs support rules in two directions: several segments combining into one fact, and several Documents appearing to state the same thing. The 2026-08-30 storage research settled both.

Decision, within one Document: **facts reference segments through a `fact_sources` join with a role on each link** — value source, context, attribution source — and at least one value-source link is mandatory for source-backed fact types. When several segments combine (a station cell plus the page label naming the External Organization; `135+58.68, 236.85' LT` splitting into station, offset, and side), the combination carries a named, replayable transformation, and every materialized value must replay from its segments through that transformation. A value that cannot be replayed from its cited segments does not validate.

Decision, across Documents: **a fact cites exactly one Document Rendition.** When a second Document agrees with the first, that is two facts — one per source — plus a typed decision or Derivation linking them. Agreement is a conclusion, never a merge.

This is already Corridor law at the hardest case: ADR-0064 resolves an unreadable cell by corroboration from another readable Document, and the corroboration is itself a decision that admits through the readable source's citation. This ADR generalizes that shape to all facts.

## Considered options

**One fact supported by several Documents.** Rejected. "What did this Document say" is the primitive the record answers for years; blending sources into one row destroys it. It also breaks the append command's simplest validation: every segment reference must belong to the one rendition the command was scoped to (ADR-0070's command validates exactly that).

**Free-form multi-segment combination.** Rejected: without a named transformation, "the value came from these cells" is unverifiable, which reintroduces the model-authored-value surface the extraction architecture exists to eliminate.

## Consequences

Cross-document rules stop being an open modeling question — they are an existing decision type. Dispute machinery (ADR-0031) keeps working unchanged: conflicting facts from different Documents coexist as separate cited rows, and settlement is a typed decision naming its reach.
