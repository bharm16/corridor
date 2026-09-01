---
status: accepted
domain: supporting-documentation
scope: current product
amends:
  - ADR-0003
  - ADR-0017
migration: locator_validation_status and semantic_support_status do not exist yet; EvidenceLink.verified is still the only field and is still shown to customers as "verified".
---

# Locator validation and semantic support are separate evidence layers

**Amends ADR-0003 and ADR-0017.**

`EvidenceLink.verified` means exactly one thing: the quotation appears on the cited page. ADR-0017 and the `models.py` module docstring both say so. It is still too easy to read as substantive validation. A customer who sees "verified" beside a Promised For date reads that the date is right; a developer who sees `verified = True` in a policy predicate reads that the claim is supported. Neither is what the flag asserts. Before more policies are built on that flag (ADR-0076 requires replayable proof for every automatic projection), the word has to stop carrying two meanings.

## Decision

Evidence for a value in the Project Record is described in **four explicit layers**, each stored and displayed separately.

**1. Typed source locator.** Where the evidence is, in the original bytes. Locators stay typed (ADR-0068):

- workbook digest, sheet, and cell range;
- document digest, page, and polygon or token span;
- email digest, MIME part, and byte offsets;
- recorded-verbal identity, recorder, and exact words (ADR-0081 makes this a spine-native source origin, not a legacy statement reference).

**2. `locator_validation_status`.** Whether the locator dereferences to the stored text or value: `valid`, `invalid`, `not_checked`. This is what `verified` meant. It is mechanical, replayable, and says nothing about the claim.

**3. `semantic_support_status`.** Whether the located source supports the recorded value: `supported`, `partially_supported`, `contradicted`, `unclear`, `not_assessed`. A released policy may set it only for the exact classes it is proven on; otherwise it is set by a Human Record Decision or left `not_assessed`. It is never inferred from layer 2.

**4. Decision lineage.** Who or what decided that the record shows this value, and when (ADR-0071's decision with principal or released policy, revision, and superseded-by).

`EvidenceLink.verified` is retained only as a compatibility view equal to `locator_validation_status == valid`. No new code reads it; existing readers migrate to the status column and the compatibility view is removed under ADR-0081's writer cut-over.

### Customer-facing language

The word "verified" is removed from customer-facing displays unless it names the object being verified: "quotation verified on page 4" is permitted; a bare "verified" beside a value is not. The customer-facing lineage for any value reads, in this order:

> Source says → Corridor proposed → person or policy decided → current record now shows.

Each arrow is a link to the layer behind it: the located passage, the proposed delta (ADR-0076), the decision with its principal, and the current projection.

### Amendments

- **ADR-0003.** "Document, page, and verified quote" as the Assertion's provenance is restated as: typed locator, locator validation status, semantic support status, and decision lineage. "No cell is bare" is unchanged; a bare cell now means a cell missing any of the four.
- **ADR-0017.** "`verified` stays mechanical: the quote is on the page, nothing more" is restated as layer 2, under its own name. The role-scoped Supporting Documentation in Use resolver is unchanged; it may consult layer 3 when choosing between candidates and must never treat layer 2 as support.

## Considered options

**Rename `verified` to `quote_on_page`.** Rejected. It fixes layer 2's name and leaves layer 3 unrepresented, so the next policy would invent its own support flag.

**One combined status enum.** Rejected. A value can have a valid locator and contradicted support, or an invalid locator on a supported value whose page was re-rendered. Two independent facts need two columns.

## Consequences

- New policies and readers reference `locator_validation_status` and `semantic_support_status`; none may branch on `verified`.
- Coordination Report, Work List, and export templates lose the bare word "verified" and gain the lineage sentence.
- Extraction Measurement (ADR-0008, ADR-0023) reports citation validity as layer 2 and support accuracy as layer 3, separately.
