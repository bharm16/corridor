# Criticality is asserted by the document, not assigned by a reviewer

> **Superseded by [ADR-0009](0009-the-document-records-a-resolution-strategy-not-a-criticality.md).**
> The premise below — that TxDOT spells the signal `Potential Conflict = Y` — describes
> Project A's local form, not TxDOT's published template, which records a
> `Resolution Strategy Selected` instead. Neither SHRP2 R15B nor TxDOT defines
> criticality at all. Retained because its argument about *why* the signal must come
> from the document rather than the reviewer is the reason ADR-0009 exists, and still
> holds.

`dependencies.criticality` has existed since the v0 schema with the values `critical | high | normal`, no definition in `CONTEXT.md`, and `adjudicate.py` hardcoding `"normal"` on every accepted Candidate. All 141 Ledger records are `normal`. Meanwhile `v0-build-spec.md` gates M7 on *"critical-dependency recall (target ≥95%)"* and weights exception severity by *"rule severity × criticality"* — two things resting on a field nothing sets.

The obvious fix is to let a reviewer mark it during adjudication. **That makes the M7 gate unmeasurable by construction**, and the reason is worth stating slowly because it is easy to miss: a reviewer can only mark rows the extractor surfaced. Recall's denominator is *all* critical Dependencies, including the ones that were missed — and a missed row is never adjudicated, so it can never be labeled critical. Reviewer-assigned criticality yields a denominator that by definition excludes every failure the metric exists to detect. It would report high recall precisely when the extractor was worst.

So criticality must be judgeable from the **document**, independently of the extractor, or critical-recall measures the labeler instead.

**A Dependency is critical when its source document states that the facility must move before construction can proceed.** TxDOT spells this `Potential Conflict = Y`, against `N` and `A` for abandoned. WSDOT contract 9424 — the unsealed sibling of the M7 holdout — heads its columns `509 Relocation Needed`, `ST Relocation Needed`, `Retain and Protect`, `Abandon / Deactivate`: the same distinction in a different shape. FDOT SR 789 carries `Recommended Conflict Resolution` with values like *To be removed* and *To be adjusted*.

Mechanically it is an **ordinary adjudicated field**: the document asserts it, the reviewer confirms or overrides, and the stored value is the conclusion drawn from the Assertions. That is what `CONTEXT.md` already says a field value is, so no new machinery is needed — only the removal of the hardcode.

## Considered options

**Derived and never stored, following ADR-0002.** Tempting, because readiness works exactly that way and the precedent is strong. Rejected on the difference between the two claims: Ready is a claim *about evidence*, and letting a human assert it directly would let them skip the proof, which is the entire point of that ADR. Criticality is a claim about *importance*, and a reviewer overriding the document is legitimate rather than a cheat — the matrix may say `N` about a duct bank that happens to sit under the only haul road. Computing it would forbid the override that makes the field worth having.

**Reviewer-typed with no default.** What deleting the hardcode gets you by accident, and the worst option: the gate's denominator becomes a function of reviewer diligence, and an unlabeled row is indistinguishable from one judged `normal`.

## Consequences

`potential_conflict` is absent from 1,249 of Project A's 3,235 rows — the column appears only in three of the five revisions. Those Dependencies have no asserted criticality and need either a reviewer decision or an explicit unknown. An absent column must not read as `normal`, or the two failure modes this ADR separates collapse back together.

The definition is close to "the document flagged a conflict at all" — 1,419 of the 1,986 Project A rows that carry the column, or 71%. A gate whose critical set is most of the set may not catch the failure it was written for. That is a real weakness and it is the price of a criticality that is measurable at all; the alternative was one that measures nothing.

Each layout needs its criticality signal identified once, in writing, before that document is measured. For the sealed holdout this must be done by reading **9424**, never 9540 — which is what the sibling was fetched for (ADR-0006's reach argument applies to evidence about a layout, not only to extraction from it).
