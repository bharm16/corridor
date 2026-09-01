---
status: accepted
domain: record-inclusion
scope: current product
amended_by:
  - ADR-0076
---

# Automatic Record Inclusion follows the segment kind

ADR-0042 fixed the authority rule: a released deterministic policy writes exact cases, agents assist, humans decide ambiguity. The facts model needs that rule bound to fact types. The 2026-08-30 research resolved it: the dividing property — can a released rule prove the fact from the cited segment alone, with exact dereference and complete accounting, and no interpretation — belongs to the **kind of source segment**, not to the fact type in the abstract.

Decision: **each fact-type contract records which segment kinds qualify it for automatic Record Inclusion; everything else is a Human Record Decision.**

Automatic, through a released policy, when validation passes and the run is the explicitly selected Current Production Run:

- values backed by structured cells under a verified mapping — Stationing, source identifiers, location text, dates, and External Organization wording as recorded, from spreadsheet and matrix cells;
- date-table facts: milestone lists, clearance-date tables, design-build utility tracking reports (the whole schedule source class per ADR-0066 — these arrive in coordination vocabulary and validate deterministically);
- email header and thread facts; attachment metadata.

The rationale: the segment supplies the value and the released mapping supplies the meaning, so a human gate there is the attributable no-op ceremony ADR-0035/0039 prohibit.

Human Record Decision, always:

- obligations and Commitments read from agreement clauses;
- statement attribution; timing changes, closures, and decisions read from minutes or email prose;
- Applies To scope, unless a structured cell states it directly;
- Discrepancy Resolution outside strict comparable-value, dated-source cases;
- entity resolution beyond an exact registered alias.

The rationale: the ambiguity is in the source; better extraction cannot remove it.

Closure splits: a matrix's marked-resolution cell auto-includes as a source fact ("this source marks it resolved"), but record-level closure is a decision — automatic only where a released rule says a marked cell in the governing source closes that Constraint.

## The one scoped write command

The agent's write path is one project-scoped application command that appends segments, facts, and immutable proposals atomically: it validates scope, digests, locator bounds, exact dereference, typed values, complete row/segment accounting, and idempotency, and rolls everything back on any failure. Fact appends are naturally content-addressable — a unique index on a deterministic hash of (type, subject, value, segment refs, run) gives retention-free idempotency; commands that may legitimately repeat (decisions) use an idempotency-key table with published retention, per the Stripe pattern. The command cannot write the Project Record; inclusion runs only through this ADR's rules.

## Promotion is earned, not asserted

A new fact type defaults to human-gated. Promotion to automatic inclusion requires a released policy with a replayable eligibility receipt, activated only after the ADR-0050 regression replay — the same trajectory matrix column mappings followed. Model confidence never substitutes: confidence self-reports are least calibrated exactly on the residue where inclusion is hard (ADR-0042's "confidence is not proof," now with the storage-side mechanism).

## Consequences

The inclusion boundary preserves current authority behavior while keying it to contracts the append command can enforce mechanically. The extraction-economics gate (issue #424) measures what fraction of facts flow through the automatic side — the product's premise per ADR-0066.
