# Phase 1 Roadmap — External-Party Readiness Project Record

**Goal of Phase 1:** a working, demoable Project Record for utility and External Party readiness, running on 2-3 real highway projects, operable by one person, no external accounts required.

> **Current direction, 2026-09-01.** The milestones below are the Phase 1 build
> history and remain accurate as history. The product now optimizes around one
> slice: take the customer's current record, observe one real new piece of
> project evidence, present the correct proposed change, and return the updated
> artifact with less coordinator work than today
> ([ADR-0075](docs/adr/0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md),
> [ADR-0076](docs/adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md)).
> The slice is import existing UCM → adopt baseline → connect one mailbox or
> folder → process one new source → show proposed deltas → accept/reject/edit →
> export updated UCM and report; its gate is
> [docs/pilot-success-criteria.md](docs/pilot-success-criteria.md). Until that
> pilot runs, the readiness system (ADR-0052/0056/0060), the generalized
> task-management surface (ADR-0035), phone and offline work, broad
> notifications, further automatic Record Inclusion classes, global
> content-inferred email routing, new legacy-table capabilities (ADR-0081), and
> extra report formats are frozen. Platform work is ADR-0078 (connectors),
> ADR-0079 (deployment), ADR-0080 (disposition), and ADR-0081 (spine
> migration).

**Companion docs:** `v0-build-spec.md` covers milestones M0-M5 in implementation detail. `corpus-acquisition-spec.md` covers document assembly, which runs in parallel from week 0. `CONTEXT-MAP.md` locates the domain vocabulary; `docs/adr/` records decisions with lasting consequences; `docs/adr/INDEX.md` says which govern today. Build nothing past M5 until M5 is done.

---

## Scope guardrails

Phase 1 never includes: CAD/GIS, scheduling engine, document-management or PMIS replacement, chatbot interface, autonomous schedule changes, legal notices, permit engine, external-party portal, marketplace, SSO/enterprise security, deep bidirectional integrations. If a task list contains any of these, cut it.

---

## Milestones

### M0 — Corpus and environment (from week 0, in parallel)

No documents are in hand. Corpus assembly is the schedule's critical path rather than its warm-up, and is specified in `corpus-acquisition-spec.md`.

- Assemble document sets for 2-3 real projects from public sources. Each project needs a **spine** (dependency records — filled conflict matrix, utility agreements, special provisions) and a **stream** (dated assertions that change over time — serial status reports, meeting minutes, board packets). Neither role alone exercises the data model. Construct a milestone list per project.
- File public-records requests in week 0, scoped to the same project as the spine so returns join one Project Record.
- Repo, Postgres, object storage dirs, one-command boot.

**Done when:** the manifest resolves to files on disk with source provenance, cataloged by project and type; stack boots with one command.

### M1 — Evidence store and ingestion (1 wk)

- Content-addressed (SHA-256) immutable file store; originals never modified.
- Document registry: project, type, source, date, parse status.
- Text extraction with OCR fallback; per-page text + page images for evidence display.

**Done when:** every file retrievable with page text and page image; re-ingest is idempotent.

### M2 — Extraction pipeline (1.5-2 wk)

- Type-specific LLM extractors: conflict matrix, meeting minutes, serial status reports, agreements, email. Output = candidate dependency records and events, every field cited to doc/page/quote.
- Automatic citation verifier (quote must match cited page).

**Done when:** candidates generated for Project A with verified citations; candidate junk rate under ~30%.

### M3 — Adjudication and Project Record (1.5-2 wk)

- Review queue: accept / edit / merge / reject, keyboard-driven.
- Entity resolution assist (same dependency across matrix, minutes, email → merge).
- Project Record with a Dependency Ledger, External Party Statements, Work Decisions, Evidence, and append-only receipts. Every merged claim is retained as an Assertion against the record, so competing source values survive rather than overwriting each other.

**Done when:** all Project A Candidates have an explicit outcome; the Ledger is browsable by location, External Party, Resolution Strategy, Ready, and Exception; a Dependency with conflicting Assertions shows both.

### M4 — Schedule link and exceptions (1 wk)

- Immutable Milestone Registration from exact CSV source rows and digests; Need Date is derived from the current registration linked to each Dependency.
- Exception engine: missing owner, missing date, missing evidence, stale, due-soon, overdue, contradiction, orphan (exact rules in build spec).

**Done when:** exception list matches a hand-check of Project A.

### M5 — Reporting and demo (1 wk) — **v0 complete**

- Cited weekly readiness report (HTML/PDF): milestone rollup, critical items, exceptions, changes since last report, aging.
- Project Record export to XLSX.
- End-to-end demo path: raw Documents → Admission and Adjudication → Project Record → reviewed Approved Export, live in under 15 minutes.

**Done when:** report contains zero uncited assertions; full run works on Project A. This is the demo checkpoint artifact.

### M6 — Second-project generalization (1 wk)

- Run the entire pipeline on Project B with zero code changes; fix every schema or prompt assumption that breaks.

**Done when:** differences between projects are config only.

### M7 — Quality gate and Extraction Measurement (1 wk)

- Machine reference on a held-out project: read the project's own filled Utility Conflict Matrix through a meaningfully different path, stamp shared blind spots, and report the result as a semi-independent ceiling rather than human gold. Use authoritative structured records, downstream corrections, or independent audits when they exist; do not require founder-authored labels.
- Metrics per exact Extraction Run: critical-dependency recall against the declared reference, overall recall/precision, citation validity, field-token failures, coverage, and the reference's limitations.

**Done when:** ≥95% recall on reference-marked critical Dependencies; 100% citation validity; metrics and limitations are stored for every exact Extraction Run included in a declared Extraction Measurement.

### M8 — Document versioning and revisions (1-1.5 wk)

- Document Supersession (rev B → rev C), exact-run Revision Comparison, affected Project Record support, and Operative Support that cites a superseded revision.
- Design is decision-complete in ADR-0015 through ADR-0023: supersession registry, `SUPERSEDED_CITATION` on operative support, role-scoped resolver, Revision Comparison, fail-closed queue with declared Active Runs, retirement of the noncompliant development Ledger, opt-in Automatic Carry-Forward for exact unchanged support, and machine-reference limits; ADR-0024 adds that its acceptance captures are regression artifacts, never production run lineage. Vocabulary in `CONTEXT.md`.

**Done when:** registering a revised document yields a Revision Comparison and a Supersession Review worklist; Revision Processing read-verifies the Comparison before invoking any authorized policy; exact-run Extraction Measurement is reproducible; human Reconfirmation or policy-authorized Automatic Carry-Forward moves existing Operative Support; and uncertain cases produce durable versioned Abstentions. Production history and sealed receipts are never overwritten; identical automation reruns do not duplicate outcomes; ADR-0021's development-only retirement is explicit, immutable, and independently verifiable.

### M9 — Second user and write-back lite (1.5-2 wk)

- Basic auth, three roles (admin / reviewer / viewer).
- P6 XER input that appends Milestone Registrations rather than mutating bare dates.
- Round-trip export: Project Record values back out in the matrix format the project already uses.
- Backup/restore.

**Done when:** a second person can review safely; the ledger round-trips into the project's existing matrix format.

---

## Total effort

~11-14 focused solo weeks. Nights-and-weekends pace: roughly double the calendar time.

## Sequencing rules

1. Nothing gets built ahead of M5 — no auth, no integrations, no polish beyond the demo path.
2. Synthetic fixtures may exercise code paths. No synthetic document may ever contribute to an `eval_run`, a quality number, or a demo — every quality claim is made against real project documents.
3. No integration work (M9's XER import included) before two projects run clean.
4. M7's eval gate is not skippable; it is the only objective measure that the core works.

## Phase 1 exit criteria (unlocks Phase 2)

- Pipeline runs on 3 projects with zero project-specific code.
- Eval gate passing on a held-out project.
- A full weekly readiness meeting is run from the tool alone **by a practitioner who does the work**, with every fallback to the old artifact recorded.
- The generated report fully replaces a manually built weekly report format. Published templates establish the baseline; replacement is proven only against a real project's populated report and that practitioner-run meeting.

## Phase 2 pointer (out of scope here)

Multi-user assignment, acknowledgment, approvals, and notifications; external link/email-based responses without accounts; closure evidence requirements; schedule write-back; reusable owner templates; SSO and security package.

Phase 1 carries single-operator, attributable internal Work Decisions — assigning an Internal Owner, a Next Action, and an Action Due Date is in scope (ADR-0025); everything multi-user or external-party-facing about them is not.
