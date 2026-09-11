---
status: accepted
domain: human-work
scope: current product
amends:
  - ADR-0085
migration: nothing here is built. #836 carries the correction request on the focused Review form, #842 performs the receipted operations re-capture, and #833's reason-to-action matrix routes an incorrect extraction to this path.
---

# A wrong extraction is corrected at its source and returns through Review

**Amends ADR-0085.** It adds one act to the set a Review Packet offers a
coordinator, and says what that act may and may not change. Everything else in
ADR-0085 stands: the adaptive packet keying, the rule that every open Proposed
Delta is actionable in exactly one packet, the three visible consequence levels,
the four primary decisions, and Defer's position as the secondary act.

## Context

The [2026-09-10 customer-journey audit](../research/customer-journey-audit-2026-09-10.md),
under "Edit currently means selecting another captured value", inspected the
focused Review form and found its central constraint working exactly as
intended. The form tells the coordinator that "An edited value is never typed
words for an external fact: it is another source's captured value"
(`src/corridor/web/templates/review.html`), and the `EDIT_AND_APPLY` branch in
`src/corridor/packet_review.py` refuses an edit that names no captured Source
Fact to apply. That is ADR-0084's constrained edit, and unrestricted typing must
not replace it.

The audit then asked the question the constraint leaves open: what does the
coordinator do when the source is clear but the extraction is wrong, and no
alternative captured value exists?

Consider a revised UCM whose Promised For cell plainly reads 2027-03-15, an
extraction that captured 2027-05-13, and an accepted record that still holds
2026-11-01. Every act the packet offers today produces a wrong record or no
progress. **Apply** writes the mis-extracted 2027-05-13. **Keep current** leaves
the stale 2026-11-01 and rejects a change the source really did make. **Edit and
apply** requires another captured Source Fact carrying 2027-03-15, and there
isn't one, because only one source states this date and it was read wrong.
**Needs coordination** opens a question for a named project person when nobody
outside Corridor is in any doubt; the utility said what it said, and the page
says what it says. **Defer** moves the same dead end to a later date. The
coordinator can see the right answer on the page in front of them and has
nowhere to put it that is both true and supported.

The tempting repair is to let them type it. That is the one repair the product
cannot allow, because a typed 2027-03-15 would enter the record as a
source-backed external fact with no source behind it, and, as ADR-0076 put it in
rejecting unattended projection, the false-write rate on material fields is the
metric ADR-0075 says the product is sold on.

## Decision

### The supported path is a correction request

When the source is clear, the extraction is wrong, and no alternative captured
value exists, the supported path is a **correction request** against the source,
not a new accepted external fact. In the maintainer's words, recorded 2026-09-10:

> Report an extraction error, retain the disputed capture and the exact source
> reference, operations performs a source-grounded correction or re-capture, and
> the corrected result returns through ordinary Review.

The coordinator identifies the incorrect field, selects the relevant source
passage or cell, and states the expected interpretation. Corridor operations
then performs the source-grounded correction or re-capture under ADR-0034's
managed-operations contract, where a technical failure is Corridor's work rather
than disguised work for the customer. The corrected result comes back to the
coordinator as an ordinary Proposed Delta in ordinary Review.

**If the source cannot substantiate the correction, the item stays a
clarification question.** Operations corrects a capture against bytes that are
already retained; it does not settle what the source failed to say. A request
whose expected interpretation the retained source will not bear is answered with
that fact, and the underlying question remains open for a person.

### Editing still means choosing another source's captured value

Editing a Proposed Delta means choosing another source's captured value, never
typed words. That rule is unchanged and is reaffirmed here. The correction
request exists precisely for the case where that rule leaves the coordinator
with nothing to choose, and it resolves that case without weakening the rule.
The expected interpretation a coordinator types is the *reason for a request*
about a capture. It is never an accepted value, and nothing in this path lets it
become one.

### The four acts, and what each may and may not change

A coordinator facing a questionable value has four distinct acts. They are not
interchangeable, and each has its own authority.

| Act | What it changes | What it may not do |
|---|---|---|
| **Correction request** | Records an attributable request that one named capture is wrong against one named source passage, with the expected interpretation. It changes nothing in the accepted Project Record and resolves no Proposed Delta. | May not change an accepted value, overwrite the disputed Source Fact or its Source Segment, or substitute for the coordinator decision the corrected result still needs. It is not a way to record what the coordinator believes; only what the source says and where. |
| **New Coordination Decision** | Records a named person's own current conclusion for the current subject, carrying the responsible human principal, the subject, and the decision time (ADR-0025, ADR-0082's Coordination Decision provenance class). | May not be presented as something a source states. It carries no source locator and no support assessment, and it does not correct an extraction or repair a capture. |
| **Edit to another source** | Makes an already-captured Source Fact from another source the accepted value, in one atomic Project Record revision (ADR-0085's Edit and apply, under ADR-0084's constraint). | May not turn unsupported free text into a source-backed value. It requires an existing captured Source Fact or one of ADR-0084's other named bases, so it is unavailable when no alternative captured value exists. |
| **Clarification** | Retains the open question with its exact wording and a responsible party, as ADR-0035's Needs clarification and ADR-0085's Needs coordination already provide. The Proposed Delta stays open. | May not change an accepted value, and may not be cleared by asserting the fact the source does not establish. |

The correction request is the act for *the capture is wrong about the source*.
A Coordination Decision is the act for *the project has decided something of its
own*. An Edit is the act for *another source already captured the right value*.
A clarification is the act for *the evidence does not settle this yet*. A
product that offers only the last three forces a coordinator with a clear source
to misuse one of them.

### What the correction process preserves

The process preserves all of the following, and a later reader can recover each
of them:

1. the original source bytes and the Source Segment that holds them;
2. the original extraction or capture, as the disputed Source Fact it was;
3. the requester and the stated reason;
4. the corrected capture's own identity and its supporting locator;
5. the link between the original and corrected results, in both directions; and
6. a subsequent coordinator decision, before any accepted value changes.

**The original Source Fact and its Source Segment are never overwritten.** A
correction produces a new capture beside the old one; it does not edit the old
one into agreement. This is ADR-0068's rule that a Source Segment stores its
exact text once, and ADR-0032's rule that nothing the machine surfaced is lost,
applied to the case where the machine was wrong. A person reviewing this field
in a year must be able to see that Corridor first read 2027-05-13, who said that
was wrong and why, what the corrected reading was, and who accepted it.

### No fifth record-changing operation

ADR-0076 decided that the Project Record changes "through four explicit
operations, and no other path". This decision adds none. The re-capture is
**Capture Source Fact**, operation 1, performed attributably by operations
rather than automatically. The corrected result is compared against the accepted
record and returns as a **Proposed Delta**, operation 3. The accepted value
changes only through **Resolve Delta**, operation 4, by the coordinator. The
correction request itself sits outside all four, because it changes nothing in
the record: it is a request about a capture.

For the same reason a correction request is not a Resolve Delta disposition. It
is not `accept`, `edit`, or `reject`, it writes no Project Record revision, and
it leaves the Proposed Delta open. It also is not a Defer: it carries no return
date, and what brings the item back is the corrected capture arriving, not the
calendar.

## Terminology

This decision introduces **no new customer-facing term**. It reuses Source Fact,
Proposed Delta, Resolve Delta, Coordination Decision, Support Assessment, Defer,
and Review Packet as the [Project Record glossary](../../CONTEXT.md) already
defines them, and Source Segment and Extraction Measurement as the
[Corridor Operations glossary](../operations/CONTEXT.md) defines them, so the
terminology-research procedure in [docs/agents/domain.md](../agents/domain.md)
is not triggered here.

`correction request` is an internal technical name, taken from the maintainer's
own decision text, and it is never shown to a customer as a defined concept. The
customer-facing wording of the control on the focused Review form is not fixed
by this ADR. If that wording is anything other than plain existing product
words, #836 runs the terminology-research procedure before the form ships, as
ADR-0085 required of any customer-facing label for a Review Packet.

## Considered options

**Let the coordinator type the correct value for this case only.** Rejected.
It is exactly the unsupported free text ADR-0084 forbids for Utility Owner,
attribution, Promised For, completion, Applies To, and agreement or permit
status, and the narrowness of the case is no protection: this is the case where
the coordinator is most confident and therefore most likely to be trusted
wrongly. The value would enter the record with a provenance class it cannot
satisfy under ADR-0082, because no locator and no support assessment exist for
words nobody wrote in a source.

**Record it as a Coordination Decision.** Rejected. A Coordination Decision is
the project's own conclusion, and its provenance class under ADR-0082 has no
source and no support assessment by design. Using it here would file a
statement about what the utility said as a statement about what Corridor's
customer decided, which is a different act by a different authority, and the
record would no longer be able to tell a reader which one happened.

**Let operations overwrite the bad capture in place.** Rejected. It destroys the
evidence that the extractor was wrong, which is the input to Extraction
Measurement and to any later question about this field, and it would put an
accepted value into the record with no coordinator decision behind it. The
correction produces a new capture and a new Proposed Delta precisely so that
both facts survive and the human decision still happens.

**Route the case to Needs coordination and leave it there.** Rejected. Needs
coordination asks a named project person a question. Here there is no question
for a project person: the source is clear and Corridor read it wrong. Sending
Corridor's own defect to the customer as coordination work is what ADR-0034
calls disguised work for the customer, and it would also hide the extraction
defect from the measurement that should count it.

**Add it as a second secondary act beside Defer.** Rejected as a framing, not as
a placement. ADR-0085 rejected "Snooze" because it was deferral under a second
name; that reasoning bars a synonym, not a distinct act. A correction request is
not deferral: it names a specific defect against a specific passage, it hands
work to a different party, and its return is caused by the corrected capture
rather than by a date. Whether the control reads as primary or secondary on the
form is a presentation question for #836.

## Consequences

- ADR-0085 carries `amended_by: ADR-0100`. Its "four primary decisions" section
  is extended by one act for the case named here; its list of four remains the
  set that disposes of a Proposed Delta, and the correction request disposes of
  nothing.
- ADR-0084 is unchanged and is relied on. Its constrained `edit` still admits
  only a captured Source Fact, a named replayable composition, a proven lossless
  normalization, or a separate attributable source origin; and unsupported free
  text about an external fact still produces a clarification, never an accepted
  value.
- ADR-0082 is unchanged and is relied on. A corrected capture is a source-backed
  fact and carries that class's complete provenance: typed source locator,
  locator validation status, at least one support assessment relating it to its
  Source Segments, and decision lineage. Nothing here alters the four provenance
  classes or the rule that a support assessment is a relation rather than a
  column.
- ADR-0076's four record-changing operations are unchanged, and this path adds
  no fifth.
- **#836** implements the decision: the correction request on the focused Review
  form, offered where no alternative captured value exists, with the retention
  and non-overwrite properties proved by test. **#833**'s reason-to-action matrix
  references it for its "incorrect extraction or locator" row. **#842**'s
  operations repair procedure performs the receipted re-capture, and may not
  override a malware finding, a missing customer authorization, or source-fact
  semantics on the way.
- Nothing ships with this ADR. It records a decision so that those three tickets
  build the same thing.
