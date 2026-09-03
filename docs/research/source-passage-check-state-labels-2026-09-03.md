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

## 3. Proposal, not accepted

> The maintainer rejected these three words on 2026-09-03; the approved labels and the reasons are in [section 5](#5-open-item-for-the-maintainer-settled-2026-09-03). This section is kept as the record of what was proposed.

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

## 5. Open item for the maintainer, settled 2026-09-03

Procedure step 6 asks for explicit user agreement where the sources give no counterpart. The three words in section 3 are the ones #493's own acceptance criteria use, so they were recorded as the product wording pending a maintainer who wanted different customer words.

**The maintainer approved different words on 2026-09-03** ([#600](https://github.com/bharm16/corridor/issues/600)). Section 3's proposal is rejected and these are the customer labels:

| Stored identifier (unchanged) | Approved customer label |
|---|---|
| `valid` | **Found at cited location** |
| `invalid` | **Not found at cited location** |
| `not_checked` | **No cited location recorded** |

Full presentation: **Source Passage Check — Found at cited location**.

Why the placeholder was rejected:

- **Passed and Failed read as a verdict.** Each implies that the evidence supports the proposition, or that the value is correct, or that some underlying work passed an inspection. The check means none of those things: it dereferences a typed locator and reports whether the stored passage was there ([ADR-0082](../adr/0082-provenance-follows-the-value-class-and-support-is-a-relation.md)). The approved words name a place and say whether the passage was at it, so there is no verdict left to misread.
- **"Not run" misdescribes the mechanism.** The status is computed by replaying the locator at read time, not written by a job that could be skipped, so `not_checked` generally means there was no locator to evaluate — which is what "No cited location recorded" says and "Not run" did not.

Section 2's finding stands: the primary sources still supply no counterpart, so these remain scoped plain-language product wording carrying explicit maintainer approval, not an adopted industry term. The concrete example in section 3 reads the same way with the approved words — the letter's passage is **Found at cited location** whether it supports the date, contradicts it, or merely mentions it.

The correction is presentation only. The stored identifiers `valid`, `invalid`, and `not_checked` are unchanged, the check stays mechanical and replayable, and a found passage is still not a Support Assessment. No successor ADR is written: the procedure permits a wording correction once the missing-counterpart decision is explicitly approved, provided it alters neither authority nor lifecycle, and this alters neither. [ADR-0082](../adr/0082-provenance-follows-the-value-class-and-support-is-a-relation.md) and #493 stand as they are.
