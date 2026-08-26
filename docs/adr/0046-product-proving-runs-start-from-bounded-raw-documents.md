---
status: accepted
---

# Product proving runs start from bounded raw Documents

Corridor has no external users yet, and the registered project data is development
test data rather than operational project truth. Product proving therefore uses the
real Corridor application and frontend with an autonomous simulated practitioner.
This is product testing: it neither grants an agent authority in a real Project
Record nor claims independent practitioner validation.

## Decision

A proving run starts with one exact, bounded packet of raw Documents rather than an
already prepared Project Record or one downstream feature. The packet contains a
Spine Document, Stream Documents, and Milestone input. Corridor Operations performs
registration, ingestion, fresh extraction, Active Run declaration, and Admission;
the simulated practitioner then enters through the Work List and performs ordinary
project work only through the frontend.

The first packet uses one SH99 Utility Conflict Matrix, the two `minutes_v4`
Equistar Meeting Minutes Documents, and the SH99 Milestone input. Meeting Minutes
are one Stream source, not the center or starting point of the product. The proving
path continues through the Project Record, Dependency Ledger, Milestone
Registrations, Need Dates, Ready, Exceptions, Evaluation, Coordination Plans,
Report review, and one Approved Export.

Extraction is fresh. Existing Active Runs are pinned comparison inputs, not reused
as the proving run's output. A baseline must use the same extractor version, model,
schema version, and prompt digest; incompatible lineage refuses before semantic
comparison even when both runs produced zero Candidates. The new Extraction Runs
must match the baseline's canonical Candidate facts and Evidence. Candidate
identities and ordering may differ; an External Party Statement, External Party,
timing, or citation that appears, disappears, or changes meaning is a repeatability
failure and stops the run before Admission. Baseline equality proves repeatability,
not correctness; Evidence validation, deterministic Admission, and
simulated-practitioner decisions remain separate checks.

Some historical Matrix runs predate immutable Candidate snapshots. An exact
same-Document replay may use the original attributable Admission plus the current
Dependency's exact verified Evidence and Assertions as its legacy proof. That
receipt names every supporting EvidenceLink and Assertion, explicitly identifies
extractor-only metadata that was not historically retained, and changes only the
duplicate Candidate disposition. It never reapplies an old field or overrides a
later human correction. A present but disagreeing snapshot, sealed partial lineage,
missing support, ambiguous association, or dismissed target still abstains.

Every residual Candidate produced by the exact packet is inspected. There is no
arbitrary Work Item count limit. The simulated practitioner records a supported
decision, marks the Candidate Not Relevant when appropriate, or leaves it explicitly
unresolved with the exact Evidence or authority gap. The run fails when the frontend
cannot represent the honest outcome. It also exercises one refused invalid action
and one Work Decision change before the final Report. A factual correction, Not
Relevant disposition, closure, or Verbal is exercised only when the bounded packet
supports that act. The simulated practitioner never fabricates a fact to satisfy a
test checklist. When no correction is exercised, the receipt records only that no
exact structured correction was observed; it does not claim that raw Evidence could
not support one.
Report verification requires correct provenance on every published value, not the
presence of a provenance class for which the bounded Project Record has no source.

Corridor Operations may use managed commands before the practitioner phase. Once
the Work List is open, ordinary project work must remain in the frontend. A terminal
command, direct database write, raw identity repair, or old external artifact needed
to complete a Candidate decision, Work Decision, correction, Report review, or
Approved Export is a product failure. Non-blocking friction is recorded; a defect
that blocks or falsifies the workflow preserves a failed receipt, triggers a product
fix, restores the baseline, and requires a complete rerun.

The current development state is sealed before the run. The seal covers every
public PostgreSQL base table and sequence, not a hand-selected ORM projection, and
its data dump is restored and compared in a freshly migrated disposable database
before the first shared-development write. Test decisions and append-only history
remain in place for the complete scenario so later reads exercise real lifecycle
behavior. After every terminal pass, the shared development database is restored to
the exact sealed baseline and the complete table-and-sequence fingerprint is read
again. A proving result requires two consecutive complete passes from that same
restored baseline. This tests reset integrity, state isolation, extraction
repeatability, and the absence of lucky leaked state.

The first proving run excludes Document revision processing. A separate follow-on
scenario introduces a successor Document and tests Supersession, Revision
Comparison, Reconfirmation, and Automatic Carry-Forward after the core raw-Document
to Approved-Export path passes.

## Claim boundary

The result may claim only that the bounded Corridor workflow completed through the
real frontend under simulated use. It does not satisfy Phase 1's independent
practitioner criterion and does not prove replacement of a project's manual weekly
Report. A manual Report is a coverage and usability benchmark, not Corridor's sole
input; replacement requires a populated same-project manual artifact and later real
practitioner use. Corridor's Report is built from upstream Documents, the Project
Record, Work Decisions, and an Evaluation rather than by copying a completed manual
Report.

## Rejected alternatives

- Starting from existing Candidates or an already prepared Project Record proves a
  downstream feature, not the Corridor product.
- Reusing Active Runs skips live extraction and cannot support a raw-Document claim.
- Treating a changed semantic Candidate set as harmless because a model is
  nondeterministic makes practitioner work unpredictable.
- Sampling residual Candidates can hide unsupported outcomes; grinding through an
  arbitrary fixed cap turns an extraction failure into unmeasured human labor.
- Requiring a correction, Not Relevant decision, closure, or Verbal when the exact
  packet supplies no support for it turns an acceptance checklist into false Project
  Record data.
- Combining revision processing with the first weekly workflow makes failures harder
  to isolate.
- Reading an already completed manual Report and reproducing it tests document
  reformatting, not replacement of the manual reporting process.
