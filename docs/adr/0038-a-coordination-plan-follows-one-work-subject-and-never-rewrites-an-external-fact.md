---
status: accepted
---

# A Coordination Plan follows one work subject and never rewrites an external fact

> **Terminology amended 2026-08-27 by [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Current prose uses the adopted construction terms. Historical quotations and implementation identifiers retain their original spelling; the authority boundaries are unchanged.

ADR-0025 introduced the Work Decision so assignment, Next Action, and Action Due
Date would not masquerade as Supporting Documentation. It assumed every Work
Decision belonged to a Constraint. ADR-0036 then established that an attributable External
Party Commitment or Committed Date Change may be real while its Constraint scope
is still unknown. Requiring a Constraint before the project can respond would now
either invent scope, discard useful coordination work, or copy one statement and
its plan onto several records.

Decision: **each Work Decision has exactly one work subject: one Constraint or one
accepted External Party Commitment or Committed Date Change, never both.** A pending
Candidate, refused attribution, Completion Reported statement, or record marked Not
relevant cannot own a Coordination Plan.

This broadens ADR-0025's Constraint-only subject boundary. It does not weaken the
more important boundary that ADR established: a Work Decision records what the
project decided to do and can never prove what an External Party said or did.

## One plan in the interface, separate decisions in the record

A **Coordination Plan** is the current internal response for exactly one work
subject. The coordinator edits it as one form, but Corridor records separate,
append-only Work Decision chains for:

- Assigned To;
- Next Action and its Action Due Date, or a structured reason the date is not yet
  known; and
- for every Committed Date Change, Effect on Milestone.

One Save commits the accepted External Party statement, its scope decision, and
the plan's Work Decisions atomically when they are made in the same guided flow.
The statement and each Work Decision retain distinct identities, provenance, and
history. A stale concurrent Save refuses the whole operation and shows the newer
state; it never overwrites or partly applies the user's work.

Effect on Milestone has three explicit states: **affects**, **does not affect**, or
**not yet known**. `Affects` links one or more exact registered Milestones; free
text cannot stand in for those links. `Not yet known` is unresolved work, not a
way to complete the item. It therefore keeps a current assigned person and Next
Action with an Action Due Date or the structured reason that date is not yet
known.

## The plan follows the statement lineage, not its scope links

A statement-level Coordination Plan remains on that statement's lineage when a
later scope decision links one, several, or all currently active party
Constraints. Corridor does not copy the plan to those Constraints. A linked
Constraint may display the statement and its plan as related context, but
Constraint-specific follow-up exists only when a person records a new Work
Decision whose subject is that Constraint.

Correcting only statement scope appends a new scope decision to the same statement.
Correcting attribution or timing, including supported facts that change the derived
type, creates a linked successor statement because it changes the External Party
fact itself. The plan stays with
the statement lineage, but a material successor marks the plan for human review;
Corridor cannot silently declare that an old response still fits a changed fact.

## Internal work never closes an External Party fact

Supporting Documentation or an attributable Verbal may establish Completion Reported
for one exact External Party Commitment. That report of completion does not
automatically complete or cancel the project's current Next Action. Conversely,
completing or cancelling the Next Action never establishes Completion Reported for
the Commitment or removes a past-due fact.

Completing or cancelling an action on any open Coordination Subject—a Constraint,
Commitment, or Committed Date Change—requires either a successor action or a
structured reason that no immediate follow-up is needed. Cancellation also requires
a structured cancellation reason; both decisions may carry an optional note. A
no-follow-up decision removes only the immediate work. An open or past-due External
Party fact remains in the Project Record and Report, and returns to the work list
when new Supporting Documentation or a Verbal arrives, its stated timing changes,
its scope changes, its Effect on Milestone changes, or another explicit return
condition becomes due.

ADR-0039 owns the atomic Save bound to Supporting Documentation, Candidate
disposition, correction, Undo, and dependent-lineage refusal rules for the guided
statement flow. ADR-0035
owns its plain-language interaction and work-list presentation. This ADR owns only
the resulting Coordination Subject and plan semantics.

## Considered options

**Require every plan to belong to a Constraint.** Rejected. It forces the user to
invent scope before responding to a real party-level Commitment and makes the SH 99
7296 and 7129 cases dishonest.

**Copy a party-level plan to every linked Constraint.** Rejected. One project
decision would become several records with ambiguous correction and completion
semantics. Scope links describe where the External Party statement applies; they do
not prove that the project chose the same action for every Constraint.

**Store one mutable plan record.** Rejected. It would collapse assignment, action, due
date, and Effect on Milestone decisions into one overwriteable blob and make targeted
correction or attribution impossible.

## Consequences

The Work Decision model and its readers need a discriminated subject rather than a
required Constraint alone. The three plan fields keep independent predecessor
chains and current projections. Statement admission, scope, plan changes, and Undo
need one transaction boundary with stale-write detection and dependent-lineage
guards. Reports and work lists read the subject explicitly and never infer a
Constraint from party identity.

Production authentication, notification delivery, and construction-worker write
permissions remain outside #196. A seeded coordinator may exercise assignment in
the internal rehearsal; the assignment takes effect, but the rehearsal does not
claim that notification or production authorization has been proved.

## Post-foundation decision map

This ADR records the 2026-08-12 post-#217 decisions P7-P23 and P34-P35:
statement-level plan ownership; plan continuity without Constraint copying; exactly
one Work Decision subject; statement eligibility; separate reports of external
completion and internal action; required Effect on Milestone; separate field chains; stale-write
refusal; successor or no-follow-up handling; one multi-reason subject; roster-backed
ownership; and structured due-date and cancellation reasons. ADR-0035 owns the
interaction contract, ADR-0036 owns the External Party facts, and ADR-0039 owns the
atomic guided act and its reversal.
