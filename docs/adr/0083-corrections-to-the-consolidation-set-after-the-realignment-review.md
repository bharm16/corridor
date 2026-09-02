---
status: accepted
domain: product
scope: current product
amends:
  - ADR-0075
  - ADR-0076
  - ADR-0078
  - ADR-0079
  - ADR-0080
  - ADR-0081
amended_by:
  - ADR-0084
migration: none of the corrected contracts (SourceEnvelope, PullConnector, PushIntake, run-kind provenance, customer-environment disposition, historical backfill) exists yet.
---

# Corrections to the consolidation set after the 2026-09-01 realignment review

**Amends ADR-0075, ADR-0076, ADR-0078, ADR-0079, ADR-0080, and ADR-0081.**

The [realignment review](../research/adr-code-open-issue-realignment-review-2026-09-01.md) accepted the direction of ADR-0075 through ADR-0081 and found specific normative errors in six of them. Each correction below changes a "must", a boundary, or a sequence, so under the lifecycle rule in [README.md](README.md) they are recorded here as explicit amendments rather than edited in place. ADR-0077's defects were large enough for a successor (ADR-0082). Nothing else in the amended ADRs changes.

## ADR-0075

- **Claim precision.** "The coordination record is maintained by hand in whatever system holds it" is too absolute. Existing systems automate portions of maintenance and workflow. The defensible claim is: substantial cross-source reconciliation between arriving evidence and the accepted record remains manual.
- **Buyer is a hypothesis.** "The first commercial buyer is a consultant" is the provisional design-partner target, subject to the buyer discovery in amended #428. It is not a repository-supported fact and may not be quoted as one until that discovery is recorded.
- **Freeze boundary.** Customer-format export of the updated UCM and one real connector are part of the first paid slice, not "broad feature expansion". The freeze list in ADR-0075 does not cover them.

## ADR-0076

- **Proposed Delta lifecycle.** A Proposed Delta has a typed identity (delta type, subject, field, accepted revision, incoming source version and facts, comparison rule version). The lifecycle is: open → resolved (accepted, edited, rejected, deferred) or superseded (a newer source version for the same subject and field coalesces into one live delta), with stale-input refusal when the accepted revision moved, deterministic reopening when a rejected value recurs from a new source, and one-item grouping for an atomic source change. The same source version cannot create duplicate live deltas.
- **Baseline adoption is human.** Adopt Baseline is a bulk human command with a named principal. It is not a member of the "automatic projection" list, which now reads: exact unchanged support transfer; exact no-semantic-change normalization; separately released class-specific policies.
- **Support transfer is not a value decision.** Exact unchanged support transfer (ADR-0022 as amended by ADR-0034) moves Supporting Documentation in Use. It never creates or changes an accepted value and never produces a Proposed Delta.
- **Apparent removal is its own band.** An `apparent removal` delta is a record-change question with distinct risk. It is not grouped with statements Corridor could not place.
- **Bands and ordering.** ADR-0076 owns only the fact that Proposed Deltas expose auditable consequence bands. Exact presentation ordering within and across bands belongs to ADR-0035 or a later work-list decision.
- **Write boundary is layered.** The architecture test is defense in depth, not the sole enforcement. The canonical spine is writable only through restricted database roles and `SECURITY DEFINER` functions (the pattern ADR-0074 already uses), with the static allowlist and a runtime bypass test on top. Segments, facts, and proposals have their own authorized appenders, separate from accepted-record decision writers.
- **ADR-0034 is amended.** Its #196 sequencing section assumes ADR-0029 mechanical Record Inclusion; that assumption is replaced by ADR-0076's operations. ADR-0034 records `amended_by: ADR-0076`.

## ADR-0078

- **Three contracts, one envelope.** The four-method contract is a **pull** connector. It does not model an inbound webhook, a project alias, a manual upload, or a pushed schedule export. The intake design is three layers:
  1. `SourceEnvelope`: the one normalized ingress record every channel produces (customer, project, channel, external identity and version, original timestamps, content digest and bytes reference, metadata, delivery identity, idempotency key).
  2. `PullConnector`: `list_changes`, `fetch_version`, `get_metadata`, `checkpoint` (renamed from `acknowledge`).
  3. `PushIntake`: authenticated, pre-bound delivery for aliases, webhooks, and uploads.
- **Checkpoint semantics.** A checkpoint advances only after every change up to and including the exact token is durably stored with its digest. Replay after a crash is idempotent by delivery identity; a checkpoint never advances past an unstored change.

## ADR-0079

- **Project boundary.** One database per customer does not make the project a database boundary. Projects remain authorization and data-partition boundaries inside the customer database, enforced by the application and its roles.
- **Provenance by run kind.** Migration, control-plane, customer-level connector, and maintenance runs may have no project or source revision. Required fields are declared per run kind; the universal list in ADR-0079 is the maximum, not the minimum.
- **"Source revision" is split** into code revision, input or source identity and version, extractor or policy version, and the Project Record revision the run read.
- **Logical roles.** "One application container" and "one worker container" name logical deployable roles, not a permanent one-replica limit. Leases and idempotency must be replica-safe from the first deployment.
- **Control-plane data.** Cross-customer control-plane data (deployment inventory, customer registry, hold state, disposition receipts, connector credentials) lives in a separate control-plane database and object namespace, never inside a customer environment.

## ADR-0080

- **Pilot posture.** A granular row-class disposition engine across 149 tables is not required before the first paid pilot. With one database and object namespace per customer, pilot disposition is **export and destroy of the complete customer environment**: contractually declared retention; legal hold; export or custody transfer; whole-environment destruction; backup and snapshot expiration; an external compliance receipt. Project-level or record-class granularity is earned by a signed customer requirement.
- **Receipt custody.** A deletion receipt cannot survive only in the customer database being destroyed. Receipts live in the control-plane store (ADR-0079 as amended above).
- **Referential retention.** A raw source cannot be disposed while a retained decision or released artifact still promises dereference to it, unless custody was transferred and the remaining record explicitly states the source is unavailable.
- **Precedence.** An active hold overrides every schedule. Overlapping schedules resolve to the longest retention. Precedence is recorded on the plan.
- **Partial failure.** Disposition is a resumable, receipted sequence across PostgreSQL, object storage, encryption keys where used, and backups. A partially executed plan is reported as such and resumed, never re-planned silently.
- **Backups.** "Deleted" data that remains in point-in-time recovery or snapshots is not deleted. Every schedule states the backup expiration that completes it.

## ADR-0081

- **Historical lineage.** Backfilling only active conclusions cannot support as-of readings or audit reconstruction. Stage 2 migrates the historical lineage each equivalence surface needs, or declares a temporary compatibility reader for a named class with an expiry criterion. The declaration is explicit per class.
- **Authorship.** The migration executor is never the semantic author. Backfilled facts and decisions preserve the original human or system actor, event time, source identity, and decision type; the migration run records its executor and receipt separately.
- **Semantic contract equivalence.** Stage 3's gate is semantic equivalence against a declared field and record inventory per surface. Byte- or cell-identical output is required only for surfaces intentionally unchanged. The current overlay in `prove_reader_equivalence` replaces a narrow field subset in a frozen legacy reading, so today's green gate is not evidence that any reader is spine-native; the gate becomes coverage-aware before stage 4.
- **Rollback window.** Stage 5 declares the window start and end, the customer cohort allowed in it, divergence thresholds that trigger rollback, the traffic owner, and the decision authority, before the writer switch.
- **Stage 6.** Retirement includes backup expiry for the dropped tables and their treatment under ADR-0080.

## Consequences

- The amended ADRs record `amended_by: ADR-0083`; their bodies are unchanged.
- Issues #496 (pull connector), #511 (push intake), #489, #458, and #514 (disposition) are written against these corrections in the same review.
- No implementation of `semantic_support_status`, granular disposition, or change-inbox schema begins before ADR-0082 and this ADR are merged.
