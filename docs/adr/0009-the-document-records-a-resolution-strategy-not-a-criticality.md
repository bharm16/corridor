# The document records a resolution strategy, and criticality is a reading of it

**Supersedes [ADR-0007](0007-criticality-is-asserted-by-the-document.md).**

ADR-0007 held that a Dependency is critical when its document says the facility must move, and that each agency spells that differently — *"TxDOT spells this `Potential Conflict = Y`"*. That sentence is wrong, and the way it is wrong changes the model rather than a value in it.

TxDOT's own [Utility Conflict Analysis Template](https://www.txdot.gov/content/dam/docs/division/row/utl/utility-conflict-analysis-template.xlsx), published by its ROW division, has no `Potential Conflict` column. It has `Resolution Strategy Selected (from Resolution Alternatives)`, described in the template's own data dictionary as *"strategy that was selected to resolve the utility conflict"*. Project A's form is a local variant, and its filename says what it is: `nhhip-seg3c2-utilities-**inventory**`.

**Neither TxDOT's template nor [SHRP2 R15B](https://onlinepubs.trb.org/onlinepubs/shrp2/R15BTrainingMaterials/Lesson3UtilityConflictMatrix.pdf), the method this project's extractor was built against, has a criticality, priority or severity field at all.** The discipline does not rank conflicts on a scale. It records three things: what the **resolution strategy** is, what the **conflict status** is (`Identified | Confirmed | Resolved | Cleared`), and what the facility's **operational status** is (`In Service | Abandoned in place`).

R15B enumerates four resolution alternatives, and they are worth quoting because two of this corpus's layouts print them almost verbatim:

> Relocation before construction · Protect in-place · Change highway design · Exception to policy

So criticality is not a property the documents assert. **The resolution strategy is, and criticality is a reading of it**: a Dependency is critical when the strategy is relocation.

## What the corpus actually carries

| layout | column | what it is |
|---|---|---|
| FDOT SR 789 | `Recommended Conflict Resolution` | the strategy, as prose |
| WSDOT 9424 / 9540 | `509 Relocation Needed` · `ST Relocation Needed` · `Retain and Protect` · `Abandon / Deactivate`, under a spanning header reading `RECOMMENDED RESOLUTION` | the strategy, as four marked columns |
| TxDOT NHHIP 3C-2 | `Potential Conflict (Yes, No, Abandoned)` | conflict **existence**, with operational status folded into `A` |
| TxDOT SH 99 | `AURL or DBA` · `Early TxDOT Utility Activity` | **who** relocates and **when**, not which strategy |

Only two record a strategy, and Project A is not one of them. It is an inventory with a conflict flag — it identifies conflicts and never says how they resolve. ADR-0007 built the whole model on the one layout that cannot answer the question.

## Where the line falls, and why not the federal one

[23 CFR § 645.105](https://www.law.cornell.edu/cfr/text/23/645.105) defines the term:

> "Relocation is the **adjustment** of utility facilities required by the highway project. It includes removing and reinstalling the facility … moving, rearranging or changing the type of existing facilities **and taking any necessary safety and protective measures**."

Federally, adjustment *is* relocation, and protective measures are inside it too. Adopt that line and 64 of SR 789's 66 rows are critical, `Retain and Protect` is a relocation, and the metric separates nothing.

That definition is scoped to **reimbursement** — it bounds what federal-aid dollars may pay for. It is not answering the schedule question, which is the only one a readiness ledger asks.

So the line is SHRP2's, and WSDOT prints it as column headings: **does the facility move, or does it stay?** `Relocation Needed` is critical. `Retain and Protect` and `Abandon / Deactivate` are not.

## Considered options

**Keep criticality and widen the signal table.** What ADR-0007 shipped, extended to cover WSDOT's four columns. Rejected because it preserves a three-value severity scale (`critical | high | normal`) that no document in the corpus fills in and no published method defines. `high` has never had a source and never will — a scale nothing can assert is a reviewer's opinion wearing a document's clothes, which is the exact failure ADR-0007 was written to prevent.

**Adopt the federal definition.** Legally exact, defensible to an auditor, and it makes almost every row critical. ADR-0007 already named this failure — *"a gate whose critical set is most of the set may not catch the failure it was written for"* — and then accepted a 71% critical set anyway. Taking the federal line would make it worse, not better.

**Derive criticality and store nothing.** Rejected by ADR-0007 on the grounds that a reviewer overriding the document is legitimate, unlike readiness. That argument survives and is why `resolution_strategy` is stored and adjudicated: the reviewer overrides *the strategy* — the matrix may say `Retain and Protect` about a duct bank under the only haul road — and criticality follows from the conclusion. The override moved fields; it did not disappear.

## Consequences

**Projects A and C assert nothing.** 4,636 of 4,703 extracted rows carry no resolution strategy, because their documents record none. SR 789's 66 rows are the only measurable data until WSDOT 9424 is ingested — it is fetched (`corpus/wsdot-9424.lock.json`) but has never been through `make ingest`. This is a real loss of coverage and it is the honest reading: an inventory is not a conflict matrix.

**M7's gate is unaffected and better founded.** The gate scores WSDOT 9540, which carries the four resolution columns. It was never going to be scored on Project A.

**A gold set's `critical` column now has a definition.** Mark a row critical when its document says the facility is to be moved, removed or relocated. Not when it is to be adjusted in place, monitored, protected, or abandoned. The eval code (#87) is unaffected — it scores whatever the labels say — but the labeller now has a rule instead of an instinct.

**`severity = rule severity × criticality` needs restating.** A derived binary cannot be a three-point multiplier.

**The canonical vocabulary is aimed at the wrong document.** `ROW_FIELDS` was derived from Project A's inventory, so SH 99 — which uses TxDOT's official template verbatim — has four standard fields with no canonical home: `Utility Subtype` (whose values include `Highly Volatile Liquid`, `Crude Oil`, `Carbon Dioxide`), `Placement Relative to Existing ROW`, `Abandoned`, and the resolution strategy itself. ADR-0005 saw the same template from the other side and noted it carries `Resolution Status` and columns no PDF here has. This is the root of the per-page mapping instability in #91: the model wavers precisely where the vocabulary has no home to offer.
