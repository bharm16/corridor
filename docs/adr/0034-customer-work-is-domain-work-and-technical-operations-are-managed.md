---
status: accepted
---

# Customer work is domain work, and technical operations are managed

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Corridor's first users are construction workers and project coordinators. The
current operating path asks a person to understand an Current Production Run id, inspect an Automatic Support Update Rules digest, and use terminal commands before the product can do
ordinary work. Those are system-maintenance choices, not project decisions. A
customer cannot give meaningful authorization to rules they cannot inspect, and a
project coordinator should not have to understand extraction lineage to use the
Constraint Log.

Decision: **customer users do domain work; Corridor manages technical operations.**
A customer-facing workflow never requires a command line, a database query, an
internal id, an Current Production Run choice, a policy digest, or knowledge of Automatic Support Update. Customer screens name the project question: choose the assigned
person, record a Next Action, say what an External Organization stated, resolve a Source Discrepancy,
or judge whether Supporting Documentation meets the stated requirement. Technical
failures become work for Corridor operations, not disguised work for the customer.

Corridor operations owns first-release project setup. It creates the project,
registers its Documents and External Organizations, connects the agreed document
location, and loads the project team. The customer confirms that setup; the
customer does not configure extraction rules or safety policies. After setup,
Corridor monitors the connected location and processes new revisions. Manual upload
is a fallback for a Document received elsewhere. Supersession still requires
Supporting Documentation that establishes the replacement; neither automation nor
an operator may infer it from a filename or date.

When an incoming Document uses an unfamiliar External Organization name, the coordinator
confirms a suggested existing party or creates a new one; Corridor records a
confirmed alias for later Documents. That is a domain identity judgment, not a
technical ingestion task. First-release accounts belong only to the project team.
External Organizations do not receive Corridor accounts; their statements enter through
Documents and attributable Recorded Verbal Statements.

## Automatic Support Update is normal fail-closed processing

Automatic Support Update no longer requires project authorization. When Document Revision Processing proves an exact, unique, mechanically verified, unchanged successor row,
Corridor moves only the Supporting Documentation in Use roles that already carry a human judgment.
It does not perform Record Inclusion, change a Constraint's conclusion, or originate
a Documentation Review. Changed, ambiguous, incomplete, or unverifiable input
produces Abstention and leaves the Ledger unchanged. A successful transfer appears in plain project history
and does not interrupt a user. A material change creates a plain-language task about
what changed; the user never sees a Human Support Update lane.

This decision **supersedes ADR-0022's project-authorization requirement**, including
its rule that the absence of an active authorization sends exact unchanged rows to
Human Support Update. ADR-0022's eligibility proof, separation of roles,
fail-closed behavior, immutable Support Update Run Record and outcome receipts, and ban on
judging that a documentation requirement is met remain in force. Rule versions and
source digests remain in the receipts for replay and audit. Changes to those rules pass through Corridor's
engineering and release controls; asking a project coordinator to sign a digest is
not a safety control.

## Current Production Run selection is automatic but never means latest

Normal Document processing declares its single successful production Extraction Run
as the Current Production Run. The declaration remains explicit, attributable in run lineage,
and replayable; automatic does not mean inferred from the greatest id or newest
timestamp. A test, evaluation, acceptance replay, experiment, or unrelated backfill
must carry lineage that makes it ineligible for automatic production selection. It
can never replace the Current Production Run.

If production processing does not yield one safe choice, Corridor selects nothing
new. If a prior valid Current Production Run exists, it remains declared; if none exists, the
Document remains without one. Corridor operations receives the technical detail and
resolves it. The customer sees only the consequence in project language, such as
"A newer document needs attention. Some supporting documents are no longer current."
The existing Ledger and Coordination Decisions remain usable. Provenance still fails closed: a
superseded Document cannot regain actionable Extracted Proposals. A documentation requirement
is no longer treated as met when all applicable support cites superseded Documents.
This refines ADR-0019's explicit-declaration rule; it does not permit an implicit latest-run rule or weaken
its successor fail-closed behavior. Acceptance captures remain outside production
lineage under ADR-0024.

## Internal operations may remain technical

Trained Corridor operators may use CLI tools in the first release. An operation that
affects the Ledger or its operative lineage writes an attributable, immutable
receipt. Its project consequence appears in plain history, while run ids, digests,
and other technical detail stay behind an audit view. Internal tools are not a
customer workflow merely because Corridor can use them to investigate or recover
from a failure.

The customer identity boundary is separate: every human act uses the person's
individual identity, captured by the product rather than typed into a form. The
first customer release uses email magic-link sign-in; enterprise SSO may follow.
The internal #196 rehearsal may use one seeded coordinator identity, but that is a
rehearsal shortcut, not the production sign-in contract.

## Safe failure behavior

A processing failure does not lock the Ledger or erase coordination state. Corridor
operations receives an actionable alert. Affected users receive a plain consequence
only when current project work is affected. Exact Automatic Support Update succeeds quietly in
history; changed Supporting Documentation, a documentation requirement no longer
met, or a current commitment affected by a new Document becomes prioritized domain
work. The system never guesses, silently
publishes stale support, or asks a customer to repair lineage.

## #196 sequencing

This ADR records the target operating contract; it does not put the Current Production Run and
Automatic Support Update redesign on #196's critical path. #196 is the first narrow coordinator
slice. Before feature work, the later local statement-publication hardening commit
and this corrected domain contract are each transplanted or applied to fresh current
`main`, independently reviewed, and delivered as separate changes. The divergent
working branch is never merged wholesale.

Corridor operations then rehearses the SH 99 mechanical Record Inclusion backfill against
an isolated clone of a pinned current database and current source. The receipt audit
compares exact before and after Ledger rows, Extracted Proposal states, Policy Runs, outcomes,
and reason codes; it never substitutes guessed totals. Candidate 7587 abstains, and
Candidates 7129 and 7296 remain pending for guided Human Record Decision bound to Supporting
Documentation. A second run may append an immutable run receipt but creates no duplicate Ledger fact.
Only after that proof and a separate explicit approval may operations mutate the
shared SH 99 database.

Setup and operations time are measured separately. The simulated coordinator then
works only through the product interface with a seeded individual identity. A
customer-facing CLI command, database query, or manual data repair fails the
rehearsal. The result is an internal workflow measurement with provisional timing,
not target-user validation. Magic-link sign-in, notification delivery, connected
ingestion, production roles, construction-worker permissions, and the operations
redesign must exist before customer use but may follow the #196 slice. Legacy
Extracted Proposals remain immutable; changing the minutes prompt or extractor is a separate
measured issue rather than a prerequisite that rewrites the rehearsal input.

## Considered options

**Teach coordinators the CLI or build them an administrative screen.** Rejected.
Changing the shape of the same technical ceremony does not turn run lineage or a
policy digest into project work.

**Keep customer authorization for Automatic Support Update.** Rejected. Exact
unchanged Supporting Documentation adds no new judgment, and a signature from
someone who cannot evaluate the rules creates false assurance. The eligibility proof, Abstention, and
receipts are the safety controls.

**Select the latest completed Extraction Run.** Rejected. An experiment, evaluation,
or backfill could silently change the Ledger. Production purpose and explicit
lineage, not time or id order, determine eligibility.

**Block the entire Ledger while technical processing needs attention.** Rejected.
Existing Constraints and Coordination Decisions remain useful. Only conclusions that need
current Supporting Documentation, including conclusions from Documentation Review,
must fail closed.

## Consequences

The implementation must distinguish production Extraction Runs from experiments,
evaluations, acceptance replays, and other non-production work before automatic
Current Production Run selection ships. Automatic Support Update must stop treating a project
PolicyApproval as execution authority while retaining versioned policy and outcome
receipts. Corridor needs operations alerting and a runbook for ambiguous or failed
processing. Customer documentation and screens must remove CLI instructions,
internal ids, Current Production Run selection, Automatic Support Update authorization, and Human Support Update
as customer tasks.

Managed setup and connected ingestion create service work for Corridor in the first
release. That is intentional: the company owns technical complexity until a safe,
plain self-service workflow exists. The permission boundary for construction workers
remains explicitly deferred; this ADR grants them no Ledger-write authority.

## Historical interview decision inventory

Terminology in the interview inventories below is preserved as recorded. Read the current decisions with the vocabulary in [ADR-0047](0047-domain-language-follows-researched-construction-practice.md); these historical uses of Ready, Dependency, and Evidence are not current product labels.


The inventories below are historical loss-prevention records. The normative current decisions live in the focused ADR sections above and ADR-0035 through ADR-0045; issue numbers, branch names, hashes, and delivery order below are not product architecture.

This appendix is the loss-prevention ledger for the 82 numbered decisions settled in
the product interview. Later decisions control where the interview corrected an
earlier provisional choice.

1. Customer users never see Active Run or Carry-Forward machinery, technical ids,
   policy digests, or CLI controls. Those are internal Corridor operations.
2. Corridor operations resolves technical processing exceptions as a managed
   service. The project coordinator receives only a plain consequence when one
   affects project work.
3. Automatic Carry-Forward is normal fail-closed behavior and needs no customer or
   project authorization.
4. Normal production ingestion selects its safe Active Run automatically. Test,
   evaluation, backfill, and experimental runs can never replace it; ambiguity keeps
   the prior safe state and alerts Corridor operations.
5. Each human uses an individual identity. Corridor records it automatically on
   every human act; users never type a principal name and do not share accounts.
6. The first customer release uses email magic-link sign-in. Enterprise SSO may be
   added later.
7. **Deferred:** whether construction workers may change the Ledger, and how their
   permissions differ from project coordinators. No ADR may infer the answer. A
   later proposal to make workers read-only was not accepted.
8. Successful Automatic Carry-Forward is visible in history but does not interrupt
   the user. Only an Abstention with a project consequence becomes work.
9. The Ledger remains usable when a new Document cannot be processed safely.
   Corridor operations receives the technical alert; affected users see that some
   Evidence is not current; Ready lapses when it depends only on superseded Evidence.
10. A coordinator opens Corridor to a short, prioritized work list rather than the
    full Ledger. The full Ledger remains searchable.
11. The home list shows work that needs attention now, ordered first by overdue
    External Party commitments, then critical Dependencies without an Internal
    Owner or Next Action, new date changes or disputed dates, Unplaced Statements,
    and finally lower-risk cleanup.
12. Internal Owner, Next Action, and Action Due Date use one Coordination plan form
    and one Save action, even if separate Work Decision receipts are written.
13. Internal Owner is selected from a searchable project-team list, not entered as
    free text. A missing person is invited or requested for addition.
14. An Internal Owner assignment takes effect immediately and notifies the person.
    Acceptance is not required; the person can flag an incorrect assignment.
15. Every Next Action has an Action Due Date or an explicit reason that the date is
    not known. A silent blank is not allowed.
16. Completing a Next Action is one click with an optional note. An explanation is
    not required.
17. Cancelling a Next Action requires a simple stated reason, with an optional note.
18. When an open Dependency's Next Action completes or is cancelled, the coordinator
    records its successor action or explicitly says why no follow-up is needed.
19. The interview first chose to prefill a Dependency Verbal with the Dependency's
    External Party so the coordinator would not retype it. Decision 74 later narrows
    this: the stored stated party remains a separate fact that must be supported and
    confirmed rather than inferred from Dependency scope.
20. Approximate dates such as "end of May" remain as stated. Corridor may show an
    approximation but must not invent an exact Committed Date.
21. A mistaken Verbal is corrected through a linked correction. The original remains
    in history; users do not delete or overwrite it.
22. An Unplaced Statement uses suggested matches and a searchable Dependency picker
    showing human-readable context. The coordinator never types a `DEP-` code.
23. While linking an Unplaced Statement, the coordinator may correct a missing or
    misread date, date precision, and statement type beside the cited page. Corridor
    preserves the original Candidate and the human correction. Post-foundation
    Decision P31 narrows the type portion: the coordinator corrects supported facts,
    and Corridor derives the statement type and direction.
24. A mistaken statement link or toss is reversible through immediate Undo and a
    later Correct action. The reversal is recorded rather than deleting history.
25. A Dispute interrupts current work only when it affects a current date, Ready, a
    Report, or the Next Action. Other Disputes remain visible without blocking work.
26. If the coordinator cannot settle a Dispute, "Needs clarification" leaves it open
    and records an Internal Owner and Next Action. Corridor never forces a false
    conclusion.
27. Ready is presented as a guided question against the named requirement and cited
    Evidence: Yes, Not yet, or Needs clarification. Users do not see a technical
    "mark Evidence as closing" toggle.
28. One designated project person may make the Ready judgment by default. A project
    may require two-person review later when its contract requires it.
29. Loss of Ready is never silent. It creates high-priority work and notifies the
    Internal Owner and the person who made the prior Ready judgment, showing the
    requirement and newer Evidence.
30. Dismissal requires a reason, a short consequence message, and confirmation. The
    Dependency remains in history and can be restored.
31. Reconfirmation is not a customer-facing lane or button. Exact unchanged support
    moves automatically; a meaningful change creates a plain task that compares old
    and new statements and asks only the affected question.
32. Candidate and Adjudication remain internal domain terms. Customer actions use
    plain verbs: add this conflict, correct the extracted information, link to an
    existing conflict, or not relevant, with the cited Document beside them.
33. Users do not check every citation. Corridor verifies citations mechanically; a
    person checks only a failed verification or information that would change a
    critical date, Ready, or a published Report.
34. A designated project person reviews and releases anything sent outside the
    project team. External Report publication is a real human authorization.
35. Internal Reports update automatically from the Ledger, remain marked internal,
    and require no approval.
36. The first release creates a fixed approved Report export with Evidence and
    release history. The project team sends it through its existing email or
    document-control system; Corridor does not claim delivery.
37. Phones support reading urgent work, recording a Verbal, completing an action,
    and notifications. Document comparison, Dispute settlement, Ready judgment, and
    external Report approval require a tablet or computer.
38. Offline work may be saved as a visible local draft, but the Ledger changes only
    after server confirmation. Until then the screen says "Draft—not yet saved."
39. Immediate notifications are limited to a new Internal Owner assignment, an
    overdue or soon-due Next Action, loss of Ready, and a new Document change that
    affects a current commitment or critical Dependency. Other information goes to
    the daily summary or work list.
40. The first release uses in-app and email notifications, not text messages.
41. Dependency history is a plain timeline of what changed, who changed it, when,
    why, and which Document or Verbal supports it. Receipt ids, policy digests, and
    technical detail stay behind an optional audit view.
42. Users never directly edit or delete earlier Ledger history. They use Correct,
    Replace, Restore, or Cancel, and Corridor preserves the original act.
43. Corridor operations configures a new project in the first release: project,
    Documents, External Parties, connected source, and project-team list. The
    customer confirms setup but does not configure extraction rules or policies.
44. Connected document ingestion is the normal path after setup. Manual upload is a
    fallback for Documents received elsewhere.
45. Supersession requires authority Evidence such as a revision index or replacement
    notice. Corridor and the coordinator do not guess from filenames or dates; the
    relationship remains unresolved until Evidence arrives.
46. When a Document uses an unfamiliar External Party name, the coordinator chooses
    a suggested existing External Party or creates a new one. Corridor records a
    confirmed alias for later Documents.
47. A work-list item may be deferred only with a structured reason and a stated
    return date. It remains in history and returns to the work list when due.
48. Each project has one named escalation contact. Urgent overdue work notifies that
    person and the Internal Owner, not the whole project team.
49. External Parties do not receive Corridor accounts in the first release. Their
    statements enter through Documents and recorded Verbals.
50. #196 is an internal workflow rehearsal, not target-user validation. Its tester
    uses only the ordinary product interface; all developer knowledge and help is
    recorded; timing is provisional. A target construction user is not currently
    available.
51. Any coordinator-facing CLI, database query, or manual data repair fails #196.
    Corridor operations may use technical tools separately to investigate a defect.
52. Coordinator timing in #196 starts when the seeded coordinator enters the product
    and sees the prioritized work. It ends after the dated statement work, resulting
    Exception review, and approved Report export. Document processing and Corridor
    operations time are separate.
53. The original one-record target was five minutes or less without developer help.
    It covered reading the cited statement, choosing its Dependency, correcting both
    dates, confirming the Exception, and creating the export. Decision 72 supersedes
    that total with separate targets for the two-record rehearsal.
54. The dated-statement work item is one guided workflow containing the cited page,
    suggested Dependencies, correction fields, and result preview. The coordinator
    does not leave it to search separate screens.
55. A statement that moves a date records the previous date stated, new date stated,
    and exact Document wording. Corridor does not assume the previous Ledger date is
    the previous date named in the statement.
56. **Committed Date Change** replaces the vague term **Slip** in domain and product
    language. It states both dates, direction, External Party, and the work to review.
57. Corridor uses Committed Date Change only when it can show both attributable
    dates. A statement with only one date is a Commitment.
58. A later Committed Date Change creates priority work. An earlier change remains in
    history and interrupts work only when it affects a Milestone or Next Action.
59. Closing a later Committed Date Change requires the coordinator to record whether
    it affects a Milestone, whether the current Next Action must change, and either
    the new Next Action or a reason no change is needed.
60. Users see plain Exception statements, not codes such as `OVERDUE`,
    `MISSING_ACTION`, or `CONTRADICTION`.
61. Trained Corridor operators may use CLI tools in the first release. An operation
    affecting the Ledger writes an attributable receipt and shows its consequence in
    plain project history; customer users never use those tools.
62. #196 exercises only the project-coordinator workflow. The deferred
    construction-worker permission decision does not block this rehearsal but does
    block final production permissions.
63. #196 is the first narrow vertical slice: show the SH 99 work, link and correct
    the statement, record the date change, show the plain Exception, record the
    project response, and create the approved Report export. Other agreed workflows
    follow after this interaction model is proved.
64. #196 uses one seeded individual coordinator identity and begins timing after
    entry. Magic-link sign-in is required before customer use but does not block the
    rehearsal.
65. The Carry-Forward and Active Run redesign does not block #196. Any technical run
    preparation needed to establish safe rehearsal inputs is a Corridor-operations
    precondition outside the timed user flow; the managed-operations slice follows.
66. Before #196, Corridor operations runs the one-time SH 99 mechanical Admission
    backfill and verifies its receipts. Setup time is measured separately and is not
    presented as customer work.
67. Kinder Morgan Candidate 7296 is the real Committed Date Change rehearsal case.
    Candidate 7306 lacks a previous date, and Candidate 7554 is less explicit.
68. One External Party Commitment may have an explicit scope covering several
    Dependencies. Corridor stores one Commitment and links each affected Dependency
    rather than duplicating the event.
69. The scope picker shows the External Party's active Dependencies with readable
    locations and utility details and offers: all active Dependencies, selected
    Dependencies, or scope not yet known. Corridor may suggest but never chooses the
    human scope decision.
70. An unknown Commitment scope does not change individual Dependencies or their
    Exceptions. Corridor preserves the party-level statement and creates work to
    determine scope.
71. The interview first split #196 into Kinder Morgan Candidate 7296 for the
    multi-Dependency Committed Date Change and an Air Products PL35 case for overdue
    reporting. The Air Products choice was later found invalid and is superseded by
    Decisions 74 and 75; it must not survive in the corrected contract.
72. The provisional coordinator target is eight minutes total: five minutes or less
    for the Kinder Morgan date-change task and three minutes or less for the separate
    overdue review and Report export.
73. A real External Party Commitment may create party-level overdue work while its
    Dependency scope remains unresolved. It must not change individual Dependencies.
74. Every Commitment identifies the party that actually made it, separate from an
    affected External Party. The cited text must support that attribution. If the
    speaker is unclear, preserve the statement but do not set a Committed Date or
    create overdue work. This rule invalidates "Air Products will be invited" as an
    Air Products Commitment.
75. Equistar Candidate 7129 replaces the invalid Air Products PL35 case in #196. It
    supports "Equistar to provide a chain of title … due 01/2025" and keeps its
    Dependency scope unresolved until confirmed.
76. A month-only date displays at its stated precision, for example "Due: January
    2025," never January 1. It becomes past due only after the last day of that month,
    and Corridor preserves the exact wording.
77. An External Party Commitment closes only through verified Evidence of completion
    or a recorded Verbal from that External Party. A Work Decision or internal
    completion click cannot prove External Party performance.
78. The overdue fact remains visible, but its item leaves the immediate inbox after
    it has an Internal Owner and current Next Action. It returns when that action is
    due or new information arrives.
79. An overdue Commitment with unresolved Dependency scope appears in a party-level
    External Party commitments Report section. It shows the External Party,
    Commitment, stated due period, scope status, Internal Owner, Next Action, and
    exact source.
80. Correct #196's contract before implementation. It must use Kinder Morgan 7296,
    Equistar 7129, Committed Date Change, multi-Dependency or unknown scope,
    stated-party attribution, precision-preserving dates, the no-CLI rule, and the
    eight-minute internal rehearsal.
81. Keep #196 as the parent rehearsal because its business objective remains valid.
    Amend its evidence and workflow; create child issues only after the corrected
    parent contract defines their boundaries.
82. These rules are domain and product contracts, not facts local to #196. The
    original interview is distributed across ADR-0034 through ADR-0037; later
    clarification separates Coordination Plans, atomic guided statement decisions,
    and external Report release into ADR-0038, ADR-0039, and ADR-0040. Include this
    complete inventory so none are lost, then amend #196. Construction-worker
    permissions remain explicitly unresolved.

## 2026-08-12 post-foundation choice ledger

This second loss-prevention ledger records every choice accepted after #217 and its
children were delivered. `P` distinguishes these numbers from the original 82. It
contains two deliberately different classes because the interview required every
approved choice to remain in an ADR:

- P6-P35 and P40-P50 are durable product or domain decisions. The focused ADR named
  by an item is normative.
- P1-P5, P36-P39, and P51-P57 are a non-architectural record of this delivery. They
  do not generalize branch names, issue numbers, rehearsal inputs, or rollout order
  into product architecture. `docs/sh99-date-rehearsal.md` is their executable
  authority and may supersede them without changing Corridor's domain model.

1. **P1:** Deliver local commit `7b9a233` only by transplanting that one change onto
   fresh current `main`; never merge the divergent branch wholesale.
2. **P2:** Review and deliver the corrected domain contract before starting another
   product slice.
3. **P3:** Keep #196 as the primary outcome rather than pivoting to another umbrella.
4. **P4:** Make SH 99 mechanical Admission backfill and receipt validation the first
   bounded #196 child, before UI work.
5. **P5:** Use two operational gates: an isolated database-clone rehearsal, then
   explicit approval before any shared-database mutation.
6. **P6:** Preserve the original legacy Candidates. Guided Adjudication may add
   Evidence-backed context; prompt and extractor changes are a separate measured
   issue.
7. **P7:** An accepted party-level Commitment or Committed Date Change may own a
   Coordination Plan (ADR-0038).
8. **P8:** The plan follows the Commitment Lineage when scope becomes known and is
   never copied to linked Dependencies; Dependency-specific follow-up requires a new
   Dependency-subject Work Decision (ADR-0038).
9. **P9:** Statement adjudication and Coordination Planning share one guided flow but
   remain two distinct kinds of domain act (ADR-0039).
10. **P10:** One Save commits the statement and its distinct Work Decisions atomically
    or changes nothing (ADR-0039).
11. **P11:** The flow is general product behavior with SH 99 cases as acceptance
    examples, not a project-specific wizard (ADR-0035).
12. **P12:** Commitment Scope supports one Dependency, a selected set, all currently
    active party Dependencies as an explicit snapshot, and scope not yet known
    (ADR-0036, ADR-0039).
13. **P13:** Every Work Decision has exactly one Coordination Subject: a Dependency
    or an accepted External Party Commitment or Committed Date Change, never both
    (ADR-0038).
14. **P14:** Only an accepted Commitment or Committed Date Change may own a
    statement-level plan; a pending Candidate, refused statement, closure, or Not
    relevant disposition may not (ADR-0038).
15. **P15:** Verified Evidence or an attributable Verbal may close an External Party
    Commitment but never completes or cancels an internal Next Action (ADR-0038).
16. **P16:** Every Committed Date Change plan records Milestone Impact as affects,
    does not affect, or not yet known (ADR-0038).
17. **P17:** `Affects` links exact registered Milestones and cannot be replaced by
    free text (ADR-0038).
18. **P18:** `Not yet known` is unresolved work and keeps an Internal Owner, Next
    Action, and Action Due Date or structured unknown-date reason (ADR-0038).
19. **P19:** One plan form writes independent append-only Work Decision chains for
    owner, action and date, and Milestone Impact (ADR-0038).
20. **P20:** A stale concurrent Save refuses atomically and never overwrites or
    partly applies work (ADR-0038, ADR-0039).
21. **P21:** Completing or cancelling the action on an open subject requires a
    successor action or structured no-follow-up reason (ADR-0038).
22. **P22:** No follow-up removes only immediate work. The open or past-due fact
    remains and returns on Evidence, a Verbal, timing, scope, Milestone Impact, or
    due-state change (ADR-0035, ADR-0038).
23. **P23:** One statement creates one work item with all Attention Reasons; its
    highest-consequence reason controls order (ADR-0035).
24. **P24:** Immediate Undo reverses the entire guided Save through append-only
    reversal records; later Correct may target one fact (ADR-0039).
25. **P25:** Undo makes the statement and Work Decisions noncurrent and returns the
    unchanged Candidate to prioritized work (ADR-0039).
26. **P26:** Undo refuses if a later act depends on any result; it never cascades
    through that work (ADR-0039).
27. **P27:** Scope correction appends on the same statement; attribution or timing
    correction, including supported facts that change the derived type, creates a
    successor in the Commitment Lineage and triggers plan review (ADR-0038,
    ADR-0039).
28. **P28:** Not relevant requires a reason, creates no statement or plan, and is
    reversible (ADR-0039).
29. **P29:** Missing stated-party or timing context requires verified Evidence,
    including another quote where needed; confirmation alone is insufficient
    (ADR-0036, ADR-0039).
30. **P30:** The accepted statement and Adjudication receipt carry corrected values;
    the original Candidate payload remains unchanged (ADR-0039).
31. **P31:** Corridor derives Commitment versus Committed Date Change and its
    direction from supported timings; unsupported direction remains unknown
    (ADR-0036, ADR-0039).
32. **P32:** The UI shows Evidence and human-readable Dependency locations, never
    technical ids; suggestions order choices but never choose scope (ADR-0035,
    ADR-0039).
33. **P33:** Unknown scope remains an Attention Reason. A plan may defer its immediate
    item only under explicit return behavior; it cannot resolve scope (ADR-0035).
34. **P34:** Internal Owner comes from the searchable project roster, never free text
    (ADR-0035, ADR-0038).
35. **P35:** Unknown Action Due Dates and cancelled actions require structured
    reasons and may carry an optional note (ADR-0035, ADR-0038).
36. **P36:** #196 defers production authentication, notification delivery, and worker
    permissions. Assignment still takes effect under the seeded rehearsal identity,
    without claiming those production capabilities (ADR-0034, ADR-0035).
37. **P37:** Backfill validation records exact before and after rows, Candidate
    states, Policy Runs, outcomes, and reasons. Candidate 7587 abstains while 7129
    and 7296 remain pending; no guessed totals substitute for the receipt audit.
38. **P38:** A second identical backfill may append an immutable run receipt but
    creates no duplicate Ledger facts.
39. **P39:** Backfill runs from clean current `main` against an isolated clone first;
    shared SH 99 mutation requires separate explicit approval.
40. **P40:** Exact-day timing becomes past due after that day; month timing after
    month end; approximate and legacy-unknown timing creates no invented overdue
    boundary (ADR-0036).
41. **P41:** Party-level past due is a Derivation over the Commitment and is never
    copied to an unproved Dependency Exception (ADR-0036).
42. **P42:** The current attributable timing, including the new timing of a Committed
    Date Change, governs until verified Evidence or an attributable Verbal closes it
    (ADR-0036).
43. **P43:** A current plan may remove one item from immediate work; it returns when
    its action is due, new information arrives, scope changes, or Milestone Impact
    changes (ADR-0035, ADR-0036).
44. **P44:** Every open party-level Commitment with unknown scope appears in the
    External Party commitments Report section, not only overdue ones; closed entries
    live in history (ADR-0040).
45. **P45:** Each section entry carries party, supported statement, timing words and
    precision, scope, current plan, status, and exact provenance (ADR-0040).
46. **P46:** Internal Reports update automatically and require no approval
    (ADR-0040).
47. **P47:** The first released format is one fixed PDF; other formats are later work
    (ADR-0040).
48. **P48:** Release binds exact bytes, SHA-256, Evaluation date, ruleset, provenance
    mode, covered records, releaser, and timestamp (ADR-0040).
49. **P49:** Only unsupported provenance, inconsistent Evaluation inputs, or failure
    to seal exact bytes blocks release. Unknown scope, overdue facts, and visible
    missing work are honest nonblockers (ADR-0040).
50. **P50:** Release is separate from email and document-control delivery. The seeded
    coordinator may release the rehearsal PDF; production roles remain later
    (ADR-0040).
51. **P51:** Retain the provisional limits of at most five minutes for 7296 and at
    most three minutes for 7129 plus release.
52. **P52:** Pin rehearsal inputs, measure operations separately, record failures
    honestly, and never present the internal rehearsal as customer validation.
53. **P53:** Deliver `7b9a233` and the corrected contract as separate reviewed changes
    based on fresh current `main`.
54. **P54:** Order the remaining #196 children: backfill and receipt; statement-subject
    Work Decisions and migration; guided atomic Save, correction, and Undo;
    prioritized work list and party-level past due; Report section; fixed release;
    timed rehearsal.
55. **P55:** #150 Report-comparator research and the human #151 records request may
    proceed in parallel but do not displace #196.
56. **P56:** Triage stale #164, #165, #172, and #70 separately from executable
    product work and close, supersede, or defer them only with evidence.
57. **P57:** Deliver the two cleanup changes before publishing the bounded #196 child
    graph, and retain the explicit shared-database approval gate when execution
    reaches backfill.
