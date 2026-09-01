# Corridor roadmap

**Effective 2026-09-01.** Replaces the Phase 1 roadmap, now historical at
[docs/history/phase-1-roadmap-2026-08.md](docs/history/phase-1-roadmap-2026-08.md).
The product is [ADR-0075](docs/adr/0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md)
as corrected by [ADR-0083](docs/adr/0083-corrections-to-the-consolidation-set-after-the-realignment-review.md);
the mechanism is [ADR-0076](docs/adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md).
The program issue is #459. The gate is [docs/pilot-success-criteria.md](docs/pilot-success-criteria.md).
The reasoning is the [2026-09-01 realignment review](docs/research/adr-code-open-issue-realignment-review-2026-09-01.md).

**Goal:** one paid vertical slice, proven with two design partners.

> Import one customer UCM → adopt one exact baseline → observe one new source → produce one or more Proposed Deltas → resolve them → return the customer's updated UCM, change summary, chase list, and weekly report → measure net customer and Corridor operations time.

## Rules

1. Every new product capability writes the spine first. A compatibility write may keep a legacy reader working during migration; no capability may exist only in legacy tables (ADR-0081 freeze).
2. Nothing on the frozen list below is built until the pilot gate returns a decision.
3. Each phase's exit is the listed issue set closed, not a date.
4. Issue numbers are the authority for scope; this file orders them.

## Phase 0 — correct the constitution

Done in the ADR-0082/0083 change unless marked open.

1. ADR-0082 supersedes ADR-0077 (provenance by value class; support as a relation).
2. ADR index authority semantics and lifecycle validation fixed; `make check` is a required status.
3. This roadmap and the exact pilot contract published.
4. Glossary entries for Source Fact, Adopt Baseline, Proposed Delta, Resolve Delta.
5. ADR-0078/0080/0081 corrections recorded in ADR-0083.

**Exit:** the Phase 0 issues (see #459) are closed with this change merged.

## Phase 1 — secure the design partner and deploy the safe shell

1. #428: validate the consultant-first buyer, budget, sponsor, procurement, and pricing hypotheses with real discovery.
2. #461: resolve PyMuPDF licensing before any external deployment that uses it.
3. #487 storage interface and object storage; #490 untrusted-intake hardening; #491 structured logs and metrics; #489 design-partner shadow environment.
4. #496 pull connector and normalized SourceEnvelope; the project-bound push-intake issue, built only for channels the partner needs.
5. #503 pilot and enterprise identity, authorization, and deprovisioning.

**Exit:** a staging environment with one customer database, backups restored once, intake hardened, and one partner's connector registered.

## Phase 2 — build the paid vertical slice on the spine

1. Adopt Baseline: preview and atomically import one customer UCM or system export.
2. Proposed Delta and Resolve Delta: the canonical backend lifecycle.
3. #450 schedule source, #455 email source, #456 minutes source, each only for the partner's source classes.
4. #494 change inbox rebuilt around Proposed Delta.
5. #495 customer-format UCM export from the adopted native workbook.
6. #425 pilot chase list from unresolved deltas and current Follow-up Plans.

**Exit:** the slice runs end to end on one partner project with the partner's own workbook and one connected source.

## Phase 3 — run the shadow pilot

1. #424 net economics measurement, #498 pilot gate report, #499 shadow comparison against customer matrix revisions.
2. Freeze each baseline and incoming-source event before coordinator action.
3. Measure coordinator time, Corridor operations time, latency, coverage, material errors, false or low-value work, and cost against the pilot criteria.
4. Produce the predeclared continue, narrow, or stop decision.

**Exit:** #498 closed with one finding per criterion.

## Phase 4 — finish the canonical cutover (ADR-0081)

1. Spine-native verbal and source origin (stage 1).
2. Historical backfill preserving original authorship (stage 2).
3. #457 permanent-state dedup as a convergence prerequisite.
4. Coverage-aware semantic-equivalence gate and reader switch (stages 3 and 4).
5. #458 writer switch, bounded shadow comparison, rollback decision, and legacy retirement (stages 5 and 6).
6. Only then reconsider the route, model, and package refactors (#500, #501, #502) as fresh post-cutover work.

**Exit:** ADR-0074 marked superseded by ADR-0081.

## Frozen until the pilot gate

The universal documentation-readiness system (ADR-0052, ADR-0056, ADR-0060); the generalized task-management surface (ADR-0035); phone and offline work; broad notification and escalation machinery; additional automatic Record Inclusion classes; global content-inferred email routing; new legacy-table capabilities; report formats beyond the customer's UCM and weekly artifact; the optional Statement Review Assistant display program; the multi-engine PDF extraction platform; record-generated coordination paperwork.

Customer-format export and one real connector are **not** frozen; they are the slice.
