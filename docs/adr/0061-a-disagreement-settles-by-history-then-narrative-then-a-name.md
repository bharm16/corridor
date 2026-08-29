---
status: accepted
---

# A disagreement settles by history, then narrative, then a name

Two verified sources can state incompatible values for the same fact on the same Constraint — the agreement says 12-inch, the matrix says 16. Unlike the matching problems (ADR-0051, ADR-0054), row evidence cannot adjudicate this: the conflicting sources *are* the row's evidence. But review on 2026-08-28 (night) found the human card was doing work that history and a bounded reading can do first. Disputes now resolve in three layers.

## Layer 1 — history (mechanical)

Same-document-family disagreements never exist: supersession already makes the newer revision current.

Cross-document disagreements run the **staleness predicate**: *the older document's value matches what the row itself used to say, and the document predates the row's recorded change.* When that holds, there is no contested reality — the old document is consistent with history, written before the data improved (the 12→16 change is a recorded comparison event, often with an investigation-quality upgrade behind it).

- **Physical facts** (size, material, location): auto-concluded "earlier value, superseded by field update," both sources kept, timeline recorded, no card.
- **A contractual document gone stale** converts to a **task**, not a card: "field data changed under the agreement — flag for amendment," on the owner's list with both quotes.

Contested means chronology *fails*: the disagreeing document is *newer* than the row's change, the value oscillated, or no usable ordering exists. The card's explanation is always the specific chronological fact with dates — never "sources differ."

## Layer 2 — narrative (the caged agent)

When mentions pile up across emails, minutes, and documents, the evidence needs reading, not a predicate. The existing investigation harness (bound case, read-only tools, budgets, quote-verified output, no authority-shaped fields) assembles a **row-scoped timeline**: every mention of the value across all sources and threads, in order, each quote verified, each classified — measurement, casual mention, restatement of an older document, correction — with a drafted reading ("one field correction in March, confirmed by Rev C; apparent current position 16"). Display only. The agent never settles.

## Layer 3 — a name (the human, one click)

The card is a **summary, not a reading assignment**: the claims as extracted values with quotes, dates, and investigation quality; the why-contested line; what the pick affects (materiality); the timeline when one exists. Documents sit behind taps — pixels are for verification, not for diffing by eyeball. The one exception: when the drawing is the claim, the images are primary. Choices: either value, a different conclusion, or "ask them," which records the follow-up task. The pick is attributable, both sources survive, and a later conflicting source can reopen — unchanged.

Materiality gating stands: a dispute interrupts only when it touches a current date, readiness, a report, or a next action. The dial recorded elsewhere applies here too: auto-settlement for clean patterns may one day be earned per class through the ADR-0050 replay against recorded human settlements. Until then, the last click on which truth stands belongs to a person.

## Consequences

Amends the pages-beside-the-question presentation for disputes (ADR-0035): the summary is primary, pages are one tap away. Ticket #346 carries the three-layer implementation. The timeline packet is a new bounded case type on the existing investigator harness.
