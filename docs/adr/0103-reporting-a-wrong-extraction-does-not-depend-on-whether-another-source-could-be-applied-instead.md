---
status: accepted
domain: human-work
scope: current product
amends:
  - ADR-0100
migration: the widened availability ships in #836's implementation; this ADR records the rule that implementation follows, because ADR-0100's Consequences clause states the narrower one.
---

# Reporting a wrong extraction does not depend on whether another source could be applied instead

**Amends ADR-0100.** It replaces one clause — when the **Report an extraction
error** control is offered — and changes nothing else ADR-0100 decided. The act
itself, its retention, its exact-capture binding, its non-overwrite guarantee,
its no-change outcome and its place as an ancillary action rather than a fifth
primary decision are all unchanged and still governed by ADR-0100 as ADR-0101
amends it.

## Context

ADR-0100 said, in its Consequences:

> **#836** implements the decision: the **Report an extraction error** control
> on the focused Review form, offered where no alternative captured value
> exists […]

That sentence was written from the ADR's motivating narrative, which is the
case where a coordinator has nowhere to put what they know: Apply writes the
misreading, Keep current rejects a change the source really made, Edit and apply
needs another captured Source Fact that does not exist, Needs coordination sends
Corridor's own defect to the customer as their work, and Defer moves the same
dead end to a later date. In that case there is no alternative captured value,
so "offered where no alternative captured value exists" described the narrative
correctly and was read as the rule.

Read as a rule it says something the narrative never claimed: that the existence
of a second source Corridor could apply instead makes the first capture no
longer worth reporting. It also confined the control to the focused form, which
left an individual change inside a batch with no way to report a misreading at
all — and a source revision arriving as a batch is the ordinary case, not the
exceptional one.

The implementation of ADR-0100 was written to the narrow sentence, so
`capture_correction.offers_correction` required both `item.focused` and the
absence of any alternative for the change.

## Decision

**The control is offered on every change an item decides, where there is a
capture to challenge.** Whether another source could supply a usable value
instead is not part of the question.

Two conditions remain, and both are about whether there is anything to report
against rather than about what else could be done:

- the change is one this item decides, not one it lists read-only — which is how
  ADR-0085's exactly-once rule is kept when the same change appears on more than
  one item's screen;
- the change shows a captured Source Fact, because a report names an exact
  capture and there is nothing to name without one.

An individual child of a batch therefore carries the control, on the same terms
as a focused item's change.

## Why the availability was wrong, not merely narrow

A misread capture is a statement about what a retained source says. Another
document carrying a usable value is a different source; applying it settles what
the record should show and leaves the misreading in place, uncorrected and still
available to be read again. Those are two different acts with two different
subjects, and making the second one suppress the first confuses "the coordinator
has a way forward" with "Corridor read this source correctly".

The narrative case — no alternative exists — is where the act is *indispensable*.
It is not where the act is *valid*.

## What this ADR does not change

- **The same-document restriction on the selected passage stays.** A passage of
  another document is an alternative source or a new capture, not evidence that
  this document was read incorrectly. ADR-0100's reasoning is unchanged and the
  binding remains structural.
- **The act is still ancillary.** ADR-0085's four primary decisions and
  secondary Defer are untouched. The correction module still appears in no
  outcome control, builds no decision request, and resolves no Proposed Delta.
  Widening where the control appears does not widen what it is.
- **ADR-0101's retirement lifecycle is untouched.** What becomes of an obsolete
  proposal after a correction is decided there, not here.

## Consequences

- `capture_correction.offers_correction` requires that the item decides the
  change and that a capture exists, and nothing more. A test proves the
  exactly-once rule holds for a change that appears on a second item read-only,
  including the case where the deciding item's own row is offered to the item
  that only lists it.
- A batch offers its reportable changes and builds the report for the one asked
  about, because a capture table, a passage picker and a report form for every
  change in a source revision is a screen each.
- ADR-0100's Consequences clause is superseded on this point only. Its narrative,
  its retention and binding properties, and its statement of what #842 may not
  override are all still in force.
- #836's first acceptance criterion was written from the same narrow sentence and
  is restated to match this decision.
