---
status: accepted
---

# The gold denominator and the Ledger's admission rule are not yet one rule

"What counts as a row" is implemented four times — `vocabulary.is_retired_row`, the extractor's admission sequence in `extract_matrix._structure_candidates`, `gold.author_machine_gold`, and `gold.prepare` — and answering "what is in the denominator?" means reading all four. An architecture review raised unifying them; this records why the unification has not happened, so the next review does not re-propose it without the constraint.

They differ in two ways, and both are real:

- **`is_retired_row` is called with a synthesised dict.** `author_machine_gold` passes `{"utility_id": ref, "notes": notes}` where the extractor passes the row's mapped fields. A retirement phrase printed in any other column is invisible to the gold side and would silently reclassify the row.
- **There is no `inherited` counterpart.** The extractor admits a row whose External Party arrives from the page header (`fields = {**inherited, **own}` before the `REQUIRED` check); the gold authoring sees `not owner` and counts an empty slot. Every such row would then be surplus in the eval, and unrecognised or spurious rather than matched — the hazard `is_retired_row`'s own docstring was written about.

Neither is reachable on the corpus as it stands. `author_machine_gold` reads WSDOT's Appendix U, which prints an owner column, so nothing inherits; and on contract 9424 the retirement phrase appears in the Notes column, which the synthesised dict carries. Measured before deciding: 9424 authors 162 rows / 97 retired / 2 empty slots and 9540 authors 192 / 0 / 0, which are the numbers the recorded artifacts hold.

The constraint is [ADR-0008](0008-a-holdout-is-spent-once-on-a-complete-measurement.md). `gold/wsdot-9540.machine.csv` is the denominator of a measurement that has been made, and a holdout is spent once. Any change to the admission rule can move that denominator, and a denominator that moves after the fact is a measurement re-scored rather than recorded. Making the change requires re-authoring against 9424 — the unsealed twin fetched for exactly this purpose — confirming the counts above are unchanged, and saying so in the same commit. That is deliberate work with a verification step, not a refactor to be applied in passing.

## Consequences

Whoever generalises gold authoring past WSDOT's Appendix U must fix both divergences first, because a header-owner layout makes them fire on the same run that introduces it. The safe order is: unify the rule against 9424, prove the counts hold, and only then reach a layout that inherits.
