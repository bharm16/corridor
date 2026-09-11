---
status: accepted
domain: record-inclusion
scope: current product
amends:
  - ADR-0083
  - ADR-0084
  - ADR-0100
---

# A corrected capture retires its obsolete Proposed Delta through a dedicated append-only relationship

**Amends ADR-0100, ADR-0083, and ADR-0084.** One clause moves in each, and
nothing else in any of the three changes.

- **ADR-0100** says the obsolete technical finding is removed or superseded
  "through the declared lifecycle — ADR-0083's Proposed Delta lifecycle as
  ADR-0084 amends it". That lifecycle cannot represent the transition, which
  ADR-0100 anticipated: "Where the existing lifecycle cannot represent that
  transition, #836 states the missing relationship explicitly rather than
  silently implementing a new disposition." This ADR is that statement, and it
  is what ADR-0100's clause now points at.
- **ADR-0083** writes the lifecycle as "open → resolved (accepted, edited,
  rejected, deferred) or superseded (a newer source version for the same
  subject and field coalesces into one live delta)". Its supersession
  definition is unchanged and is deliberately not widened. What changes is that
  the two exits it lists are no longer the only ones.
- **ADR-0084** restates that lifecycle as "open → resolved (accepted, edited,
  rejected) or superseded; deferred is an open state with a return condition".
  Its deferral rule, its constrained `edit`, and its spine-native reader rule
  are unchanged. The restated exit list gains a third member below.

ADR-0085, ADR-0082 and ADR-0076 are unchanged and are relied on throughout.

## Context

ADR-0100 decided that a correction to a wrong capture may establish that
**nothing changed**, and called that "a truthful outcome of this path, not a
failure of it". It also decided what must then happen: retain the correction
and its evidence, recompute the actionable reading, remove or supersede the
obsolete technical finding through the declared lifecycle, manufacture no
zero-difference delta, and record no customer rejection or acceptance that
nobody performed.

#836 then built the correction request and found that the declared lifecycle
cannot carry the last of those. Standing is derived, not stored: a Proposed
Delta is `resolved` if a disposition row exists, `superseded` if a supersession
row names it as the prior delta, `deferred` if a live deferral exists, and
`open` otherwise (`src/corridor/delta_resolution.py`, `live_delta_status`).
Only two of those four are exits, and both are false entries for this outcome.

- A `reject` disposition is ADR-0084's "decision that the accepted value
  stands". Writing one files a coordinator decision that no coordinator made,
  into the decision lineage ADR-0082 requires every accepted value to carry.
  ADR-0100 already refused it in those words.
- A supersession is ADR-0083's *newer source version for the same subject and
  field* coalescing into one live delta. A corrected re-read of the **same**
  source version is a different cause, and PostgreSQL refuses the row anyway:
  `ck_delta_supersessions_successor` requires that a supersession name a
  successor artifact — `superseding_delta_id is not null or source_reading_id
  is not null or minutes_capture_id is not null` — and a retirement caused by a
  correction that produced no replacement names none of the three.

So #836 stopped, as ADR-0100 instructed, and named the gap with a refusal
(`withdraw_for_no_change`, refusing with `NO_CHANGE_EXIT_UNAVAILABLE`) rather
than closing it with an unannounced new disposition value. The consequence a
coordinator lives with until this is built is that operations cannot complete
the no-change path at all: the obsolete finding stays open on their screen with
all four primary decisions offered, and the one honest outcome — Corridor
misread this, and nothing actually changed — has nowhere to go.

This ADR records the maintainer's decision of 2026-09-11 closing that gap.

## Decision

### The transition is its own append-only relationship

A corrected capture retires the Proposed Delta that depended on it through a
**dedicated, append-only relationship**. In the maintainer's words, recorded
2026-09-11:

> Use a dedicated, append-only relationship, with an internal name such as
> `DeltaCaptureCorrection`:
>
> *This particular Proposed Delta no longer represents an actionable comparison
> because the particular capture on which it depended was corrected.*
>
> For the no-change outcome, its retained explanation is: *The corrected
> capture was compared against the accepted record, and that comparison
> established no difference.*

That sentence is the whole assertion. It is not a decision about what the
record should show, it is not a claim that a coordinator concluded anything,
and it is not a newer source version arriving. It says that the comparison this
delta represented was computed from a capture that has since been corrected, so
the comparison no longer stands for anything a person should be asked to
decide.

The relationship is append-only and immutable, like every other row in this
lifecycle. A retirement recorded in error is not edited; it is answered by the
recomparison that follows a further corrected capture, on the same terms as any
other mistaken capture.

### A corrected Source Fact alone does not prove "no difference"

The relationship **must not** reduce to `old_delta_id → corrected_fact_id`.
In the maintainer's words:

> A corrected Source Fact alone does not prove "no difference." That conclusion
> also depends on the accepted record and comparison rule.

A corrected capture says what the source says. Whether that equals what the
record holds is a second question whose answer depends on which accepted
revision was read and which comparison rule was applied. A pair of foreign keys
records the first and leaves the second unrecorded, so a later reader asking
"why did this stop being a question?" would find the corrected fact and have to
recompute the conclusion from whatever the record holds *now* — which is not
the record the conclusion was drawn from.

**The relationship, or the correction-result receipt it references, therefore
identifies the complete proof:**

1. the **project** and the **prior Proposed Delta** being retired;
2. the **correction request** that caused the investigation;
3. the **exact challenged capture** — the Source Fact that was on the screen,
   by the immutable identity ADR-0100 requires, never by a query that moves;
4. the **corrected capture and its source evidence** — the new Source Fact
   beside the old one, its typed locator and its support assessment
   (ADR-0082's source-backed class);
5. the **accepted revision used for the recomparison**;
6. the **comparison rule and its version**;
7. the **recomparison outcome**;
8. the **executing identity and the recorded instant**; and
9. an **idempotency identity**.

Items 1 through 9 may be split between the relationship row and the
correction-result receipt it names, provided every one of them is reachable
from the relationship without recomputation. Where they sit is an
implementation choice; that they are all recorded is not.

**This is not a reason to build a generalized event framework.** The request
identity already exists as the row #836 records
(`capture_correction_requests`, written by `report_capture_correction`), and
the result identity is the receipt #842's re-capture procedure already has to
issue. This relationship reuses both. A generic correction-event table with a
payload column would reintroduce exactly the shape ADR-0067 retired and would
make the binding conventional rather than structural.

### The comparison is the ordinary comparison, not a second opinion

The recomparison uses the **normal comparison contract** —
`proposed_delta_comparison.compare_stated_subjects`, under a declared
`comparison_rule_version`, the same rule every producer of Proposed Deltas
already compares under. **A correction handler may not invent a second
"looks equal" check of its own.** Two comparison rules mean a value that agrees
on one path and proposes a change on the other, and this path is the one where
disagreement is least visible: its output is a proposal quietly disappearing.
The rule version recorded at item 6 above is that contract's version, so a
later reader can replay the conclusion.

### The four outcomes, deliberately

A correction investigation ends in exactly one of four states, and each is
recorded as itself.

**The corrected result matches the accepted value.** Retire the obsolete
proposal through the correction relationship, with the retained explanation
above. **Create no zero-difference delta.** ADR-0100 already refused the
invented delta — "a finding with no content" — and nothing changes about that:
there is no difference to propose, so there is no proposal, and no Resolve
Delta is performed because there is no accepted value to change.

**The corrected result still differs.** Produce the valid replacement proposal
from the corrected capture, and link the original's retirement to the same
correction result. **The replacement case may not quietly reuse
newer-source-version supersession either.** The cause is a correction to a
capture, not a newer source version, and ADR-0083's supersession sentence
stays true by not being stretched to cover a second cause. A reader asking why
the first proposal went away is told the truth in both cases, and it is the
same truth: its capture was corrected.

**The correction cannot be substantiated.** Retain the investigation result and
leave a clarification path open. **Do not claim a successful correction.** The
proposal stays open; ADR-0100's rule that "operations corrects a capture
against bytes that are already retained; it does not settle what the source
failed to say" is unchanged. In the maintainer's words:

> "The source did not establish this assertion" is not automatically "the
> source matches the accepted value." If correction finds missing or ambiguous
> evidence, record that outcome honestly rather than creating a Source Fact
> claiming absence or equality.

A capture asserting that the source says nothing, or that it says what the
record already holds, is a manufactured fact with a locator that dereferences
to nothing of the kind. It is forbidden here for the same reason unsupported
free text is forbidden as an accepted value (ADR-0084), and the honest record
is the investigation result plus the still-open question.

**The original proposal was already decided.** Preserve that decision and all
issued history. The retirement is refused, not applied over the top: a delta
carrying a disposition has already produced a Project Record revision or a
recorded decision that the accepted value stands, and a later correction to the
capture does not unmake either. Any necessary repair to the accepted record
**returns through a new authorized Review path**, where a person decides again
with the corrected evidence in front of them. Previously issued artifacts are
not rewritten.

### What the coordinator is told, approved as written

The three sentences below are approved wording. Each is shown linked to the
retained correction request, the original capture, the corrected evidence, and
the comparison result, so a coordinator who wants the proof can reach it from
the sentence.

**No change.** *"Corridor corrected its reading of this source. The corrected
value matches the accepted record at revision 12, so this proposed change is no
longer in Review. No accepted value changed."*

**Replacement.** *"Corridor corrected its reading of this source. The original
proposed change has been replaced by a corrected proposal. Review the corrected
proposal before changing the accepted record."*

**Stale Apply.** *"This proposal can no longer be applied because its source
reading was corrected. Nothing was applied. View the correction result."*

The revision number in the first sentence is the accepted revision the
recomparison actually read — proof item 5 above, printed rather than described.

**The original item leaves active Review without disappearing from history.**
It is gone from the Work List, the packets and the open counts because it is no
longer actionable; it remains in the record history, with its correction
result, its prior deferral receipt and its Follow-up history, because it
happened. A coordinator who remembers seeing a proposed change and goes looking
for it finds it, and finds out what became of it.

### Technical Operations authorizes and executes it, with no second decision

**A person holding the Technical Operations designation may authorize and
execute the source-grounded correction procedure. Its validated result may
retire the obsolete Proposed Delta through a narrowly granted command, without
another coordinator decision.**

The reason is what the operation is. It removes a machine-generated comparison
that no longer has a valid basis; it does not choose what the customer's
accepted record should say. Asking a coordinator to approve the removal would
hand Corridor's own extraction defect back to the customer as extra
coordination work, which is exactly what ADR-0034 calls disguised work for the
customer and what ADR-0100 already refused when it rejected routing this case
to Needs coordination. The Operations boundary the
[Corridor Operations glossary](../operations/CONTEXT.md) already draws puts
repairing Corridor's own processing on Corridor's side of it, and this is that
kind of act.

**When a worker executes part of the procedure, the responsible operations
actor and the service identity performing the work stay distinct.** A queued
re-capture running under a service identity does not become the author of the
decision to correct; the designation holder who authorized it remains the
responsible actor, and both are recorded. This is ADR-0081's rule that a
migration executor is never the semantic author, applied to the same shape of
problem.

**This must not become a generic operations permission to remove things from
Review.** The command is narrow, and it establishes six things before it
retires anything. They are the proof bound above, stated as the conditions the
command checks:

1. **the exact challenged capture and the request are identified** — by the
   immutable identity ADR-0100 requires, not by a query that moves;
2. **the corrected capture is supported by the retained source** — the
   reporter's suggested answer alone is not evidence, and an expected
   interpretation never becomes a value (ADR-0084, ADR-0100);
3. **the recomparison used the declared rule and the stated accepted
   revision** — the ordinary comparison contract, both inputs recorded;
4. **the delta is still eligible for correction retirement** — a customer
   decision made during the investigation must not be undone;
5. **no accepted value changes and no disposition is fabricated** — the
   command writes neither; and
6. **the operation is retained and idempotent** — its evidence survives and a
   repeat is the same act, not a second one.

**No second-person approval is required for this first slice.** The protection
is the bounded proof, the command's own authority, the concurrency checks and
the retained evidence — not asking another person to click through the same
technical result. A reviewer who cannot re-derive the comparison adds a
signature, not a check.

### It is a derived terminal reason, not a status column

**Do not add a mutable `status` column to `ProposedDelta`.** Standing stays
derived from immutable relationships, which is why the current lifecycle can be
read consistently by four independent readers and re-derived in SQL for the
shadow seal. This is one more derived terminal reason, backed by one more
append-only relationship, and it is read the same way the other three are.

Ordering, where a reader must pick one word: a disposition still wins, then a
supersession, then this retirement, then a live deferral, then open. The last
of those is the one that does real work — **a deferred delta is retired without
being woken**, because a scheduled return to a comparison that no longer exists
is a return to nothing.

**That a delta never carries two terminal relationships at once is a required
invariant, not something the precedence order proves.** Checking retirement's
own inputs is not enough; the invariant needs protecting in both directions and
at every write:

- **retirement refuses** a delta already disposed of or superseded;
- **resolution, supersession and scheduling commands refuse** a delta already
  retired by correction;
- **concurrent competing acts serialise** — one wins, and the other receives a
  bounded refusal rather than a lost write or a silent overwrite;
- **Undo of an old scheduling act may not reactivate a correction-retired
  proposal**, because reversing a deferral must not resurrect a comparison
  whose basis is gone; and
- **inconsistent imported or historical records** that contain incompatible
  terminal relationships surface an **integrity problem** rather than letting
  precedence quietly erase the contradiction.

In the maintainer's words: *the shared readers may use the order to present
valid history; they should not be the mechanism that makes contradictory writes
appear harmless.*

**Retiring a proposal preserves everything that already happened to it.** Any
prior deferral receipt and any Follow-up history are retained and remain
readable. Removing an invalid proposal from active work does not mean the
scheduling or the correspondence never happened, and a history reading that
hid them would be making the same false claim as a fabricated disposition.

### Every shared lifecycle reader and write command honours it

Visibility is not the requirement. In the maintainer's words:

> Adding the new relation only to the Work List would leave a stale browser
> able to apply the obsolete delta.

A retired Proposed Delta is out of the actionable set **as an authority rule**,
enforced at the write boundary, and every shared reading agrees with that rule.
The responsibilities, with the modules that carry them today:

- **ordinary Review and batch readings** — the focused Review form and the
  source-revision batch (`packet_review.read_review_items`,
  `review_packet_reading.read_open_deltas`, `open_deltas`, `current_deltas`,
  `standing_sets`);
- **standalone and packet resolution** — both halves, Python and the
  `SECURITY DEFINER` commands, which already spell the state guards separately
  (`delta_resolution.resolve_delta`, `validate_child_decision`,
  `review_packets.resolve_review_packet`, `_validate_child`, and
  `public.resolve_proposed_delta_decision`);
- **deferral and resume** — scheduling a retired delta, and returning one
  (`delta_resolution.defer_delta`, `public.defer_proposed_delta`,
  `review_packet_reading.live_deferrals_by_project`, the reschedule route);
- **Follow-up readings** (`follow_up_bundles.read_follow_up_bundles`,
  `native_follow_up_reading`);
- **portfolio counts** and the project Work List
  (`project_portfolio.read_portfolio`, `derive_standings`,
  `project_workflow.read_project_workflow`, and the source register's
  per-document counts);
- **correction and decision history** (`record_history._delta_history`,
  `_standing`, and the packet receipt reading), which shows the retirement as
  the terminal reason it is rather than hiding it;
- **issue readiness and disclosure calculations**
  (`release_candidate._open_difference_state`, `candidate_is_stale`,
  `issue_rendering._unaccepted_deltas`,
  `report_preparation.execute_report_preparation`); and
- the **shadow and native parity readers**, whose SQL re-derivation of standing
  must move with the Python.

That list is the inventory of today's implementations, not a fixed set. The
rule is the invariant: **a reading that answers "is this delta actionable?"
answers it the same way everywhere**, and a new reader joins the list.

### A stale Apply submission refuses, and says why

**A previously rendered Apply submission must refuse after the correction
invalidates its input, and the refusal links to the correction result.**

This is the case the authority requirement exists for. A coordinator opens the
Review screen, operations corrects the capture and the recomparison establishes
no difference, and the coordinator — whose browser is showing a page that was
true when it was rendered — submits Apply. The submission names a delta whose
comparison no longer exists. It refuses, with a named refusal in the shared
refusal vocabulary (`delta_refusals`) raised by both the Python and the SQL
halves, carrying the correction result so the coordinator is told what happened
to the item rather than being told only that it is gone. It refuses in the
approved words above: *"This proposal can no longer be applied because its
source reading was corrected. Nothing was applied. View the correction
result."* The middle sentence is the one that cannot be dropped — silence, a
generic conflict, or a page that quietly drops the row all leave a person
believing they applied something they did not.

### Two edge cases

**A customer decision arriving during the investigation.** Finalising a
retirement **rechecks the accepted revision and the proposal's standing under
the same concurrency discipline ordinary decisions use** — the observed
accepted revision carried into the command, and the stale-revision and
already-resolved refusals the resolve path already raises. In the maintainer's
words:

> A comparison computed earlier is not sufficient permission to retire the
> proposal after the record changes.

A recomparison computed against revision *n* proves nothing about revision
*n+1*. If the record moved, the retirement is refused and the comparison is
recomputed; if a coordinator decided the delta in the meantime, the decided
case above governs and the decision stands.

**Prepared release packages.** Even when no accepted value changed, a
configured report may disclose open proposals as open questions, so removing
one **can change an issue's content**. Re-run the existing candidate and
readiness checks against the corrected reading. **Do not assume that "the same
accepted revision" means every prepared candidate remains current**: the
accepted revision is not the only input to what an issue discloses.
Previously authorized bytes are unchanged — ADR-0092's retained published
reading and ADR-0086's authorized package are not rewritten by a later
correction, and a package that must say something different is prepared again.

### The properties this decision requires

These belong to the decision, not to one implementation's test file. Each is
proved:

1. an erroneous capture corrected to the accepted value retires the proposal,
   writes no zero-difference delta, and writes no disposition;
2. a correction whose corrected capture still differs produces the replacement
   proposal and links the original's retirement to the same correction result,
   through this relationship and not through a newer-source-version
   supersession;
3. an exact retry of the same correction result is idempotent — one retirement,
   the same identity returned;
4. the same idempotency identity presented with different content is refused as
   a bounded conflict, rather than overwriting or silently returning the prior
   row;
5. the accepted value changing during the investigation refuses the retirement
   computed against the earlier revision;
6. another capture of the same document and field arriving does not move what
   the request and the retirement name;
7. an Apply form rendered before the withdrawal and submitted after it refuses,
   and the refusal carries the correction result;
8. an original proposal that was already accepted keeps its decision and its
   issued history, and is not retired; and
9. a correction that changes an open-question disclosure is reflected by the
   readiness and candidate checks rather than leaving a prepared package
   claiming an open question that no longer exists.

The one-terminal-relationship invariant carries three more, because an
invariant that only the happy path observes is not enforced:

10. a resolution, supersession or scheduling command acting on an
    already-retired delta refuses, and two competing acts serialise so that one
    wins and the other receives a bounded refusal;
11. Undo of a scheduling act that preceded the retirement does not reactivate
    the retired proposal, and the deferral receipt and Follow-up history it
    reverses remain readable; and
12. a record carrying incompatible terminal relationships — from an import or
    from retained history — surfaces an integrity problem rather than being
    silently resolved by the reader's precedence order.

## Terminology

This decision names a lifecycle relationship, so the terminology-research
procedure in
[docs/agents/domain.md](../agents/domain.md#research-before-proposing-terminology)
**applies** — a named lifecycle relationship in an ADR and in code is a domain
concept even while it is internal, and only ordinary software helper names sit
outside that gate. The evidence is
[capture-correction-retirement-terminology-2026-09-11.md](../research/capture-correction-retirement-terminology-2026-09-11.md).

**The finding: published practice does not supply this concept.** W3C PROV
(*PROV-DM*, W3C Recommendation, 30 April 2013) keeps two nearby relations
apart. **Revision** (§5.2.2) is a derivation whose result is a revised version
of an original — it relates a revised entity to the earlier one. **Invalidation**
(§5.1.8) is the start of an entity's destruction, cessation or expiry, after
which it is "no longer available for use". Corridor's corrected capture is a
revision in that sense; the obsolete Proposed Delta is nearer invalidation. But
neither relation says anything about retaining the earlier entity's evidence,
and neither says anything about a decision history recorded against it — and
both are exactly what this relationship must preserve. Construction change
control names the merit decision on a proposed change (change request, change
order, dispositioning under ISO 10007) and has no term for withdrawing an
automatically generated proposal because the generator misread its input.

**The treatment.** `DeltaCaptureCorrection` is retained as the proposed
**internal implementation name**, documented as a **Corridor-specific
relationship — not an established construction term and not a verbatim PROV
concept.** It is not adopted into the Project Record glossary and is never
shown to a customer. The [Corridor Operations glossary](../operations/CONTEXT.md)
carries its concise definition, under **Capture Correction Retirement**, beside
the other repair concepts that describe Corridor's work on its own processing.

It is distinct from four Corridor concepts it would otherwise be confused with,
and the research note states each distinction: a **Document Revision** is a new
issued version of the source, where here the bytes are unchanged and only the
reading was wrong; a **Supersession** names the newer version replacing an
earlier one for current use, where here there is no newer version and often no
successor; a **Resolve Delta `reject`** or **Keep current** is a person's
conclusion that the accepted value stands, where here nobody concluded
anything; and **deletion or disposition** removes data, where here every piece
of evidence and history is retained.

**Customer-facing wording is settled by description, not by a defined type.**
The three approved sentences above say what happened in plain words and name no
new concept, which is the same treatment ADR-0100 gave the "Report an
extraction error" control. Should a surface later want a defined
customer-facing **type** for this outcome rather than a description of it, that
research completes first, as ADR-0085 required of any customer-facing label for
a Review Packet.

## Considered options

**Close the delta with a `reject` disposition.** Rejected, and already rejected
by ADR-0100. It files a coordinator decision nobody made. The disposition is
the customer-facing surface of "the accepted value stands", and it carries an
attributable principal; using it here attributes a conclusion to a person who
was never asked.

**Widen supersession to admit a prior delta with no successor.** Rejected. It
requires relaxing `ck_delta_supersessions_successor`, which is the constraint
that makes a supersession row mean something: every existing row names the
artifact that took over. Relaxing it to admit a row naming only a prior delta
would make "superseded" mean two different things in one column, and the two
have different causes, different evidence, and different explanations owed to a
reader. ADR-0083's definition stays as written by not being asked to cover a
case it was not written for.

**Add a `status` column to `ProposedDelta`.** Rejected. Standing is derived
from immutable relationships precisely so that it cannot drift from its
evidence, and a mutable column would be writable by any path that forgot the
guard. It would also lose everything except the word: no request, no corrected
capture, no accepted revision, no rule version, no executor.

**Record `old_delta_id → corrected_fact_id` and nothing more.** Rejected for
the reason stated above: a corrected fact does not prove no difference, and a
conclusion whose inputs are not recorded cannot be replayed.

**Build a generalized correction-event framework.** Rejected. The complete
proof this relationship binds is a reason to record specific columns, not a
reason to build a generic event store; the request and result identities #836
and #842 already need are the ones to reuse.

**Add the relation to the Work List only.** Rejected. The Work List is one
reader. A rule enforced only where a person happens to be looking is not a
rule; a stale browser, a batch reading, a packet resolution, or a report
preparation would each still act on a comparison that no longer exists.

## Consequences

- ADR-0100, ADR-0083 and ADR-0084 record `amended_by: ADR-0101`; their bodies
  are unchanged. ADR-0100's "remove or supersede the obsolete technical finding
  through the declared lifecycle" now points at the relationship named here,
  which is what its own Consequences asked for. ADR-0083's and ADR-0084's exit
  lists gain a third member; ADR-0083's supersession definition, ADR-0084's
  deferral rule and constrained `edit`, and everything else in all three are
  untouched.
- ADR-0085 is unchanged and is relied on. The four primary decisions and
  secondary Defer are not extended, and packets remain a derived reading
  recomputed from the current set of open deltas — a set this decision shrinks
  rather than restructures.
- ADR-0082 is unchanged and is relied on. The retirement writes no accepted
  value, so it owes no decision lineage entry; the corrected capture is a
  source-backed fact and carries that class's complete provenance. Refusing
  both false exits is what keeps the lineage honest.
- ADR-0076's four record-changing operations are unchanged, and this adds no
  fifth. The retirement changes no accepted value and writes no Project Record
  revision. Where the corrected result still differs, the replacement is
  **Proposed Delta**, operation 3, and any accepted value still changes only
  through **Resolve Delta**, operation 4, by a coordinator.
- ADR-0092 and ADR-0086 are unchanged. A retirement never rewrites a published
  reading or an authorized package; it changes what the next preparation finds.
- ADR-0034 is unchanged and is relied on for the execution authority. Repairing
  Corridor's own reading is managed technical operations, so the Technical
  Operations designation (`access.TECHNICAL_OPERATIONS`, read live from the
  roster as `operations_repair` already does) authorizes and executes it, and
  the customer is not asked to approve Corridor's defect.
- The [Corridor Operations glossary](../operations/CONTEXT.md) gains one
  concise entry, **Capture Correction Retirement**, and
  [the terminology research note](../research/capture-correction-retirement-terminology-2026-09-11.md)
  records its evidence: W3C PROV supplies the revision/invalidation
  distinction but not this concept, construction change control names only the
  merit decision, and the relationship is therefore documented as
  Corridor-specific. The Project Record glossary is not extended, and no
  customer-facing type is defined.
- **#836** gains the relationship its `withdraw_for_no_change` refusal is
  holding open, and its stale acceptance criterion — that the corrected result
  returns as a Proposed Delta — is corrected to admit the no-change outcome.
  **#842** performs the receipted re-capture and the recomparison, and issues
  the correction result this relationship binds. Both keep every guard they
  have: #842 may not override a malware finding, a missing customer
  authorization, or source-fact semantics.
- Nothing ships with this ADR. It records a decision so that the lifecycle
  addition is built once, in the open, rather than appearing as an unannounced
  disposition value.
