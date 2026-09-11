# Which decision governs a Follow-up Plan: the ADR-0038 and ADR-0085 crosswalk

Two accepted decisions both describe a Follow-up Plan, and a reader who meets
either one first can reasonably conclude it governs the other's runtime path.
[ADR-0038](adr/0038-a-coordination-plan-follows-one-work-subject-and-never-rewrites-an-external-fact.md)
is indexed with `scope: optional module`, and the customer-journey audit of
2026-09-10 asked whether that status should change now that the adopted-project
packet workflow records follow-up too. The maintainer declined to flip the
status from the classification alone and asked (#845, decision 8) for this
crosswalk first: which semantics apply to which runtime path, and whether the
two are genuinely inconsistent.

**Conclusion: they are not.** The two decisions govern two different runtime
paths and two different records, a project is on exactly one of those paths at
a time, and ADR-0038's `optional module` scope is the correct value under the
lifecycle rules in [docs/adr/README.md](adr/README.md). No ADR metadata
changes, and no successor ADR is required. Two observations that fall out of
the analysis are recorded at the end without being decided here.

## The path a project is on is derived, not chosen

`src/corridor/operating_mode.py` (#520) derives one of two modes from a single
immutable baseline-adoption receipt: a project holding one is in
`adopted_baseline` mode, and a project without one is `legacy`. The transition
runs once, in the same transaction as the baseline it adopts, and PostgreSQL
refuses every update, delete and truncate of the receipt, so the mode is
one-way by construction rather than a column somebody can set back.

Every project is therefore on exactly one path at any time, and the question
"which Follow-up Plan semantics apply here" has exactly one answer for it.

## What each decision governs

| | ADR-0038 | ADR-0085 (with ADR-0084) |
|---|---|---|
| **Runtime path** | a legacy project — no adopted baseline | an adopted-baseline project (#520) |
| **What raises the plan** | the guided statement flow: an accepted External Organization Commitment or Change to Promised Timing, or a Constraint (`statement_coordination.py`) | **Needs coordination**, one of ADR-0085's four primary packet decisions, recorded inside the packet's one atomic Project Record revision (`review_packets.py`, `packet_review.py`, #526) |
| **The subject** | exactly one work subject: one Constraint or one accepted Commitment Lineage, never both | one Proposed Delta, named by its target subject identity and target field |
| **Where it is retained** | `work_decisions`, grouped by `follow_up_plan_receipts`, with `follow_up_plan_reversals` (`work_decisions.py`) | `delta_follow_up_plans`, with `delta_follow_up_plan_evidence` (`models/delta.py`) |
| **What the record holds** | three independent append-only decision chains: Assigned To; Next Action and its Action Due Date, or a structured reason the date is not yet known; and Effect on Key Dates for every Change to Promised Timing | the exact open question, the person **or** organization who owes the answer, the date the question returns, the scope it affects, and the effective Support Assessments that raised it |
| **What it does to the source fact** | nothing: completing or cancelling the Next Action never establishes Completion Reported and never removes a past-due fact | nothing: the plan leaves the proposed value unaccepted and the Proposed Delta open, and writes no `delta_dispositions` row |

The models say the same thing about themselves. `work_decisions.py` opens:
"Attributable project decisions for one Coordination Subject (ADR-0038) … A
Coordination Subject is exactly one Dependency or accepted Commitment Lineage."
`DeltaFollowUpPlan` in `models/delta.py` closes its docstring: "This is the
spine's Follow-up Plan. `follow_up_plan_receipts` is the frozen legacy grouping
receipt over `work_decisions` (ADR-0081) and is not extended for
adopted-baseline projects (ADR-0084 §3)."

These are two records with different shapes, not one record two ADRs disagree
about. The legacy plan carries an internal assignee and a next action; the
spine's plan deliberately carries neither, because — as
`native_follow_up_reading.py` records — "the coordinator never recorded
either", and composing customer prose from an absent record is exactly what a
report may not do.

## What ADR-0038 governs on both paths

One ADR-0038 boundary is not path-specific, and it is the more important half
of that decision: **a project's own coordination decision can never prove what
an External Organization said or did.** Internal work never closes an external
fact, and closing an external fact never completes the internal action.

ADR-0084 §1 states the same boundary for the adopted path in its own terms: for
Utility Owner, attribution, Promised For, completion, Applies To, agreement or
permit status and similar external facts, unsupported free text produces an
open Work Item and never an accepted value. The rule survives the change of
path; the record that implements it does not.

## What the separation actually rests on

Worth stating precisely, because it is weaker than a database refusal and a
reader should not assume otherwise. The operating-mode triggers guard
`dependencies` and `dependency_events` — `OPERATING_MODE_GUARDED_TABLES` in
`src/corridor/migrations/source_append_commands/operating_mode.py` — so an
adopted project cannot gain a legacy accepted value. `work_decisions` itself
carries no operating-mode trigger and `work_decisions.py` performs no mode
check.

The separation therefore rests on the **subject**, not on the writer: an
ADR-0038 plan hangs off a Constraint or an accepted Commitment Lineage, an
adopted project's accepted values live on the spine, and ADR-0084 §3 says an
adopted-baseline project's surfaces have no mandatory legacy dual-write and no
legacy reader is extended to show them. ADR-0081 then freezes the table family
against new capability, which `CLAUDE.md` repeats by name: no new feature may
be implemented solely against `dependencies`, `dependency_events`,
`work_decisions`, `operative_support`, or the dispute tables.

## Why ADR-0038's index scope is already correct

[docs/adr/README.md](adr/README.md) defines the values: "`scope`: `current
product`, `optional module`, `historical`, or `future` … An accepted ADR whose
expansion is frozen (ADR-0075) is `optional module`."

ADR-0038's model is exactly that. It is accepted and still governs every legacy
project; its tables are frozen against new capability by ADR-0081; and it is
neither superseded nor historical, because legacy projects still run on it
until ADR-0081 stage 6 completes. `optional module` is the value the lifecycle
rules prescribe for that state, so there is nothing to correct, and a status
flip would misdescribe a decision that is still in force.

Nor is ADR-0085 in conflict with it. ADR-0085 amends **ADR-0035's** Work List
presentation for adopted-baseline projects and says so in its frontmatter; it
never claims ADR-0038's subject semantics, and ADR-0038 never claims the
packet path. The `amends` relation between them is absent because there is no
clause to move.

## Two observations, recorded and not decided

Both fall out of the crosswalk, and both are for the maintainer rather than for
this note.

1. **ADR-0085's sentence for Needs coordination and the retained record
   differ.** ADR-0085 writes that the coordinator "cannot settle it yet, so it
   becomes an open Work Item with an assigned person and a Next Action". The
   record #526 actually writes, `delta_follow_up_plans`, holds an open
   question, the party who owes the answer, a return date and the affected
   scope — no internal assignee and no next-action sentence. Whether that is a
   wording correction to ADR-0085 or a real gap in the adopted path is an
   ADR-0085 question, not an ADR-0038 one, and it is not settled here.
2. **The Project Record glossary's Follow-up Plan entry describes the legacy
   shape only** — "Assigned To, Next Action, Action Due Date, and any required
   Effect on Key Dates decision" — which is ADR-0038's record and not the
   spine's. Whether the entry widens, splits, or stays as the legacy sense is
   terminology work under
   [the terminology research procedure](agents/domain.md#research-before-proposing-terminology),
   and no label is proposed here.
