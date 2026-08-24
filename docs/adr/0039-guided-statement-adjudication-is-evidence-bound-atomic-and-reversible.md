---
status: accepted
---

# Guided statement Adjudication is Evidence-bound, atomic, and reversible

An extractor may preserve a real External Party statement without enough supported
context to admit it mechanically. The coordinator may need to establish attribution,
recover a timing from another quote, choose Commitment Scope, and record the
project's response in one guided flow. Those are related but distinct acts. If they
write independently, a timeout or stale screen can leave an accepted statement
without the plan the user just saved, a plan without the statement it responds to,
or an Undo control that guesses which rows belong together.

Decision: **one guided Save is an atomic, Evidence-bound command whose distinct
results remain separately attributable and reversible as one act.** The accepted
External Party statement, its Evidence, scope decision, Candidate disposition,
Coordination Plan Work Decisions, exact Milestone links, and one grouping receipt
all commit, or none does. The grouping receipt identifies the exact results of the
Save; it is not a substitute for their individual provenance.

Before writing, the command verifies that the Candidate and every expected Work
Decision predecessor are still current. A stale Candidate, scope decision, plan
tail, or statement lineage refuses the whole Save and presents the newer state. It
does not overwrite or partly apply the user's work.

## The source constrains every correction

The original Candidate payload and its Evidence never change. If the extractor
missed the stated party or timing context, the accepted statement and Adjudication
receipt preserve the corrected values while retaining the Candidate exactly as
received. Confirmation alone cannot manufacture an External Party fact. Verified
Evidence—including a separately verified quote when the original excerpt lacks
the necessary context—must support attribution and timing.

The coordinator corrects the supported facts, not a classification label. Corridor
derives a Commitment from one attributable timing and a Committed Date Change from
two attributable timings for the same party and commitment. If the Evidence does
not establish earlier or later direction, direction remains unknown. The UI shows
the derivation before Save and never invites a person to select an unsupported type.

The scope control uses human-readable locations and Dependency context, never
technical identifiers. Suggestions may order choices but cannot select them. The
coordinator explicitly chooses one Dependency, a selected set, all currently active
Dependencies for the stated party as a recorded snapshot, or scope not yet known.

Choosing **Not relevant** requires a structured reason and confirmation. It records
the Candidate disposition but creates no External Party statement, scope decision,
or Coordination Plan. The disposition remains reversible.

## Undo reverses the Save; Correct changes the record forward

Immediate **Undo** is an attributable compensating command over the exact grouping
receipt. It appends reversal records for every result of the guided Save, makes the
statement, scope decision, and Work Decisions noncurrent, and returns the original
Candidate to prioritized work. It deletes or mutates none of them.

Undo refuses atomically after any later act depends on a result of the Save. It does
not cascade through another person's work. The user then uses **Correct**, which may
target one fact without reversing unrelated decisions.

A scope correction appends a new scope decision on the same statement. A correction
to attribution or timing, including supported facts that change the derived type,
creates a linked successor statement in the same Commitment Lineage because it
changes the asserted External Party fact.
The statement-level Coordination Plan remains associated with the lineage, but a
material correction marks that plan for review under ADR-0038. The product never
silently treats an old response as appropriate for a changed fact.

## Considered options

**Save the statement first and the plan second.** Rejected. A partial failure would
contradict the single successful action the interface presented.

**Let a coordinator confirm missing attribution without more Evidence.** Rejected.
Human confidence is not a source quote and cannot turn an affected party into a
speaker.

**Let the coordinator choose Commitment versus Committed Date Change.** Rejected.
Those terms describe the timings Evidence supports, not a workflow preference.

**Update the Candidate with corrected values.** Rejected. It erases what the
extractor actually produced and makes evaluation and replay dishonest.

**Delete on Undo or cascade through dependent work.** Rejected. Deletion breaks the
record; cascading reversal can silently invalidate later decisions. Append-only
reversal is safe only before dependent work exists.

## Consequences

The guided command needs one transaction, optimistic predecessor checks, a grouping
receipt, explicit Candidate dispositions, and reversal records. Statement, scope,
Work Decision, Milestone, and Candidate readers must honor current/superseded state
rather than assuming physical deletion. Public behavior tests must cover total
rollback, stale refusal, whole-Save Undo, dependent-work refusal, targeted Correct,
Candidate restoration, and reversible Not relevant.

## Post-foundation decision map

This ADR records the 2026-08-12 post-#217 decisions P6, P9-P12, and P24-P32:
preserving legacy Candidates; one workflow containing two distinct acts; atomic
Save; all four scope choices; append-only whole-Save Undo; Candidate restoration;
dependent-work refusal; scope versus statement correction; reversible Not relevant;
Evidence-supported attribution and timing; immutable Candidate payload; derived
statement type and direction; and human-readable, non-selecting scope suggestions.
