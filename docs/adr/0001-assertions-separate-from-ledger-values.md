---
status: accepted
---

# Assertions are stored separately from ledger values

Source documents routinely disagree about a Dependency's committed date or status — the conflict matrix says one date, the minutes say another, the utility's email says a third. The original schema stored a single value per field on `dependencies`, and `evidence_links` recorded no asserted value, so there was nowhere to represent the disagreement and the `CONTRADICTION` exception rule was uncomputable as written.

Every source claim is now stored as an **Assertion** — `(dependency_id, field_name, asserted_value, evidence_link_id, doc_date)` — and the Dependency's own field values are the *adjudicated conclusion* drawn from them.

## Considered options

- **Columns on `evidence_links`.** Rejected: one quote commonly supports several fields at once, forcing either duplicate link rows per field or a list encoded in a column.
- **Derive contradictions from merged candidates.** Rejected: `candidates` is designed as a transient review queue, and rejected candidates muddy the query. Coupling ledger semantics to queue state is a trap.
- **Drop `CONTRADICTION` from v0.** Rejected: "your two documents disagree and nobody noticed" is the product's sharpest demonstration, not an optional extra.

## Consequences

A ledger field value is a conclusion, not a record of what any document said. Reading a field alone loses the disagreement beneath it, so the Dependency detail view must surface its assertions — otherwise the tool reproduces the silent-overwrite behavior it exists to replace.
