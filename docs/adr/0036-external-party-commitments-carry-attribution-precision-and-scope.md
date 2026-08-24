---
status: accepted
---

# An External Party commitment preserves who spoke, the date as stated, and its known scope

The SH 99 date rehearsal exposed three facts that the existing event shape had
collapsed: who made a commitment, how precisely that party stated its timing,
and which Dependencies the commitment affects. That collapse creates false
project facts. Candidate 7587 turns “Air Products will be invited ... on May
8th, 2025” into an Air Products commitment even though the sentence states
what someone else will do. Candidate 7129 turns Equistar's “Due date of
01/2025” into January 1 even though the source states only a month. Candidate
7296 says Kinder Morgan's March 2026 completion timeline is unattainable and
proposes May 16th, but it does not name one of the several Kinder Morgan
Dependencies discussed in the minutes.

Decision: an External Party **Commitment is an attributable statement by that
party about what it will deliver and when**. The record keeps the stated or
committing party separately from the External Party and Dependencies affected
by the statement, even when those facts have the same value. Mentioning an
External Party, holding an event for it, or describing what the project team
will do to it is not attribution. Evidence or a recorded Verbal must establish
that the External Party made the commitment. If the speaker is unclear, the
statement remains preserved for human work but it sets no Committed Date and
cannot produce an overdue fact.

The stated party answers “who said they would do it?” The affected External
Party answers “whose work does this concern?” The first is the actor; the
second helps determine scope. Only an attributable External Party actor can
create that party's Commitment, even when another party is affected.

This sharpens ADR-0026's actor-masquerade boundary. A project-side actor still
cannot set an External Party's Committed Date; in addition, an extractor cannot
use the party mentioned in a sentence as a substitute for the actor. The event
admission policy must prove the stated party by deterministic rules or abstain.
A model may demote an uncertain attribution and may never promote one into a
Commitment.

## Timing keeps the source's precision

Every stated timing keeps the exact words and its precision. Normalization may
support display, ordering, and date rules, but it may not add precision that the
source did not state:

- An exact day remains an exact day.
- An exact-day Commitment is past due only after that calendar day has ended.
- A month remains a month. “01/2025” displays as “January 2025”; it is not
  stored or presented as a claim about January 1. A month-level Commitment is
  past due only after the last day of that month.
- An approximate expression such as “end of May” or “in about two weeks”
  remains approximate and keeps those words. It may guide coordination, but it
  does not become an exact Committed Date or support an invented overdue
  boundary. Legacy timing whose original precision cannot be established has the
  same fail-closed treatment; a normalized scalar cannot manufacture lateness.

The previous `Slip` term is replaced by **Committed Date Change**. A Committed
Date Change records the previous stated timing, the new stated timing, their
respective precision and wording, and the direction of the change. It exists
only when both timings are attributable to the same External Party and the
same stated commitment. A Need Date, Milestone, project estimate, or date
mentioned by another actor cannot supply the previous commitment. If the
timings do not establish whether the change moved earlier or later, Corridor
preserves both statements but does not invent a direction.

A later Committed Date Change creates priority work. An earlier change creates
work only when it affects a Milestone or the current Next Action. Every change's
Coordination Plan records Milestone Impact as **affects**, **does not affect**, or
**not yet known**. `Affects` links exact registered Milestones; free text does not
stand in for identity. `Not yet known` remains open work with an Internal Owner,
Next Action, and Action Due Date or structured unknown-date reason. ADR-0038 owns
those Work Decisions. Closing that coordination work does not close the External
Party's Commitment.

## One commitment, explicit scope

One Commitment may apply to one Dependency, several Dependencies, or an
External Party while its Dependency scope is not yet known. Corridor keeps one
event and explicit scope links; it does not copy the event into every possible
Dependency. A coordinator may confirm all active Dependencies for the External
Party as an explicit snapshot, select a smaller set, name one, or state that the
scope is not yet known. Suggestions may order but never make that decision.

A later scope correction appends a scope decision to the same statement. A
correction to attribution or timing, including supported facts that alter the derived
type, creates a linked successor in the same
Commitment Lineage. A statement-level Coordination Plan follows that lineage and is
not copied onto newly linked Dependencies; a material successor requires the plan
to be reviewed (ADR-0038, ADR-0039).

Unknown scope fails closed. Corridor preserves the party-level Commitment and
creates an Attention Reason to determine its scope, but it does not change any
individual Dependency, its Committed Date, or its Exceptions. This does not hide a
real missed commitment: when an attributable party-level Commitment passes the end
of its supported period without closure, a Derivation over that Commitment records
the party-level past-due fact. It is never copied into a Dependency Exception.
ADR-0040 requires every open, unknown-scope Commitment—not only those past due—in
the External Party commitments Report section. It never places the Commitment under
an invented Dependency.

The past-due fact and the current coordination task are different. The current
attributable timing, including the new timing of a Committed Date Change, governs
until verified Evidence or an attributable Verbal establishes closure. Once a
current Coordination Plan exists, the one multi-reason work item may leave the
immediate list under ADR-0035 and return when its action is due, new information
arrives, scope changes, or Milestone Impact changes. The fact remains in the Ledger
and Report. Completing an internal Next Action does not prove delivery.

A Commitment closes only when verified Evidence states completion or a Verbal
attributable to the External Party states completion. A Work Decision may record
the project's response, complete its Next Action, or establish a successor action;
it cannot close an External Party Commitment. External closure likewise does not
complete or cancel an open internal Next Action: that requires a later Work
Decision. This preserves ADR-0025's boundary between project decisions and claims
about the world.

## Concrete SH 99 cases

**Candidate 7587, Air Products, refuses as a Commitment.** Its verified quote
says, “Air Products will be invited to the TxDOT-Utility Owners-DB Proposers
Workshop on May 8th, 2025.” Air Products is the affected party, not the actor
making a promise. The quote supports neither an Air Products Committed Date nor
an overdue Air Products Commitment.

**Candidate 7129, Equistar, can become a month-precision Commitment through guided
Adjudication.** Its verified
quote says, “Equistar to provide a chain of title on the ROW agreement that is
in DOW's name (Due date of 01/2025).” It attributes the delivery to Equistar
and states January 2025, not January 1. Without a recorded closure it becomes
past due after January 31, 2025. Because the quote does not establish one
Dependency, it remains party-level until its scope is confirmed.

**Candidate 7296, Kinder Morgan, can become a Committed Date Change with unresolved
Dependency scope through guided Adjudication.** Its verified quote says, “The March 2026 completion
timeline seems unattainable. Propose extending to May 16th.” It preserves the
previous month-level timing, the proposed day-level wording, and the later
direction. Verified surrounding Evidence—not confirmation alone—must establish
Kinder Morgan as the stated party; the quote does not justify selecting one Kinder
Morgan Dependency. The coordinator chooses the affected set once, or leaves the
scope unknown.

Mechanical backfill does not guess the missing context for either positive case.
Candidates 7129 and 7296 remain pending until that guided Evidence-bound act, while
Candidate 7587 receives a mechanical Admission Abstention and creates no
Commitment.

## Relationship to existing decisions

ADR-0033 remains in force for a Verbal's append-only provenance, named recorder,
conversation date, correction history, and honest report rendering. Its amended date
semantics use the same rule: a later attributable timing is a Committed Date Change
that keeps both timings and their precision. ADR-0033 also no longer assumes that
every Verbal starts from one Dependency or contains an exact parseable promised date.
A Verbal may first be party-level or multi-Dependency, and it may preserve
approximate timing without inventing an exact Committed Date. The stated party
remains mandatory; a single-Dependency screen may prefill that party from the
Dependency, but the stored attribution is a separate fact and must be confirmed
rather than inferred from scope.
This is the attribution rule from Decision 74 narrowing Decision 19's earlier
user-interface decision: removing redundant typing does not permit Corridor to
infer who spoke.

ADR-0026 remains in force for deterministic event admission, receipts,
abstention, and demote-only model assistance; ADR-0029 remains in force where
it removes customer sign-off from that mechanical path. This ADR replaces no
admission machinery. It defines the facts that machinery must prove.

## Considered options

**Treat the affected External Party as the committing party.** Rejected.
Candidate 7587 would continue to turn an invitation sent by somebody else into
an Air Products promise.

**Normalize every timing to a calendar day.** Rejected. January 1 is materially
different from January 2025: it makes the record appear overdue up to thirty
days before the source supports that conclusion and prints a date nobody said.

**Keep `Slip` as the event name.** Rejected. It is vague and encodes only a
later movement. Committed Date Change names what changed, preserves both
statements, and supports an earlier or later direction without implying a
project response.

**Require every Commitment to resolve to one Dependency.** Rejected. Candidate
7296 does not name one, and copying a party-wide schedule statement to several
Dependencies would create duplicate events. Forcing a choice invents scope;
discarding it hides a real commitment.

**Apply an unknown-scope Commitment to all party Dependencies.** Rejected. The
party-level fact is real, but its effect on each Dependency is not. The record
must surface the former without fabricating the latter.

**Use an approximate or legacy-normalized date as an overdue boundary.** Rejected.
It would turn an ordering convenience or lost precision into a claim the source
never made.

## Consequences

The event contract must represent stated party, affected party, timing text,
timing precision, and explicit Dependency scope independently. Existing
day-normalized values must not be trusted as source precision; Candidate 7129
is the regression case that prevents January 1 from surviving as a source
claim. Committed Date projection and Exceptions become precision-aware.

The user workflow asks for attribution and scope in plain language, suggests
possible Dependencies, and permits “scope not yet known.” It lets a coordinator
record Evidence-backed corrections while preserving the original Candidate.
Corridor derives the type and direction rather than accepting an unsupported label.
The Ledger, work list, history, Attention Reasons, Exceptions, and Reports use
Committed Date Change rather than `Slip` and keep technical event codes out of
customer copy.

The #196 rehearsal uses Candidate 7296 to prove the multi-Dependency Committed
Date Change path and Candidate 7129 to prove party-level overdue and Report
behavior. Candidate 7587 is the negative attribution case. These are two
separate records because their sources do not establish that they concern the
same External Party or Dependencies.

## Interview decision map

- Decision 74 narrows Decision 19: a Verbal screen may prefill the Dependency's
  External Party, but the record still proves and stores the stated party as a
  separate fact.
- Decision 20 preserves approximate timing without invented precision.
- Decisions 55–57 require both attributable timings, their words, and their
  direction, and replace `Slip` with Committed Date Change.
- Decisions 58–59 set consequence-based priority and the project-response
  requirement for later changes.
- Decisions 68–70 allow multi-Dependency or unknown scope and make unknown
  scope fail closed.
- Decisions 71 and 75 select Candidates 7296 and 7129 as the two positive #196
  cases rather than forcing one record to prove unrelated facts.
- Decisions 73–79 establish party-level overdue work, stated-party
  attribution, month-precision behavior, closure proof, separation of the
  overdue fact from its coordination task, and party-level reporting.
- Post-foundation decisions P27-P33 and P40-P45 establish statement correction
  lineage, derived type and direction, exact-day/month/unknown timing behavior,
  current-statement authority, party-level Derivation without Dependency
  projection, work-list return triggers, and complete open-commitment reporting.
