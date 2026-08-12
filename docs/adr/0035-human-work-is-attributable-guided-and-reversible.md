# Human work is attributable, guided, and reversible

The first working screens exposed the shape of the database to the person doing
the project work. A coordinator had to type an Internal Owner, save a Next
Action in a second form, know a `DEP-` reference to place a statement, and see
Candidate, Adjudication, and Exception codes. Other controls required terminal
commands and internal ids. That is not a workable contract for construction
workers or project coordinators. ADR-0034 separates customer work from
Corridor operations. This decision defines the customer side: **every human
act is attributed to an individual, presented in a guided project interaction,
and reversible through a later recorded act rather than by editing history.**
One interaction may contain several distinct domain acts; the interface must not
collapse their identities merely because one Save commits them atomically.

## Identity and access

Each person uses an individual account. Shared project accounts are not
supported. Corridor gets the recording principal from the signed-in session;
the user never types a principal name. The first customer release uses a
passwordless email magic link. Company single sign-on may be added for an
enterprise deployment. An internal rehearsal may use one seeded coordinator
identity, but that shortcut does not validate sign-in and is not permitted for
customer use.

This ADR does not decide what a construction worker may change in the Ledger.
That permission boundary is deliberately deferred. Until it is decided, no
implementation may infer that workers have coordinator permissions or that
they are read-only. The #196 rehearsal exercises only the project-coordinator
workflow.

## One work list, in project language

A project coordinator opens Corridor to a short work list, not a queue of
records and not the full Ledger. It contains work that needs attention now and
states the action in plain language, such as "Choose which date is correct" or
"Assign the person responsible." The complete Ledger and the lower-priority
backlog remain searchable.

The list orders work by project consequence:

1. past-due External Party commitments;
2. Critical Dependencies without an Internal Owner or Next Action;
3. Committed Date Changes and disputed dates;
4. statements that Corridor could not place; and
5. lower-risk cleanup.

ADR-0036 defines the commitment, date, and scope rules behind the first and
third groups. This ADR decides only how the resulting human work is presented.
A single statement appears once even when several Attention Reasons apply. Its
highest-consequence reason controls its position, and the card names every current
reason rather than hiding or duplicating them.
A Dispute interrupts current work only when it affects a current date, Ready,
a Report, or the Next Action. Other Disputes stay visible. If the coordinator
cannot determine the correct conclusion, they choose **Needs clarification**,
assign an Internal Owner and Next Action, and leave the Dispute open. Corridor
never requires a false Settlement.

Candidate and Adjudication remain internal domain and implementation terms.
The user sees actions such as **Add this conflict**, **Correct the extracted
information**, **Link to an existing conflict**, and **Not relevant**. Internal
Exception codes are also hidden. The UI says what happened: for example, "The
Committed Date passed, and no closure is recorded." Receipt ids, policy
digests, and other technical details stay behind an optional audit view. A
plain history shows what changed, who acted, when, why, and which Document or
Verbal supports it.

Corridor verifies ordinary citations mechanically. A person checks a citation
only when verification failed or the proposed conclusion changes a critical
date, Ready, or an external Report proposed for release. When a person must
decide, the cited page appears beside the question. Missing stated-party or timing
context must be supplied by verified Evidence, possibly another verified quote;
the coordinator's confirmation by itself is not factual support.

## A Coordination Plan is one guided Save

The coordinator creates or changes a **Coordination Plan** with one form and
one Save action:

- Internal Owner, selected from a searchable project-team roster;
- Next Action; and
- Action Due Date, or a structured reason that the date is not known, with an
  optional note.

Free-text owner names are not accepted. If a person is absent from the roster,
the coordinator invites them or requests that they be added. Assignment takes
effect at once and notifies the Internal Owner. The recipient may flag a wrong
assignment, but a notification that has not been accepted cannot leave the
Coordination Subject ownerless. Notification delivery and production identity are
deferred from #196; its seeded coordinator proves neither. Behind the form,
Corridor preserves the distinct append-only Work Decisions and exactly-one-subject
rule in ADR-0025 and ADR-0038.

Completing a Next Action is one click with an optional note. Cancelling one
requires a structured reason: no longer needed, replaced by another action,
subject changed, or entered by mistake. An optional note may explain more. After
an action is completed or cancelled on an open Dependency, External Party
Commitment, or Committed Date Change, the subject must receive a successor Next
Action or a structured reason that no immediate follow-up is needed. No follow-up
changes only the project's immediate work; it does not close or hide an External
Party fact.

## Guided and reversible judgment

The Unplaced Statement workflow never asks a coordinator to type a Dependency
identifier. It shows suggested matches and search results using the External
Party, Stationing, facility details, and current Internal Owner. Suggestions order
but never select scope. The general control supports one Dependency, a selected
set, all currently active Dependencies for the party as an explicit snapshot, and
scope not yet known. The cited page stays beside the statement. The coordinator may
correct Evidence-backed attribution or timing facts; Corridor derives Commitment
versus Committed Date Change and its direction from those supported timings.
Corridor preserves the original Candidate and records the correction. **Not
relevant** requires a reason, creates no statement or plan, and remains reversible.
ADR-0036 owns the External Party facts and ADR-0039 owns the atomic Save.

A statement decision has immediate **Undo** and a later **Correct** action in
history. Undo reverses every result of that guided Save and returns the Candidate
to work, unless a later act depends on one of those results; it never deletes or
cascades through later work. Correct can then target one fact. The same rule applies
across the product. Users do not directly edit or delete earlier Ledger history.
They use Correct, Replace, Restore, or Cancel, and Corridor records the new act
beside the old one (ADR-0039).

Dismissal is therefore not a one-click removal. The coordinator selects a
reason, reads a short statement of the consequence, and confirms. The
Dependency stays in history and can be restored. A Settlement is likewise
made beside the competing Assertions, and a later correction does not erase
what either Document said.

## Device, interruption, and continuity boundaries

A phone supports reading urgent work, recording a Verbal, completing a Next
Action, and receiving notifications. Comparing Documents, settling a Dispute,
judging Ready, and releasing a Report require a tablet or computer. Corridor
does not compress a high-consequence document comparison into a phone flow.

An offline device may keep a local draft. It may not change the Ledger until
the server confirms the write. The screen labels unsynchronized work
**Draft - not yet saved**.

Only four kinds of event interrupt a user:

- a new Internal Owner assignment;
- a soon-due or past-due Next Action;
- loss of Ready; or
- a new Document change that affects a current commitment or Critical
  Dependency.

The first release uses in-app and email notifications, not text messages.
Everything else stays in the work list or a daily summary. A coordinator may
defer work with a reason: waiting for information, waiting for an External
Party, or assigned to someone else. Every deferral has a return date. An
unknown Action Due Date is not itself a deferral: without a return date the work
stays immediate. Deferral does not hide the record or its Attention Reasons; the
item returns to immediate work when the date arrives, the action is due, new
Evidence or a Verbal arrives, statement timing or scope changes, or Milestone
impact changes. Each
project has one named escalation contact. Urgent overdue work notifies that
contact and the Internal Owner, not the whole project team.

## Considered options

**Expose the current forms and train the customer.** Rejected. Training cannot
make terminal commands, database identifiers, free-text identities, and
internal record types into project work. Technical operations belong behind
the managed-service boundary in ADR-0034.

**Use one shared account per project.** Rejected. It makes a Work Decision,
Settlement, Dismissal, or correction unattributable and prevents useful
assignment notifications.

**Put every unresolved record in one review queue.** Rejected. It makes low
consequence cleanup compete with missed commitments and ownerless Critical
Dependencies. The Ledger stays complete; the work list is selective.

**Allow ordinary edit and delete controls.** Rejected. They make a correction
easy by making the record untrustworthy. Recorded correction is both usable
and auditable when the product supplies the correct verb.

**Make every workflow fully functional on a phone and offline.** Rejected.
Small screens are unsafe for document comparison, and an unconfirmed offline
write would make the device claim a Ledger state the server does not have.

## Consequences

The current web UI does not satisfy this ADR. The customer release needs
session identity, project membership and roster search, a prioritized work
list, the combined Coordination Plan, structured cancellation and deferral,
recorded correction and restoration, notifications, and plain-language
history. Existing append-only receipts remain the record beneath those
workflows.

The internal #196 rehearsal may use a seeded coordinator identity and begins
after entry to the product. The simulated coordinator must complete the guided
workflow in the UI. Any coordinator-facing terminal command, database lookup,
or manual data repair is a rehearsal failure. This produces provisional
workflow timing only; it is not target-user validation. The concrete case,
timing boundary, and acceptance checks remain in
`docs/sh99-date-rehearsal.md`.

Construction-worker Ledger permissions remain an explicit open decision. Corridor
must resolve that permission shape before granting or denying their write authority;
this ADR does not decide whether they are read-only or which records they may see.
The open question does not block the coordinator-only #196 rehearsal.

## Interview decision map

This ADR records decisions 5-7, 10-18, 21-26, 30, 32-33, 37-42, 47-48,
50-51, 54, 60, 62, and 64 from the original product-workflow interview. It also
records the interaction portions of the 2026-08-12 post-foundation decisions
P9-P12, P20-P24, P28-P35, P43, and P54. Decisions whose
domain meaning belongs to commitments, Ready, Reports, or managed operations
are recorded in ADR-0036, ADR-0037, ADR-0038, ADR-0039, ADR-0040, or ADR-0034
respectively; #196 records the rehearsal case and timing contract.
