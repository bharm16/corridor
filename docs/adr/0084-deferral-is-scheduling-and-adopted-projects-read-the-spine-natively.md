---
status: accepted
domain: record-inclusion
scope: current product
amends:
  - ADR-0076
  - ADR-0081
  - ADR-0083
amended_by:
  - ADR-0101
migration: the deferral receipt and the constrained-edit rules are implemented (#518, #519); the spine-native adopted-project readers and the #512 sequencing rule are not.
---

# Deferral is Work List scheduling, and adopted-baseline projects read the spine natively

**Amends ADR-0076, ADR-0081, and ADR-0083.**

The [late 2026-09-01 contract review](../research/implementation-contract-review-2026-09-01-late.md) found three clauses in the consolidation set that the implementation contracts could not satisfy as written. Each is a normative change, so each is recorded here rather than edited in place.

## 1. Deferral is scheduling, not a semantic disposition

ADR-0076 lists Resolve Delta as "accepts, edits, rejects, defers, or resolves", and ADR-0083 lists `deferred` among the resolved dispositions. A deferral says nothing about what the Project Record shows, so it cannot be a resolution.

- `accept`, `edit`, and `reject` are the **semantic delta dispositions**. Each writes one atomic Project Record revision (or, for `reject`, a decision that the accepted value stands) under ADR-0071.
- **`defer` is Work List scheduling.** The delta remains open. It is hidden from immediate work until its return date or a defined wake-up event (a newer source version for the same subject and field, a changed accepted value, or the deferral's own expiry). Deferral writes its own attributable receipt and **no Project Record revision**.
- A deferred delta is omitted from the chase list and the change summary's "resolved" section, and is shown in their "open" sections, until it returns.

### `edit` is constrained

An edited resolution may not turn unsupported free text into a source-backed value. An edit is permitted only when it:

- selects an existing captured Source Fact;
- composes supported Source Facts under a named, replayable transformation;
- performs a proven lossless normalization; or
- records a separate attributable source origin, such as a Recorded Verbal Statement (ADR-0082's verbal provenance class).

For Utility Owner, attribution, Promised For, completion, Applies To, agreement or permit status, and similar external facts, unsupported free text produces **Needs clarification** (an open Work Item), never an accepted value.

## 2. Non-verbal baseline and delta work precedes spine-native verbal origin

ADR-0081 names the spine-native Recorded Verbal Statement origin (stage 1, #512) as the first implementation task. The pilot criteria exclude Recorded Verbal Statements from the gated source population, so that ordering would block the paid slice on a source class the pilot does not measure.

- Non-verbal baseline and delta work (#492, the support-assessment relation, #518, #520, #509, #519) may precede #512.
- #512 remains mandatory before Recorded Verbal Statements join the target product and before full cutover (ADR-0081 stages 4 through 6).
- While #512 is open, verbals remain legacy-compatible project context and are neither captured as Source Facts nor compared as Proposed Deltas.

## 3. Adopted-baseline projects read the spine natively

ADR-0074 and ADR-0081 describe dual-write as the transitional rule for every human flow. That rule exists so legacy readers keep working for legacy projects. It does not require the new commercial model to be mirrored into legacy tables so that old development readers can see it.

- A **legacy project** (no adopted baseline) keeps its legacy readers and, during ADR-0081 stages 1 through 5, its compatibility dual-writes.
- An **adopted-baseline project** (#520) uses spine-native product surfaces: the Work List of Proposed Deltas (#494), the UCM export (#495), the change summary and weekly report, and the chase list (#425). These surfaces read the current and as-of projections only; there is **no mandatory legacy dual-write** for them, and no legacy reader is extended to show them.
- Legacy-project compatibility remains until #458 completes ADR-0081 stage 6.
- ADR-0081 stage 3's equivalence proof applies to legacy projects' surfaces that move readers; a spine-native surface with no legacy counterpart has nothing to prove equivalent to and is verified against its own contract.

## Consequences

- ADR-0076's Resolve Delta list reads: accept, edit, or reject; defer schedules. ADR-0083's lifecycle reads: open → resolved (accepted, edited, rejected) or superseded; deferred is an open state with a return condition.
- #519 carries the disposition and edit rules; #518 carries the deferral receipt; #425 and the change-summary issue carry the visibility rules.
- ADR-0081's "first implementation task" sentence is superseded by section 2 above; its stages and exit criteria are unchanged.
