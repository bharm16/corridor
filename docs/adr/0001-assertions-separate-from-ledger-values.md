---
status: accepted
domain: project-record
scope: current product
amended_by:
  - ADR-0076
---

# Source field values are stored separately from the project's conclusions

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Source documents routinely disagree about a Constraint's Promised For date or other fields — the Utility Conflict Matrix says one date, the minutes say another, the Utility Owner's email says a third. The original schema stored a single value per field on `dependencies`, and `evidence_links` recorded no asserted value, so there was nowhere to represent the disagreement and the `CONTRADICTION` constraint alert rule was uncomputable as written.

A document's value for one Constraint field is a **source field value**, retained internally as an **Assertion** — `(dependency_id, field_name, asserted_value, evidence_link_id, doc_date)`. The Constraint's own field value is the project's recorded conclusion drawn from those sources. A normalized field value and its source's exact wording remain distinct; neither alone proves a physical fact.

## Considered options

- **Columns on `evidence_links`.** Rejected: one quote commonly supports several fields at once, forcing either duplicate link rows per field or a list encoded in a column.
- **Derive contradictions from merged candidates.** Rejected: `candidates` is designed as a transient review queue, and rejected candidates muddy the query. Coupling ledger semantics to queue state is a trap.
- **Drop `CONTRADICTION` from v0.** Rejected: "your two documents disagree and nobody noticed" is the product's sharpest demonstration, not an optional extra.

## Consequences

A Constraint Record's field value is a conclusion, not a record of what any document said. Reading a field alone loses the disagreement beneath it, so the Constraint detail view must surface its source field values and citations — otherwise the tool reproduces the silent-overwrite behavior it exists to replace. `Assertion` remains the internal provenance identity; the customer label does not rename it.
