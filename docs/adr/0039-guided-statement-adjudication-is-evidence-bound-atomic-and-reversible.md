---
status: accepted
---

# A guided statement decision is atomic, reversible, and bound to Supporting Documentation

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

An extractor may preserve a real External Organization statement without enough supported
context to admit it mechanically. The coordinator may need to establish attribution,
recover a timing from another quote, choose statement scope, and record the
project's response in one guided flow. Those are related but distinct acts. If they
write independently, a timeout or stale screen can leave an accepted statement
without the plan the user just saved, a plan without the statement it responds to,
or an Undo control that guesses which rows belong together.

Decision: **one guided Save is an atomic command bound to Supporting Documentation whose distinct
results remain separately attributable and reversible as one act.** The accepted
External Organization statement, its Supporting Documentation, scope decision, Extracted Proposal disposition,
Follow-up Plan Coordination Decisions, exact key date links, and one grouping receipt
all commit, or none does. The grouping receipt identifies the exact results of the
Save; it is not a substitute for their individual provenance.

Before writing, the command verifies that the Extracted Proposal and every expected Coordination Decision predecessor are still current. A stale Extracted Proposal, scope decision, plan
tail, or statement lineage refuses the whole Save and presents the newer state. It
does not overwrite or partly apply the user's work.

## The source constrains every correction

The original Extracted Proposal payload and its Supporting Documentation never change. If the extractor
missed the stated party or timing context, the accepted statement and Human Record Decision
receipt preserve the corrected values while retaining the Extracted Proposal exactly as
received. Confirmation alone cannot manufacture an External Organization fact. Verified
Supporting Documentation—including a separately verified quote when the original excerpt lacks
the necessary context—must support attribution and timing.

The coordinator corrects the supported facts, not a classification label. Corridor
derives a Commitment from one attributable timing and a Change to Promised Timing from
two attributable timings for the same party and commitment. If the Supporting Documentation does
not establish earlier or later direction, direction remains unknown. The UI shows
the derivation before Save and never invites a person to select an unsupported type.

The scope control uses human-readable locations and Constraint context, never
technical identifiers. Suggestions may order choices but cannot select them. The
coordinator explicitly chooses one Constraint, a selected set, all currently active
Constraints for the stated party as a recorded snapshot, or scope not yet known.

Choosing **Do not add** requires a structured reason and confirmation. It records
the Extracted Proposal disposition but creates no External Organization statement, scope decision,
or Follow-up Plan. The disposition remains reversible.

## Undo reverses the Save; Correct changes the record forward

Immediate **Undo** is an attributable compensating command over the exact grouping
receipt. It appends reversal records for every result of the guided Save, makes the
statement, scope decision, and Coordination Decisions noncurrent, and returns the original
Extracted Proposal to prioritized work. It deletes or mutates none of them.

Undo refuses atomically after any later act depends on a result of the Save. It does
not cascade through another person's work. The user then uses **Correct**, which may
target one fact without reversing unrelated decisions.

A scope correction appends a new scope decision on the same statement. A correction
to attribution or timing, including supported facts that change the derived type,
creates a linked successor statement in the same Commitment Lineage because it
changes the asserted External Organization fact.
The statement-level Follow-up Plan remains associated with the lineage, but a
material correction marks that plan for review under ADR-0038. The product never
silently treats an old response as appropriate for a changed fact.

## Considered options

**Save the statement first and the plan second.** Rejected. A partial failure would
contradict the single successful action the interface presented.

**Let a coordinator confirm missing attribution without more Supporting Documentation.** Rejected.
Human confidence is not a source quote and cannot turn an affected party into a
speaker.

**Let the coordinator choose Commitment versus Change to Promised Timing.** Rejected.
Those terms describe the timings Supporting Documentation supports, not a workflow preference.

**Update the Extracted Proposal with corrected values.** Rejected. It erases what the
extractor actually produced and makes evaluation and replay dishonest.

**Delete on Undo or cascade through dependent work.** Rejected. Deletion breaks the
record; cascading reversal can silently invalidate later decisions. Append-only
reversal is safe only before dependent work exists.

## Consequences

The guided command needs one transaction, optimistic predecessor checks, a grouping
receipt, explicit Extracted Proposal dispositions, and reversal records. Statement, scope,
Coordination Decision, key date, and Extracted Proposal readers must honor current/superseded state
rather than assuming physical deletion. Public behavior tests must cover total
rollback, stale refusal, whole-Save Undo, dependent-work refusal, targeted Correct,
Extracted Proposal restoration, and reversible Do not add.

## Post-foundation decision map

This ADR records the 2026-08-12 post-#217 decisions P6, P9-P12, and P24-P32:
preserving legacy Extracted Proposals; one workflow containing two distinct acts; atomic
Save; all four scope choices; append-only whole-Save Undo; Extracted Proposal restoration;
dependent-work refusal; scope versus statement correction; reversible Do not add;
attribution and timing supported by Supporting Documentation; immutable Extracted Proposal payload; derived
statement type and direction; and human-readable, non-selecting scope suggestions.
