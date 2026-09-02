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

1. #505: ADR-0082 supersedes ADR-0077 (provenance by value class; support as a relation).
2. #506: ADR index authority semantics and lifecycle validation fixed; `make check` runs as a standalone CI status (server-side enforcement needs a ruleset the private free-plan repository cannot create).
3. #507: this roadmap and the exact pilot contract published.
4. #508: glossary entries for Source Fact, Adopt Baseline, Proposed Delta, Resolve Delta.
5. ADR-0078/0080/0081 corrections recorded in ADR-0083.

6. Implementation-readiness corrections (the [2026-09-01 readiness review](docs/research/implementation-readiness-review-2026-09-01.md)): main restored to green (#516); amended issues rewritten into single contracts; #510 corrected and split; pilot sampling fixed; contributor docs mark the current pipeline transitional.
7. Implementation-contract corrections (the [2026-09-01 late contract review](docs/research/implementation-contract-review-2026-09-01-late.md)): ADR-0084 (deferral is Work List scheduling, not a semantic disposition; non-verbal work precedes spine-native verbal origin; adopted-baseline projects read the spine natively). #492/#518/#519/#520 contracts hardened; #530 Support Assessment relation added; #489 split from live activation (#535); #491 split into #491A/#532; #534 change summary and weekly report owned; #503 decision separated from its implementation (#531); a metadata-derived spine-cleanup inventory test replaces the hand-listed table set.

**Exit:** #505, #507, #508, #516 closed and this change merged. Server-side status enforcement (#506) stays unavailable on this plan; the manual all-green merge rule in AGENTS.md applies.

## Phase 1 — secure the design partner and deploy the safe shell

1. #428: validate the consultant-first buyer, budget, sponsor, procurement, and pricing hypotheses with real discovery.
2. #461: resolve PyMuPDF licensing before any external deployment that uses it.
3. #492 least-privileged database write authority first (the first substantive code PR: separate deployment credentials, NOLOGIN function owners, no PUBLIC execute, command-only appends; the database itself refuses application-role writes to accepted authority). Then #487 storage interface and object storage; #490 staged untrusted-intake hardening (byte gate, sandboxed structural inspection, rich processing; real scanner or a named risk acceptance before external data); #491A operational telemetry; #489 synthetic environment foundation (no customer data).
4. #496 pull connector and normalized SourceEnvelope; #511 project-bound push intake, built only for channels the partner needs.
5. Live-activation gates (#535): #461 licensing, #522 customer-data and provider governance, #531 identity/authorization/deprovisioning (implementing #503), #514 customer-environment disposition. Real customer data enters only after all of them and after the project is in adopted-baseline mode (#520); development bytes may enter only an isolated non-authoritative environment with processing disabled.

**Exit:** #489 synthetic environment with one customer database, backups restored once, intake hardened; #535 gates identified and tracked; one partner's connector registered.

## Phase 2 — build the paid vertical slice on the spine

The spine foundations come before Adopt Baseline; non-authoritative #509 parsing, preview, and fixtures may proceed earlier. Corrected order:

1. #530 Support Assessment relation (proposition-to-source assessments; ADR-0082), on #492.
2. #446 replayable Source Fact materialization enforcement (a model literal never becomes a Source Fact).
3. #518 Proposed Delta identity, groups, and lifecycle (immutable occurrence + derived live view; source-lineage coalescing; apparent-removal completeness proof).
4. #520 adopted-baseline operating-mode rails and database refusal (before #509, not after).
5. #509 Adopt Baseline, invoking the #520 transition atomically (accepted writer on #492, #520, #530).
6. #519 typed Resolve Delta commands (accept/edit/reject are semantic; defer is Work List scheduling, ADR-0084).
7. #494 Work List of Proposed Deltas.
8. #495 UCM export + #534 change summary and weekly report + #425 chase list, all from one frozen revision.
9. #450 schedule source, #455 email source, #456 minutes source, each only for the partner's source classes (#455/#456 on #446).

**Exit:** the slice runs end to end on one partner project with the partner's own workbook and one connected source, and the adopted project runs in baseline/delta operating mode (no legacy automatic accepted-value update).

Recorded Verbal Statements are outside the gated pilot source population until #512 ships; they remain available as legacy-compatible context (pilot criteria, source classes).

## Phase 3 — run the shadow pilot

1. #424 net economics measurement, #498 pilot gate report, #499 shadow comparison against customer matrix revisions.
2. Freeze each baseline and incoming-source event before coordinator action.
3. Measure coordinator time, Corridor operations time, latency, coverage, material errors, false or low-value work, and cost against the pilot criteria.
4. Produce the predeclared continue, narrow, or stop decision.

**Exit:** #498 closed with one finding per criterion.

## Phase 4 — finish the canonical cutover (ADR-0081)

1. #512 spine-native verbal and source origin (stage 1).
2. #513 historical backfill preserving original authorship (stage 2).
3. #457 permanent-state dedup as a convergence prerequisite.
4. Coverage-aware semantic-equivalence gate and reader switch (stages 3 and 4).
5. #458 writer switch, bounded shadow comparison, rollback decision, and legacy retirement (stages 5 and 6).
6. Only then reconsider the route, model, and package refactors (#500, #501, #502) as fresh post-cutover work.

**Exit:** ADR-0074 marked superseded by ADR-0081.

## Frozen until the pilot gate

The universal documentation-readiness system (ADR-0052, ADR-0056, ADR-0060); the generalized task-management surface (ADR-0035); phone and offline work; broad notification and escalation machinery; additional automatic Record Inclusion classes; global content-inferred email routing; new legacy-table capabilities; report formats beyond the customer's UCM and weekly artifact; the optional Statement Review Assistant display program; the multi-engine PDF extraction platform; record-generated coordination paperwork.

Customer-format export and one real connector are **not** frozen; they are the slice.
