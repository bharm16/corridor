---
status: accepted
domain: migration
scope: current product
amends:
  - ADR-0074
amended_by:
  - ADR-0083
migration: recorded_verbal_statement segments still reference dependency_events through statement_id; every human flow dual-writes legacy and spine; no reader consumes the spine projection alone; stages 1 through 6 below are all open.
---

# The spine is the target model, and legacy tables retire by staged exit criteria

**Amends ADR-0074, and supersedes it once stage 6 below is complete.**

ADR-0074 put Human Record Decisions and Recorded Verbal Statements on the spine by dual-writing every human flow to both the legacy tables and the spine. It also gave the `recorded_verbal_statement` segment kind a nullable `statement_id` foreign key to `dependency_events`. That is the most serious defect in ADR-0074: it makes the new evidence spine depend on a legacy aggregate. A source segment should identify its own source and provenance; it must not require a legacy Project Record event to exist first. And ADR-0074 names no exit. The codebase already carries parallel Candidate and Extracted Proposal models, the legacy record tables, and the Facts spine; if that transitional split persists, every new feature costs roughly twice as much and authority eventually diverges.

## Decision

### The target model

```
Source Artifact
    → Source Segment
    → Typed Source Fact
    → Proposed Delta
    → Fact/Record Decision
    → Project Record Revision
    → Current and as-of projections
```

Every product reader consumes the last line. Every writer enters through ADR-0076's four operations.

### Stages and exit criteria

**1. Define the target model.**
- No target entity requires a foreign key to a legacy Project Record table.
- Recorded Verbal Statements use a spine-native source-origin identity: the segment carries recorder, statement time, exact words, and `content_sha256`; `statement_id` becomes migration lineage only.
- Coordination Decisions, support designations, discrepancy resolutions, and dispositions all project through typed decisions (ADR-0074 decisions 3, 6, and 7 stand).
- Exit: the schema for every target entity is defined and no target column references `dependencies`, `dependency_events`, `work_decisions`, `operative_support`, or a dispute table except through an explicitly named lineage column.

**2. Backfill.**
- Convert all active legacy conclusions and relationships to spine-native facts and decisions, under one attributable migration run with ADR-0079 run provenance.
- Preserve original legacy identities as migration lineage, not target authority.
- Exit: every active legacy row has a spine counterpart, and the backfill is replayable to the same digests.

**3. Prove equivalence.** For each of: Constraint Log; change inbox and Work List; current and as-of Project Record; Coordination Report; workbook export; report release; source and decision history. The existing `prove_reader_equivalence` gate is extended to all seven surfaces.
- Exit: every surface renders identically from legacy and spine for the development corpus and each design partner's adopted baseline.

**4. Move readers.**
- All product readers consume the spine projection.
- Legacy tables become compatibility views or read-only references.
- Exit: no reader imports a legacy table module; the architecture test enforces it.

**5. Move writers.**
- Disable direct legacy writes.
- Run one limited rollback period with legacy shadow comparison, bounded in the runbook, not indefinite dual-write.
- Exit: the shadow comparison reports zero divergence for the rollback window, and the window has closed.

**6. Retire.**
- Remove dual-write code.
- Archive or drop obsolete tables through a supported migration after the rollback window and an ADR-0080 retention review.
- Exit: ADR-0074's status changes to `superseded by ADR-0081`.

### Freeze

Effective immediately: **no new feature may be implemented solely against legacy `dependencies`, `dependency_events`, `work_decisions`, `operative_support`, or dispute tables.** A feature that needs one of those tables writes through the spine and, during stages 1 through 5, dual-writes the legacy row as ADR-0074 already requires. Legacy-only capability work is refused in review.

## Amendments to ADR-0074

- Decision 1's `statement_id` foreign key is demoted to lineage; the segment's identity is its own recorder attribution, time, and exact-words digest.
- Decision 6's "new work dual-writes" is bounded by stages 5 and 6 above; dual-write is a stage, not a steady state.
- The sequencing section gains these exit criteria. Nothing else in ADR-0074 changes.

## Considered options

**Indefinite dual-write with the spine as a shadow.** Rejected. It is the current state, and it is what makes each feature twice as expensive.

**Big-bang cut-over.** Rejected. Seven reader surfaces and two design partners' records need equivalence proof before authority moves; the staged exits are how that proof is recorded.

## Consequences

- ADR-0074 records `amended_by: ADR-0081` now and `superseded by ADR-0081` at stage 6.
- The decision index lists this migration as unresolved until stage 6.
- Stage 1's spine-native verbal identity is the first implementation task, because ADR-0077's recorded-verbal locator and ADR-0076's Capture Source Fact both depend on it.
