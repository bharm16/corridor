---
status: accepted
domain: supporting-documentation
scope: current product
supersedes:
  - ADR-0077
amends:
  - ADR-0003
  - ADR-0017
migration: the support-assessment relation exists as support_assessments with its append command (#530), and Adopt Baseline (#509) and Resolve Delta (#519) cite it, but no policy or screen does yet; locator_validation_status is computed rather than stored, and EvidenceLink.verified is still the only locator column and is still shown to customers as "verified".
---

# Provenance follows the value class, and semantic support is a relation between a proposition and its sources

**Supersedes ADR-0077. Amends ADR-0003 and ADR-0017.**

ADR-0077 separated "the quotation is on the page" from "the source supports the value", which was right, and then required one four-link lineage for every published value: *Source says → Corridor proposed → person or policy decided → current record now shows*. The 2026-09-01 realignment review found three defects. The chain fits a source-backed accepted field and nothing else: a Coordination Decision has no source, a Derivation's provenance is a rule and its inputs, and a Recorded Verbal Statement's origin is a recorder's attestation, not a document proposal. Semantic support is not a property of a passage: one passage can support one field, contradict a second proposition, supply attribution for a third, and be irrelevant to a fourth, which is exactly why ADR-0001 refused to put asserted field meaning on evidence links. And a Human Record Decision about what the record shows is a different act from an assessment that a passage supports a proposition, even when one screen records both.

## Decision

### Provenance follows the value class

Every published value carries exactly one **valid provenance class**, and the class determines which layers exist. "No cell is bare" (ADR-0003) now means: no cell lacks the complete provenance its class requires.

| Value class | Required provenance |
|---|---|
| **Source-backed fact** (a field value drawn from a document, workbook, or message) | typed source locator (ADR-0068); `locator_validation_status`; at least one support assessment on the relation to its Source Segments; decision lineage (ADR-0071) |
| **Recorded Verbal Statement** | spine-native source origin: recorder, recorded time, conversation time, exact words and digest (ADR-0081 stage 1); recorder attribution (ADR-0033/0036); decision lineage. No document locator, so no locator validation beyond digest self-consistency |
| **Coordination Decision** (ADR-0025) | responsible human principal, the current subject, decision time, and the decision's own lineage. No source and no support assessment |
| **Derivation** (ADR-0003/0013) | rule identity and version, exact input record identities, evaluation time; each input drills to its own class |

Registry metadata (document identity, supersession, organization identity) keeps the authority path its own ADR defines (ADR-0015, ADR-0030, ADR-0051) and is not forced into any of the four.

### Locator validation

`locator_validation_status` (`valid`, `invalid`, `not_checked`) records whether a typed locator dereferences to the stored text or value. It is mechanical and replayable and says nothing about the claim. `EvidenceLink.verified` survives only as a compatibility projection of `locator_validation_status == valid`; no new code reads it, and it is removed under ADR-0081's writer cut-over.

### Semantic support is a relation

A **support assessment** is a typed relation between **one proposition** (a typed fact, an Extracted Proposal, a Proposed Delta, or an accepted field value) and **one or more Source Segments**, carrying:

- a **role**: `value_support`, `attribution`, `timing`, `scope`, or `context`;
- an **assessment**: `supported`, `partially_supported`, `contradicted`, `unclear`, `not_assessed`;
- who recorded it: a human principal, or a released policy for the exact classes it is proven on (ADR-0050); and when.

It is never a column on an EvidenceLink or a Source Segment. The existing Assertion (ADR-0001), which already binds one field to one evidence link, is the first carrier of this relation for legacy source-backed fields; on the spine the relation is its own row keyed by proposition and segment.

### Support assessment is distinct from the record decision

A Human Record Decision (ADR-0070/0074) decides what the accepted record does. A support assessment records whether a passage supports a proposition. One guided Save may write both in one transaction, and each keeps its own identity, actor, and reversal path. A policy that projects a value automatically (ADR-0076) must cite the support assessment it relied on; it may not infer support from locator validation.

### Customer-facing language

"Verified" is removed from customer-facing displays unless it names the object: "quotation verified on page 4" is permitted, a bare "verified" beside a value is not. The lineage shown for a value is its class's lineage:

- source-backed: *Source says → Corridor proposed → person or policy decided → current record now shows*;
- verbal: *Recorded by [person] on [date] → person decided → current record now shows*;
- Coordination Decision: *[Person] decided on [date] for [subject]*;
- Derivation: *Computed by [rule, version] from [n] records as of [time]*, drilling to each record.

## Amendments

- **ADR-0003.** "Document, page, and verified quote" is restated as the source-backed class above; Derivation provenance is unchanged and is now one of four named classes. The verifier checks class-complete provenance, not a fixed link shape.
- **ADR-0017.** "`verified` stays mechanical" is restated as `locator_validation_status`. The role-scoped Supporting Documentation in Use resolver may consult support assessments when choosing between candidates and must never treat locator validation as support.

## Considered options

**Keep ADR-0077 and add exceptions for decisions and derivations.** Rejected. An exception list is the same defect stated twice; the class is the primary key.

**Store support on the Source Segment with a per-field map.** Rejected. It recreates the JSON-payload shape ADR-0067 retired and makes the assessment unattributable per proposition.

## Consequences

- ADR-0077 becomes `superseded by ADR-0082`; #493 is limited to locator validation and the compatibility projection until the support relation exists.
- Extraction Measurement reports locator validity and support accuracy separately, per class.
- Coordination Report and export templates label each cell's provenance class.
