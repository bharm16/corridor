# SH 99 External Party commitment rehearsal, bounded

> Re-decided 2026-08-10 and completed by the post-foundation decisions recorded in
> ADR-0034 through ADR-0040. This contract supersedes the earlier event-cohort rehearsal
> plan. Admission is mechanical under ADR-0029; customer users do not authorize
> policy or operate Active Runs; Committed Date Change replaces the prior later-date
> classification. The former Air Products overdue example is rejected because its
> source is an invitation, not an Air Products commitment.

The City of Houston rehearsal proved that Corridor can organize cited Dependencies.
This rehearsal is the first narrow coordinator slice for the date half of the
product. It must prove that Corridor can preserve what an identified External Party
actually stated, at the precision and scope the source supports, and turn that fact
into useful coordination work and an honest Report.

It is an **internal workflow rehearsal**, not target-user validation. No realistic
project coordinator is currently available to test it. The timings below are
provisional product targets measured by the internal operator through the normal
interface. They cannot be reported as customer usability evidence.

## The claim to earn

For two real SH 99 statements, a project coordinator can start from one prioritized
work list and, without a command line, database query, technical id, or separate
screen search:

1. preserve a cited Kinder Morgan Committed Date Change whose Dependency scope is
   not safely known; and
2. respond to a cited, party-level Equistar Commitment that became past due after
   January 2025, then release a fixed Report export that presents it without
   inventing a Dependency or an exact January day.

The rehearsal succeeds only if the product distinguishes the External Party's
statement from the project's Coordination Plan. Assigning an Internal Owner,
completing a Next Action, or closing the work-list item cannot rewrite or close the
External Party commitment.

## The three source cases

### Candidate 7296 — guided Committed Date Change case

The Candidate's cited statement says: “The March 2026 completion timeline seems
unattainable. Propose extending to May 16th.” It preserves the earlier timing, the
new timing, their source wording and precision, and the later direction. Verified
surrounding Evidence must establish Kinder Morgan as the stated party; confirmation
alone is insufficient. The
statement does not safely identify one Dependency. The coordinator therefore
sees the general choices—one Dependency, a selected set, every currently active
Kinder Morgan Dependency as an explicit snapshot, or **scope not yet known**—and
chooses **scope not yet known** for this source. Unknown scope preserves the
party-level statement and changes no individual Dependency.

### Candidate 7129 — guided party-level past-due case

Equistar's cited statement says: “Equistar to provide a chain of title on the ROW
agreement that is in DOW's name (Due date of 01/2025).” This is attributable to
Equistar. Its timing is **January 2025**, not January 1, 2025, and it becomes past
due only after January 31. No closure is currently established. Because the source
does not establish one Dependency, the commitment stays party-level until its scope
is confirmed.

### Candidate 7587 — required negative attribution case

The Air Products quote says that Air Products “will be invited” to a workshop on May
8, 2025. It does not say Air Products promised to deliver anything on that date.
Corridor must refuse to set an Air Products Committed Date or produce an overdue
fact from it. The affected party is not automatically the stated actor.

These cases replace the earlier plan's claim that Candidate 7587 proved a real PL35
overdue commitment. It does not. The five earlier PL35 closure Candidates add
contradiction risk, but the actor-attribution failure is sufficient to reject the
claim before closure is considered.

## Operating boundary

At the 2026-08-10 planning checkpoint, SH 99 has no admitted Dependencies and 3,030
pending Candidates. That backlog is not coordinator work. Before the timed rehearsal,
Corridor operations pins the source revision, database snapshot, declared Active
Runs, policy version, and corpus inputs. It rehearses mechanical Admission from clean
current `main` against an isolated clone of that database before requesting explicit
approval to mutate the shared SH 99 database.

The receipt audit records exact before and after Ledger rows, Candidate states,
Policy Runs, per-Candidate outcomes, and reason codes; it does not guess expected
totals. Candidate 7587 must abstain, and Candidates 7129 and 7296 must remain pending
for guided Adjudication rather than gaining guessed attribution or scope. Repeating
the same backfill may append an immutable run receipt but creates no duplicate Ledger
fact. Operations then seeds one individual coordinator identity. Setup, repair, and
backfill time are measured separately and excluded from coordinator timing.

Any customer-facing use of a CLI command, database query, Active Run id,
Carry-Forward authorization, or manual record repair fails the rehearsal. A trained
Corridor operator may use internal technical tools outside the timed workflow, with
receipts. Magic-link sign-in, connected ingestion, and the managed Active Run and
Automatic Carry-Forward redesign remain required before customer use but are not on
issue #196's critical path.

The existing SH 99 EventCohortReceipt, cohort lane, and their tests are not this
rehearsal's acceptance boundary and must not be expanded merely to satisfy the former
plan for issue #196. A child slice may reuse compatible mechanics or retire the
residue after the new attribution, precision, scope, and guided-workflow contract is
represented.

## Timed coordinator workflow

### A. Record the Kinder Morgan change — target: at most 5 minutes

1. Open the highest-priority plain-language work item from the coordinator home.
2. Read the cited statement beside the proposed Committed Date Change.
3. Use verified Evidence, including a separate verified quote if required, to
   establish Kinder Morgan as the stated party; preserve both timings and their
   precision without erasing the Candidate. Corridor derives the statement type and
   direction from those supported facts.
4. Choose **scope not yet known**. The general control also supports all currently
   active party Dependencies as an explicit snapshot, one Dependency, or a selected
   set, but this source does not justify any of those choices.
5. Record Milestone Impact as **affects**, **does not affect**, or **not yet known**;
   `affects` names exact registered Milestones. Save the current Internal Owner, Next
   Action, and Action Due Date—or structured reason a date is not yet known—in one
   Coordination Plan. The statement and separate Work Decisions commit atomically.

### B. Respond to the Equistar past-due commitment and release the export — target:
at most 3 minutes

1. Open the Equistar work item from the same prioritized list and see the plain fact:
   “The January 2025 commitment passed, and no closure is recorded.”
2. Verify the stated party, month precision, source, and unresolved party-level scope.
3. Save the current Coordination Plan. The overdue fact remains until Evidence or an
   attributable Verbal establishes closure, even if the immediate work item leaves
   the inbox while that Next Action is current.
4. Open the automatically updated internal Report and explicitly release one fixed
   PDF. The export includes every open unknown-scope party-level Commitment—not only
   Equistar or overdue entries—in an External Party commitments section. Its receipt
   binds exact bytes, SHA-256, Evaluation date, ruleset, provenance mode, covered
   records, releaser, and timestamp. Sending the same sealed bytes through email or
   document control is outside Corridor and outside this rehearsal.

The total provisional coordinator target is **at most 8 minutes**. The clock starts
when the seeded coordinator opens the prioritized work item and stops when the fixed
export and its release receipt are persisted. Wall-clock time includes any wait for
export generation. Operations setup has its own measurement.

## Exit criteria

1. Mechanical backfill on the isolated clone produces the exact before/after receipt
   audit and an idempotent second run without duplicate Ledger facts. Shared-database
   execution remains separately approved; the timed user sees no setup machinery.
2. Stated party and affected party are separate facts. Candidate 7587 records an
   Admission Abstention, creates no Commitment, and cannot create a past-due fact;
   Candidates 7129 and 7296 remain pending after mechanical backfill.
3. Guided Adjudication makes Candidate 7296 one party-level Committed Date Change
   with **scope not yet
   known**, preserving both source timings and direction and changing no Dependency.
   The scope control and automated coverage also prove that one Commitment can link
   all active party Dependencies or a selected set without duplicating the event.
4. Guided Adjudication makes Candidate 7129 display as January 2025, become past due
   only after January 31, and remain party-level without invented Dependency scope.
5. The coordinator completes the 7296 flow in at most 5 minutes and the 7129 plus
   release flow in at most 3 minutes, through the normal interface only.
6. The Report has an External Party commitments section containing every open
   unknown-scope party-level Commitment, with party, statement, timing text and
   precision, scope status, current Coordination Plan, status, and exact provenance.
   Release seals one PDF with all ADR-0040 receipt fields. Unsupported provenance,
   inconsistent Evaluation inputs, or inability to seal the bytes blocks release;
   honest unknown scope, past due, or visible missing work does not.
7. Completing internal work does not close the External Party commitment. Closure
   requires verified Evidence or a Verbal attributable to that party.
8. One stale guided Save refuses without partial mutation. Immediate Undo appends
   reversal records, makes the entire Save noncurrent, and returns the Candidate to
   work; dependent later decisions force targeted Correct instead.
9. History remains append-only and plain by default: what changed, who acted, when,
   why, and the source. Corrections, restorations, and replacements do not delete the
   original record.

## What this rehearsal does not prove

- usability by a real construction worker or project coordinator;
- the still-deferred construction-worker Ledger permission boundary;
- production magic-link or SSO authentication;
- connected document ingestion or the managed technical-operations redesign;
- external delivery of the approved export;
- an exact-day overdue calculation for a month-only source; or
- every human workflow in ADR-0035, ADR-0037, and ADR-0040.

## Build order

Two cleanup deliveries precede new #196 children. Transplant local commit `7b9a233`
onto fresh current `main`, review and validate it independently, and do not merge its
divergent branch wholesale. Deliver ADR-0034 through ADR-0040 and this corrected
contract as a separate change from fresh current `main`.

The remaining #196 children then run in this order:

1. mechanical backfill and exact receipt validation;
2. statement-subject Work Decisions and their migration;
3. guided Adjudication with atomic Save, correction, and Undo;
4. the prioritized work list and precision-aware party-level past due;
5. the External Party commitments Report section;
6. fixed PDF release; and
7. the timed internal rehearsal.

Report-comparator research in #150 and the human records request in #151 may proceed
in parallel but do not displace this outcome. Issues #164, #165, #172, and #70 are a
separate tracker cleanup, not runnable product work by assumption; each is closed,
superseded, or deferred only after its live evidence is recorded. Publishing the
bounded #196 child graph follows the two cleanup deliveries, and its backfill child
retains the explicit shared-database approval gate.

Implementation work is split into child issues only after this umbrella contract is
accepted. Each child owns one vertical slice and its tests; #196 remains the outcome
contract rather than a grab bag of implementation tasks.
