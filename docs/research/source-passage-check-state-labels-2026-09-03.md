# Customer labels for the Source Passage Check states

**Date:** 2026-09-03. **Question raised by:** [#493](https://github.com/bharm16/corridor/issues/493), which requires every presentation of the mechanical quote-on-page flag to name the **Source Passage Check** with its passed, failed, or not-run state. **Procedure:** [Research before proposing terminology](../agents/domain.md#research-before-proposing-terminology).

## 1. The concept already has an adopted name

"Source Passage Check" is not new. It was adopted by [ADR-0048](../adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md) from the [complete glossary review](glossary-terminology-review-2026-08-27.md#additional-19-source-passage-check) and is defined in the [Corridor Operations glossary](../operations/CONTEXT.md): *a check that a Cited Passage is present in the identified source page or row, under the stated matching method and its limits.* That review already recorded the finding that **no exact construction-wide term was found** for this concept and that the wording is a scoped plain-language product label. Reusing it unchanged needs no further naming exercise (procedure step 1).

What was missing is the vocabulary for the check's **outcome**, which #493 must print beside values.

## 2. What the primary sources supply

**Machine identifiers — W3C.** [PROV-CONSTRAINTS](https://www.w3.org/TR/prov-constraints/) is the source already cited for this area. It defines *valid* as the name for an instance that satisfies the constraint checks: "This document defines a subset of PROV instances called *valid* PROV instances, by analogy with notions of validity for other Web standards", and notes that the intent of validation "is to ensure that a PROV instance represents a consistent history". It names no formal outcome state for the negative case and none at all for a check that was not run. This corroborates the stored identifiers `valid` / `invalid` / `not_checked` that [ADR-0082](../adr/0082-provenance-follows-the-value-class-and-support-is-a-relation.md) fixed for `locator_validation_status`; it does not supply customer words.

**Highway-agency practice — no counterpart.** The responsible-agency sources record the result of an inspection or review in compliance language, not as a named check state:

- [TxDOT Construction Contract Administration Manual, Control of the Work, Section 3: Inspections](https://www.txdot.gov/manuals/cst/cah/control_of_the_work/inspections-cachgagd.html) records results as work "constructed in general compliance with plans and specifications". It names no result state and no term for an inspection that was not performed.
- [FHWA, Recommended Framework for a Bridge Inspection QC/QA Program](https://www.fhwa.dot.gov/bridge/nbis/nbisframework.cfm) uses "acceptable performance" and "out-of-tolerance" for review findings, and again names nothing for a check that was not performed.

Pass/fail is ordinary vocabulary for a control point on a construction quality-control checklist, but it is a checklist convention rather than a defined term in either agency source, and neither source is about documentary citation checking. **There is no industry counterpart to adopt** (procedure step 6).

## 3. Proposal

Plain-language product wording, matching the words #493 itself uses for the three states, applied at the presentation boundary only:

| Stored identifier (unchanged) | Customer label | Meaning |
|---|---|---|
| `valid` | **Passed** | The typed locator was dereferenced and recovered the stored passage or value from its registered source. |
| `invalid` | **Failed** | The locator was dereferenced and did not recover it — the passage is not where the citation says it is. |
| `not_checked` | **Not run** | There was no locator to dereference, so no check happened. Distinct from a check that ran and failed. |

"Not run" rather than "Not applicable": the check applies to any cited passage; the fact being recorded is that it has not been performed, not that it could never apply.

**Concrete example.** A utility owner's relocation letter is cited on page 4 for a Promised For date. The Source Passage Check reads **Passed** when that exact sentence is on page 4 of the registered file. It reads **Passed** whether the letter supports the date, contradicts it, or merely mentions it — that reading is a separate Support Assessment with its own recorded assessor ([ADR-0082](../adr/0082-provenance-follows-the-value-class-and-support-is-a-relation.md), #530). A cell with no cited document at all reads **Not run**.

## 4. Boundaries preserved

- The check is mechanical and says nothing about the claim. Nothing may present a passed check as support, as Supporting Documentation in Use, or as Record Inclusion; those stay visibly distinct.
- "Verified" leaves customer-facing displays except where it names the object ("quotation verified on page 4"). The column `evidence_links.verified` keeps its name for [ADR-0048](../adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md) compatibility as the projection `locator_validation_status == valid`; its removal is scheduled under #458.
- A passed check is not Documentation Review, physical inspection, Completion Reported, or Contract Acceptance.

## 5. Open item for the maintainer

Procedure step 6 asks for explicit user agreement where the sources give no counterpart. The three words below are the ones #493's own acceptance criteria use, so they are recorded as the product wording; a maintainer who wants different customer words should say so before the labels reach a customer artifact.
