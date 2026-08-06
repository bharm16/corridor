# The NHHIP workflow rehearsal: City of Houston, bounded

Decided 2026-08-06. The active Ledger is empty by design (ADR-0021), and the way back is not
"repopulate the Ledger" or humans clearing thousands of Candidates. The goal is to prove an
end-to-end utility-conflict workflow on one real coordination slice: surface the flagged
conflicts, consolidate their evidence, show what is missing or contradictory, decide the next
action, assign it, and track resolution. Human adjudication is a control at consequential
decision points, not the product.

## The cohort, and what it is not

**City of Houston, provisionally — the workflow-rehearsal cohort, not the status-evidence
pilot and not Phase-1 proof.** City provides the learning density: 154 rows (151 verified,
3 verification-blocked — W163, WW7, WW72, all City water/wastewater), 112 document-flagged
`Y` conflicts, revision churn across the five inventory revisions. CenterPoint has more
volume (135 Y of 212) and would teach less.

The first pass is **deterministic, not judgmental**: every City conflict that is newly
added, changed `N→Y`, or verification-blocked, recomputed and pinned from fresh December
and February production runs (ADR-0024 — the pinned acceptance capture is a regression
artifact and never lineage). That exercises Revision Comparison, verification resolution,
Adjudication, Work Decisions (ADR-0025), and reporting without admitting 109 repetitive
rows.

Boundaries stated up front:

- The five NHHIP "matrix" documents are Utility Inventories by the glossary: nothing in
  them asserts a Resolution Strategy, so nothing in this cohort can be Critical, and the
  report's Critical Items section will exclude every rehearsal row. The rehearsal's
  reporting surface is a coordination section, not Critical Items.
- The eleven agreements (7 City, 3 METRO, 1 Harris County) are parking, signal,
  illumination, and maintenance instruments. They are not current conflict status, and
  naming the same party does not link them to a facility. Their extraction (PR #32) no
  longer survives in production lineage; re-extraction is not on the rehearsal's path.
- No NHHIP cohort has facility-linkable dated status evidence today. The due-soon and
  overdue lanes stay dark until #151's returns (or another genuine dated stream) arrive.
  The full pilot-success claim waits for that evidence plus at least one real follow-up
  cycle. If dated status must be demonstrated sooner, investigate a bounded SH 99 cohort,
  where a dated coordination stream already exists.

The honest claim the rehearsal can earn: *Corridor provides a cited, owner-organized view
of document-flagged utility conflicts and exposes uncertainty in their location data.* The
product claim — which conflicts need attention, what happens next, who owns it, when it is
due, whether it happened — remains unproven until the status evidence exists.

## Three vertical slices

**1. A trusted, runnable City cohort.** Register document identities and the RID-sourced
supersession chain from the live RID index (doc 237 today has no `registry_id`; no live
supersession edge exists). Make Active Run declaration attributable and append-preserving —
`declare_active_run` currently records no principal. Extract December and February fresh
under v3 — `make extract` is project-scoped and would run all five matrices; the rehearsal
needs document-scoped extraction — and note the spend in this record, because Extraction
Runs store neither tokens nor cost. Create the Revision Comparison pinned to those two
exact runs, derive the cohort membership deterministically, persist it as a pinned set,
and give the queue a bounded-cohort lane; today it has none.

**2. Work Decisions end to end.** Settle the semantics first — the Action Due Date is
optional, completion and cancellation append and never overwrite — then typed immutable
receipts (recording principal, timestamp, exact before/after values, predecessor decision;
the generic audit log is explicitly insufficient), queryable current values, owner/action/
due-date surfaces in the queue detail and Ledger views, and the coordination-report section
with field-exact provenance: every cell pins the exact Work Decision ids, Assertion, or
Derivation behind it, replacing the record-level citation the report reuses across
unrelated fields today.

**3. Run and measure.** Adjudicate the pinned cohort through the queue; record Work
Decisions for owner and next action on the rows that warrant them; exercise at least one
real follow-up change (a Next Action completed, reassigned, or revised against new
evidence); generate the coordination report from the tool; record every fallback to an
artifact outside Corridor; and time the work — the timed sample is an acceptance
measurement of this slice, not its own engineering issue, and it is the only sanctioned
source for any future effort estimate.

## In parallel, not gated

Submit the records requests already tracked: #151 (NHHIP recurring utility-status
artifacts) and #7 (email correspondence, both projects). Their returns are more likely
than the old agreements to supply actionability. Before returns arrive, verify #149's
boundary holds: a sequencing document entering a supported project must be refused or
visibly quarantined, never lossily ingested.
