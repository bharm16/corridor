---
status: accepted
---

# The document records a resolution strategy, and criticality is a reading of it

**Supersedes [ADR-0007](0007-criticality-is-asserted-by-the-document.md).**

Amended by [ADR-0010](0010-an-exception-is-a-fact-to-filter-not-a-score-to-rank.md): the consequence below that `severity = rule severity × criticality` "needs restating" was first answered by restating the multiplier as ×3, and later abolished — severity is retired entirely, and Criticality filters Exceptions rather than weighting them. Everything else stands.

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

## Where the line falls

[23 CFR § 645.105](https://www.law.cornell.edu/cfr/text/23/645.105) defines the term, and [WSDOT's Utilities Manual](https://www.wsdot.wa.gov/publications/manuals/fulltext/m22-87/Utilities.pdf) glossary adopts it verbatim:

> "Relocation is the **adjustment** of utility facilities required by the highway project. It includes removing and reinstalling the facility … moving, rearranging or changing the type of existing facilities **and taking any necessary safety and protective measures**."

Federally, adjustment *is* relocation and protective measures are inside it too. Adopt that line and 64 of SR 789's 66 rows are critical, `Retain and Protect` is a relocation, and the metric separates nothing.

**It would be wrong to dismiss that as a cost-only definition.** FDOT's [Utility Relocation Schedule Manual](https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/construction/schedulingeng/fdot-utility-production-rate-manual.pdf) — a manual about *scheduling* — carries `WA03 Adjust one water valve box to grade` and `SA08 Adjust ring and cover to grade` as relocation activities with durations, crews and permitting factors. Adjustment is scheduled relocation work in the agency's own scheduling practice. The federal definition is not merely a reimbursement artifact.

Two things separate them anyway, and both are published rather than inferred.

**FDOT distinguishes them on the plans, in colour.** Its Utility Accommodation Manual §4.9 codes existing utilities as:

> **Red:** to be removed or relocated **horizontally**, or to be placed out-of-service (deactivated) but left in place
> **Brown:** to be adjusted **vertically** but to remain in the same horizontal alignment
> **Green:** to remain in place with no adjustment

**And the magnitudes are not comparable.** From FDOT's own duration table: adjusting a valve box to grade averages **0.5 days** — the shortest of the manual's 22 activities, by a factor of four — against 8 days to encase a high-pressure gas line and **28 days** (up to 40) to remove and replace fifty feet of force main. Whatever the reimbursement rules call them, a half-day casting adjustment and a six-week main replacement are not the same schedule risk, and a readiness ledger exists to track the second.

So the line follows FDOT's Red/Brown boundary, which is also SHRP2's and which WSDOT prints as column headings. Note that **deactivation is on the Red side**: an abandoned facility is scheduled utility-owner work, not a facility that stays. Project A agrees, giving `Abandoned` its own answer rather than folding it into `No`.

| strategy | critical | basis |
|---|---|---|
| relocate · remove · abandon/deactivate | **yes** | FDOT Red · WSDOT `Relocation Needed` |
| adjust vertically | no | FDOT Brown · a 0.5–1 day activity |
| protect in place | no | FDOT Green · WSDOT `Retain and Protect` |
| change highway design · exception to policy | no | no utility-owner work at all |

This decomposes R15B's single `Relocation before construction` alternative along FDOT's published Red/Brown split. It is an extension of the four, not a departure from them.

**A caveat on "before construction".** WSDOT's manual is explicit that the timing is a goal rather than a guarantee: *"The primary goal of any project utility conflict should be to relocate the utility before construction begins. However, this is often not possible when utility relocation is dependent upon the acquisition of right of way or the construction of a highway element such as a utility conduit on a bridge."* Criticality is therefore about the **kind of work the document commits the utility owner to**, not about a date. Some critical relocations provably happen mid-construction.

**And a contradiction that is only apparent.** WSDOT's glossary defines relocation broadly while its own Appendix U prints `Relocation Needed` as distinct from `Retain and Protect` — and `retain and protect` appears **zero times** in the manual's 283 pages. The glossary is the legal and cost term; the matrix columns are the operational one. Corridor reads documents, not manuals, so the document's narrower line governs.

## Considered options

**Keep criticality and widen the signal table.** What ADR-0007 shipped, extended to cover WSDOT's four columns. Rejected because it preserves a three-value severity scale (`critical | high | normal`) that no document in the corpus fills in and no published method defines. `high` has never had a source and never will — a scale nothing can assert is a reviewer's opinion wearing a document's clothes, which is the exact failure ADR-0007 was written to prevent.

**Adopt the federal definition.** Legally exact, defensible to an auditor, adopted verbatim by WSDOT, and — as FDOT's scheduling manual shows — genuinely used for scheduling rather than only for cost. It is the strongest of the rejected options and was rejected on consequence rather than on principle: it makes 64 of SR 789's 66 rows critical and turns `Retain and Protect` into a relocation. ADR-0007 already named this failure — *"a gate whose critical set is most of the set may not catch the failure it was written for"* — and then accepted a 71% critical set anyway. Taking the federal line would make it worse, not better.

**Derive criticality and store nothing.** Rejected by ADR-0007 on the grounds that a reviewer overriding the document is legitimate, unlike readiness. That argument survives and is why `resolution_strategy` is stored and adjudicated: the reviewer overrides *the strategy* — the matrix may say `Retain and Protect` about a duct bank under the only haul road — and criticality follows from the conclusion. The override moved fields; it did not disappear.

## Consequences

**Projects A and C assert nothing.** 4,636 of 4,703 extracted rows carry no resolution strategy, because their documents record none. SR 789's 66 rows are the only measurable data until WSDOT 9424 is ingested — it is fetched (`corpus/wsdot-9424.lock.json`) but has never been through `make ingest`. This is a real loss of coverage and it is the honest reading: an inventory is not a conflict matrix.

**M7's gate is unaffected and better founded.** The gate scores WSDOT 9540, which carries the four resolution columns. It was never going to be scored on Project A.

**A gold set's `critical` column now has a definition, and it is the table above.** Mark a row critical when its document says the facility is to be relocated, removed, or abandoned in place. Not when it is to be adjusted vertically to grade, monitored, protected in place, resolved by a design change, or excepted from policy. The eval code (#87) is unaffected — it scores whatever the labels say — but the labeller now has a rule instead of an instinct.

The labelling rule and the Ledger's derivation are the same sentence deliberately. If they diverge, the gate scores one definition against another and the number means nothing — which is this ADR's own subject matter, and a footgun it very nearly stepped in: an earlier draft of this paragraph excluded abandonment while the table above included it.

**A document that has not decided is not a critical row.** The rule reads what the document *settled on*, not what words it contains. SR 789 prints `To be adjusted or relocated` on five rows and `To be monitored and adjusted as needed` on seven: the first names a strategy from each side of the line and the second commits to nothing yet. Neither is critical, neither is not-critical, and both must be left unlabelled — a labeller who marks them critical because the word "relocated" appears would move six of that document's 66 rows, which is more than a ≥95% bar can absorb. The Ledger declines the same phrases for the same reason (`adjudicate.RESOLUTION_VOCABULARIES`), and the two lists have to be read together.

**`severity = rule severity × criticality` needs restating.** A derived binary cannot be a three-point multiplier.

**The canonical vocabulary is aimed at the wrong document.** `ROW_FIELDS` was derived from Project A's inventory, so SH 99 — which uses TxDOT's official template verbatim — has four standard fields with no canonical home: `Utility Subtype` (whose values include `Highly Volatile Liquid`, `Crude Oil`, `Carbon Dioxide`), `Placement Relative to Existing ROW`, `Abandoned`, and the resolution strategy itself. ADR-0005 saw the same template from the other side and noted it carries `Resolution Status` and columns no PDF here has. This is the root of the per-page mapping instability in #91: the model wavers precisely where the vocabulary has no home to offer.
