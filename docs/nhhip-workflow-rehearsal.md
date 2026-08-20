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

**The fresh runs exist (2026-08-07).** matrix_tiered_v3 / gpt-5.6-luna:
December (run 33765) read 564 rows, 0 unverified, 26 pages all from the
text layer; February (run 33766) read 707 rows, 3 unverified — the known
City verification-blocked rows — 28 pages all text layer. Spend across
both: 413,844 input tokens (113,620 cached) / 12,608 output, on the order
of cents, consistent with ADR-0024's benchmark. Noted here because
Extraction Runs store neither tokens nor cost. The operator declared both
Active as `local:bryceharmon` on 2026-08-07: declaration 3422 names the
December run and declaration 3423 names the February run. The
[pinned capture replay](https://github.com/bharm16/corridor/issues/172#issuecomment-5220794045)
found no mechanical divergence. The later
[read-only closure audit](https://github.com/bharm16/corridor/issues/172#issuecomment-5337119239)
confirms those declarations and cohort receipt 75: 17 City members over those runs
and comparison 8071, with content digest
`abe1661b20dbf09667e68695f7e7cdf31dbc20d39ef8f53d30ce1c1514dd8c62`.
The human adjudication and follow-up rehearsal remains separate (#165).

**1. A trusted, runnable City cohort (delivered).** At decision time, document 237 had
no `registry_id`, no live supersession edge existed, Active Run declaration recorded no
principal, extraction was project-scoped, and the queue had no bounded-cohort lane. The
delivered slice registered the RID-sourced identities and supersession chain, made Active
Run declaration attributable and append-preserving, added document-scoped extraction,
and pinned the real December/February comparison and 17-member cohort described above.
Extraction Runs still store neither tokens nor cost, so the spend remains recorded in this
rehearsal document.

**2. Work Decisions end to end (delivered).** Semantics settled 2026-08-06: **completion** records the
project's judgment the action was carried out — still not proof of external fact —
and **cancellation** withdraws it as no-longer-intended; both append, both leave no current
Next Action unless a successor is recorded. **Current values** are a projection of the
latest receipt per field, recomputable from the chain; divergence is a defect caught by a
consistency check. The **Internal Owner is a named-person string** in Phase 1 — registered-
principal owners arrive with M9 — while the **recording principal meets Admission's bar**:
an attributable human, always; no system principal may record a Work Decision. The **typed
receipt is the record** (recording principal, timestamp, exact before/after values,
predecessor decision); `audit_log` carries a pointer event naming the receipt id, payload
never duplicated. **Mutation flows** through the queue detail and Ledger record forms, one
Work Decision per submit, no bulk mutation in Phase 1. **Exceptions:** `MISSING_OWNER`
redefined and `MISSING_ACTION` added as Derivations over the absence of a current Work
Decision; `ACTION_DUE_SOON`/`ACTION_OVERDUE` over the Action Due Date with per-project
thresholds, categories distinct from the document-date lanes so project-controlled dates
never masquerade as external commitments. **Field-exact provenance is the whole report's
end state** — the citation-reuse defect is a correctness defect wherever it occurs; the
coordination section ships field-exact within the rehearsal, and the legacy-section
retrofit is an explicit child of the slice-2 umbrella. On top of those semantics: the
surfaces — owner/action/due-date in queue detail and Ledger views, and the coordination-
report section pinning the exact Work Decision ids, Assertion, or Derivation behind every
cell.

**3. Run and measure.** Adjudicate the pinned cohort through the queue; record Work
Decisions for owner and next action on the rows that warrant them; exercise at least one
real follow-up change (a Next Action completed, reassigned, or revised against new
evidence); generate the coordination report from the tool; record every fallback to an
artifact outside Corridor; and time the work — the timed sample is an acceptance
measurement of this slice, not its own engineering issue, and it is the only sanctioned
source for any future effort estimate.

**The original slice-3 attempt failed (audited 2026-08-20).** The durable record
shows that all 17 receipt-75 Candidates were accepted on August 7, 2026, but no
Work Decisions followed and only one later Report exists. The timing/fallback
protocol required by #165 was not predefined before the first Adjudication, so
that sample cannot be reconstructed or passed retroactively. Issue #165 is closed
as a failed/partial historical attempt, not as completed. The supervised successor
rehearsal is #279; its first exploration stopped honestly when the consumed cohort
rendered Queue empty. Issue #280 owns the agent-ready product prerequisite: keep
the exact admitted cohort actionable for Coordination Plan work without resetting
Candidates, substituting a cohort, or rewriting history.

## Public-data follow-up, re-decided 2026-08-20

The August 20 official-source audit supersedes the earlier direction to submit #151
and #7 immediately in parallel. TxDOT now publishes six NHHIP status summaries and a
larger utility RID bundle than the local corpus represents, while FDOT publishes
additional utility schedule and plan artifacts for the held-out project. Run the
pinned rehearsal without changing its accepted cohort; afterward register and ingest
the newly public material, then re-evaluate and narrow #151 against the remaining
utility-owner status, coordination-log, and dated-clearance gap. The bounded search
still found no public DOT-to-utility correspondence for either project, so #7 remains
a likely separate human request rather than part of this rehearsal.

Before any returns or new public intake enter a supported project, verify #149's
boundary holds: a sequencing document must be refused or visibly quarantined, never
lossily ingested.
