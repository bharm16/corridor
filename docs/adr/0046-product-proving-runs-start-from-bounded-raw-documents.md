---
status: accepted
---

# Product Test Runs start from bounded raw Documents

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Corridor has no external users yet, and the registered project data is development
test data rather than operational project truth. Product testing therefore uses the
real Corridor application and frontend with an autonomous simulated practitioner.
This is product testing: it neither grants an agent authority in a real Project
Record nor claims independent practitioner validation.

The retained technical operation and receipt identity are `ProductProvingRun`.
**Product Test Run** is the explanatory name; it does not weaken any input,
frontend, restoration, repeatability, or two-pass requirement below.

## Decision

A Product Test Run starts with one exact, bounded packet of raw Documents rather than an
already prepared Project Record or one downstream feature. The packet contains a
Spine Document, Stream Documents, and key date input. Corridor Operations performs
registration, ingestion, fresh extraction, Current Production Run declaration, and Record Inclusion;
the simulated practitioner then enters through the Work List and performs ordinary
project work only through the frontend.

The first packet uses one SH99 Utility Conflict Matrix, the two `minutes_v5`
Equistar Meeting Minutes Documents, and the SH99 key date input. Meeting Minutes
are one Stream source, not the center or starting point of the product. The test
path continues through the Project Record, Constraint log, Key Date Versions,
Required By dates, Documentation Review, Constraint Alerts, Evaluation, Follow-up Plans,
Coordination Report review, and one Report Approved for Release.

Extraction is fresh. Existing Current Production Runs are pinned comparison inputs, not reused
as the test run's output. A baseline must use the same extractor version, model,
schema version, and prompt digest; incompatible lineage refuses before semantic
comparison even when both runs produced zero Extracted Proposals. The new Extraction Runs
must match the baseline's canonical Extracted Proposal facts and Supporting Documentation. Extracted Proposal
identities and ordering may differ; an External Party Statement, External Organization,
timing, or citation that appears, disappears, or changes meaning is a repeatability
failure and stops the run before Record Inclusion. Baseline equality proves repeatability,
not correctness; Supporting Documentation validation, deterministic Record Inclusion, and
simulated-practitioner decisions remain separate checks.

For numbered Meeting Minutes Action Items, repeatability includes Extracted Proposal
membership. Corridor enumerates those rows from the exact registered page text and
deterministically recognizes only attributable External Organization commitments with
exact timing, changes to promised timing with two exact timings, and explicit
completion wording. That deterministic set replaces model output drawn from the
same Action Items; model omission, duplication, or reclassification cannot change
it. Unsupported untimed work and project-side Action Items remain outside the
Extracted Proposal set. Model extraction may still identify supported statements elsewhere
on the page.

Some historical Matrix runs predate immutable Extracted Proposal snapshots. An exact
same-Document replay may use the original attributable Record Inclusion plus the current
Constraint's exact verified Supporting Documentation and Assertions as its legacy proof. That
receipt names every supporting EvidenceLink and Assertion, explicitly identifies
extractor-only metadata that was not historically retained, and changes only the
duplicate Extracted Proposal disposition. It never reapplies an old field or overrides a
later human correction. A present but disagreeing snapshot, sealed partial lineage,
missing support, ambiguous association, or dismissed target still abstains.

Every residual Extracted Proposal produced by the exact packet is inspected. There is no
arbitrary Work Item count limit. The simulated practitioner records a supported
decision, marks the Extracted Proposal Do Not Add when appropriate, or leaves it explicitly
unresolved with the exact Supporting Documentation or authority gap. The run fails when the frontend
cannot represent the honest outcome. It also exercises one refused invalid action
and one Coordination Decision change before the final Coordination Report. A factual correction, Do Not Add disposition, report of completion, or Recorded Verbal Statement is exercised only when the bounded packet
supports that act. The simulated practitioner never fabricates a fact to satisfy a
test checklist. When no correction is exercised, the receipt records only that no
exact structured correction was observed; it does not claim that raw Supporting Documentation could
not support one.
Coordination Report verification requires correct provenance on every published value, not the
presence of a provenance class for which the bounded Project Record has no source.

Corridor Operations may use managed commands before the practitioner phase. Once
the Work List is open, ordinary project work must remain in the frontend. A terminal
command, direct database write, raw identity repair, or old external artifact needed
to complete an Extracted Proposal decision, Coordination Decision, correction, Coordination Report review, or
Report Approved for Release is a product failure. Non-blocking friction is recorded; a defect
that blocks or falsifies the workflow preserves a failed receipt, triggers a product
fix, restores the baseline, and requires a complete rerun.

The current development state is sealed before the run. The seal covers every
public PostgreSQL base table and sequence, not a hand-selected ORM projection, and
its data dump is restored and compared in a freshly migrated disposable database
before the first shared-development write. Test decisions and append-only history
remain in place for the complete scenario so later reads exercise real lifecycle
behavior. After every terminal pass, the shared development database is restored to
the exact sealed baseline and the complete table-and-sequence fingerprint is read
again. A Product Test Run result requires two consecutive complete passes from that same
restored baseline. This tests reset integrity, state isolation, extraction
repeatability, and the absence of lucky leaked state.

The first Product Test Run excludes Document Revision Processing. A separate
follow-on scenario introduces a successor Document and tests Supersession,
Revision Comparison, Human Support Update, and Automatic Support Update after
the core path from raw Documents to a Report Approved for Release passes.

## Claim boundary

The result may claim only that the bounded Corridor workflow completed through the
real frontend under simulated use. It does not satisfy Phase 1's independent
practitioner criterion and does not prove replacement of a project's manual weekly
Coordination Report. A manual Coordination Report is a coverage and usability benchmark, not Corridor's sole
input; replacement requires a populated same-project manual artifact and later real
practitioner use. Corridor's Coordination Report is built from upstream Documents, the Project
Record, Coordination Decisions, and an Evaluation rather than by copying a completed manual
Coordination Report.

## Rejected alternatives

- Starting from existing Extracted Proposals or an already prepared Project Record proves a
  downstream feature, not the Corridor product.
- Reusing Current Production Runs skips live extraction and cannot support a raw-Document claim.
- Treating a changed semantic Extracted Proposal set as harmless because a model is
  nondeterministic makes practitioner work unpredictable.
- Prompting the model more strongly to enumerate every numbered Action Item still
  leaves membership under probabilistic control and cannot support repeatability.
- Sampling residual Extracted Proposals can hide unsupported outcomes; grinding through an
  arbitrary fixed cap turns an extraction failure into unmeasured human labor.
- Requiring a correction, Do Not Add decision, report of completion, or Recorded Verbal Statement when the exact
  packet supplies no support for it turns an acceptance checklist into false Project
  Record data.
- Combining revision processing with the first weekly workflow makes failures harder
  to isolate.
- Reading an already completed manual Coordination Report and reproducing it tests document
  reformatting, not replacement of the manual reporting process.
