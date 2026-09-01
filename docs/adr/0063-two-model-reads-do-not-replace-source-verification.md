---
status: superseded by ADR-0064
domain: extraction
scope: historical
---

# Two model reads do not replace source verification

> Its proposed second-read workflow never shipped: ADR-0064 replaced the design before any implementation.

A scanned page can have no usable OCR text because its image is skewed, degraded, or obstructed. Two independent model calls can expose disagreement and prepare a bounded comparison for a person, but their agreement is not Supporting Documentation and cannot replace the citation check: ADR-0042 expressly excludes model agreement as a Record Inclusion predicate. This proposed decision keeps that boundary while defining the only safe use of a second reading.

## Decision

An **OCR-failed page** is a technical processing condition, not a customer or Project Record status. A page may enter the second-read path only when a named, versioned, human-approved eligibility profile records the page rendition and all of the following facts:

1. the registered page image is the exact rendition to be read;
2. the retained OCR result is missing or fails the profile's declared deterministic quality checks;
3. the normal text-layer/geometry path cannot supply the field tokens used by Source Passage Check; and
4. no current Supporting Documentation already supplies the proposed value.

The profile must declare its quality checks, version, page scope, model/configuration identities, independent-call requirements, time/token/request budgets, and failure handling before it can run. Credentials, a page image, an OCR warning, or an operator's ad-hoc request do not enable it.

The two reads must be independent executions over the same pinned image and prompt/configuration contract. Their comparison is exact only when each proposed value has the same field identity, byte-for-byte normalized value under a named canonicalization rule, and identical source-page locator. The retained comparison receipt records both raw readings, the normalization/version, page bytes, configurations, budgets, and outcome.

- A disagreement, missing value, failed call, exhausted budget, stale page, or invalid receipt writes no Project Record fact and produces one visibly pending human work item. That item shows the page image and both readings side by side.
- Exact agreement may produce a non-authoritative comparison receipt and a clearly labelled suggested transcription for the ordinary guided human flow. It does **not** pass Source Passage Check, establish Supporting Documentation, admit a Constraint or statement, settle a discrepancy, or make a Documentation Review field Ready.
- Ordinary scanned pages continue through the existing single-read-plus-citation-check path. The second-read path must never widen itself to pages that do not meet the declared profile.

## Consequences

Ticket #369 may implement the bounded comparison and human-review path only after this decision is accepted. It cannot implement the ticket body's proposed automatic substitution of model agreement for citation verification without a later accepted ADR that explicitly supersedes ADR-0042's confidence rule and defines a replayable non-model proof. Tests must include a garbled-scan disagreement fixture, strict profile refusal, stale-byte refusal, and proof that exact agreement alone creates no authoritative state.

## Considered options

**Treat agreement as citation verification.** Rejected: independent calls can share a semantic or transcription error; agreement is confidence, not a document-supplied value or replayable deterministic proof.

**Ignore OCR-failed pages.** Rejected: it hides an actionable gap. A bounded, attributable comparison can make the unresolved work legible without inventing authority.
