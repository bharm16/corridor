---
status: accepted
domain: extraction
scope: current product
amends:
  - ADR-0006
migration: the reader package (#729), independent fixtures (#730), frozen measurement (#731), disabled adapters (#732 to #735) and remaining-call-site migration (#740) are implemented; coherent prose and cell segmentation (#736), semantic mapping (#737), reference integration (#738), qualification and selection (#447 and #739), provider-posture acceptance (#732) and final retirement (#741) remain. `ENGINE_ALLOWLIST` in `tests/test_architecture.py` still records retained incumbent imports.
---

# PDF facts come from the paired-rendition reader, scanned pages from Textract, and PyMuPDF and Tesseract leave the product

**Amends ADR-0006.** ADR-0064, ADR-0068 and ADR-0073 stand; this ADR reconciles the replacement with each of them below and moves none of their clauses.

**Maintainer acceptance, 2026-09-08.** The maintainer accepts this architectural
direction: the paired-rendition native reader, Textract for the declared OCR
role, preserved unconfirmed-reading and accepted-record authority boundaries,
and eventual removal of PyMuPDF and Tesseract. This acceptance does not select
a production configuration, waive historical-citation requirements, accept
unverified AWS controls, or authorize customer processing. #447, #739, #741
and #522 retain those responsibilities; provider-posture acceptance remains
recorded separately under #732. No production default changes through this
acceptance.

**This ADR changes the decision. It does not prove removal.** Both engines remain in the source, the dependency locks, CI and the runtime images until #741 removes them and proves it with an audit of the built image. Until then `tests/test_architecture.py` holds an exact allowlist of every module that still imports PyMuPDF or Tesseract; each ticket in #727 deletes the lines for the modules it moved, and #741 empties the list.

## Context

Corridor reads PDFs through PyMuPDF at every stage that touches a page: ingest, page inventory, native token layers, source segmentation, table geometry, locator verification, rendering, report release and the review surfaces. PyMuPDF and MuPDF are dual-licensed AGPL-3.0 or commercial (Artifex). Corridor is proprietary and is to be operated as a network service for customers, a distribution model #461 records as very likely incompatible with the AGPL's terms. #461 asked for a documented answer before any production selection, and on 2026-09-03 it recorded the commercial licence as the chosen option, reaffirmed on 2026-09-04, with procurement as the remaining act.

The maintainer changed that disposition on 2026-09-05, on evidence that did not exist when the licence was chosen. A standalone paired-rendition reader built on pypdfium2 and pypdf — native glyphs, boxes, rules, clip paths, marked content and the structure tree, with a deterministic table reconstructor — was measured against a 333-pair reference corpus, each pair a structured original and its printed rendition. It passes the exact gate on 262 of 263 development pairs and 70 of 70 holdout pairs (1,589 of 1,589 and 409 of 409 pages), and its cell-ID semantics tier matched 162 of 162 and 192 of 192 rows against the WSDOT 9424 and 9540 machine references. Those standalone measurements justified beginning integration. The reader package, frozen measurement and disabled native, inventory, render and Textract adapters have since merged; the remaining integration and qualification gates still do not establish a selected production pipeline.

Tesseract is a separate question that the same program answers. The local OCR engine and Amazon Textract were each measured, but on different probe populations, so their results do not rank the two engines against each other; each result keeps its own dataset, configuration and measurement identity. Textract was measured on the ten development pairs in three lanes that must never be collapsed into one number:

| Reading | Pairs passing | Pages passing | Exact reference cells |
|---|---:|---:|---:|
| Frozen native reader | 9/10 | 53/53 | 14,615/14,641 |
| Textract geometry + native glyphs | 0/10 | 25/53 | 14,342/14,641 |
| Clean scans, Textract words | 0/10 | 7/53 | 13,450/14,641 |
| Degraded scans, Textract words | 0/10 | 6/53 | 12,427/14,641 |

Textract has not earned verified scanned-cell transcription: 695 of the 901 mismatched cells in the clean-scan lane carried a mean word confidence of 95 or more. Confident and wrong is the common failure, and the unconfirmed-reading rule below is built on that fact.

## Decision

### Engines

**PDF facts come from the paired-rendition reader: pypdfium2, pypdf and the deterministic reconstructor.** Native glyphs, boxes, rules, clip paths, marked content, the structure tree and reconstructed tables come from that reader, imported unchanged from its measured commit (#729). **Scanned pages and image regions read through Amazon Textract.** **PyMuPDF leaves the product.** This is the disposition of #461: replacement, not procurement. Commercial procurement is paused. #461 stays open as the deployment gate and is blocked by #741; it closes by decision only when #741 proves the selected image no longer contains the dependency.

Paired renditions are the measurement method. They are not a prerequisite for reading a production PDF.

### Tesseract

**Tesseract leaves the product.** Its removal is a deliberate technology and provider decision, separate from PyMuPDF licensing; nothing about Tesseract's licence required it. The local OCR and Textract experiments used different probe populations and do not establish a head-to-head accuracy ranking. Their individual results retain their dataset, configuration and measurement identities. Textract remains selected for the declared role, with Textract-only values unconfirmed.

### Qualification versus commercial validation

The selection gate (#447) qualifies a named replacement configuration for the declared source classes and controlled deployment scope, recording processing cost and available handling-burden evidence; it does not claim validated customer savings. Partner-level economic validation remains #424 and #498 and is not a prerequisite for this replacement.

Four claims stay separate throughout the program: *we chose this provider*, *this reading is repeatable*, *this value is verified*, and *this configuration is approved for production*. No ticket, receipt or screen may let one stand in for another.

### Textract is the OCR provider, and a Textract-only value is unconfirmed

**Textract is the OCR provider** for image regions and OCR-routed pages. A value supplied only by Textract remains an **Unconfirmed reading** until the existing corroboration rules establish otherwise. Not corroboration: reading the same cached response twice, asking Textract again, a model repeating words it was given from Textract, checking a model transcription against those same words, a high confidence score. Token matching may detect disagreement between the model and OCR; it never proves the OCR value matches the document.

"Unconfirmed reading" is ADR-0064's second state: the value is present in the source, no reading of it is proven, it displays flagged, never contributes to Ready, and upgrades without ceremony the moment corroboration arrives. Textract's confidence is recorded as a signal, as OCR confidence already is in the token layers; it is never proof.

### ADR-0006 stands, with its engine clauses moved here

The model reads structure; the document supplies values. Tier 0 is unchanged: an authoritative spreadsheet remains the Preferred Source File and is read directly (ADR-0005). Tier 1 lists cells by ID: the model returns whether the page holds a Utility Conflict Matrix, which table is the matrix, the header row, the column-to-canonical-field mapping, page attributes as cell IDs, and refusals, and code fills every value from Source Segments (#737). Tier 2 transcription remains the last resort, and its OCR text is Textract's. The model still never writes a digit.

What this ADR moves out of ADR-0006 is exactly the engine. "Tier 1 leans on PyMuPDF finding table geometry" and "code then reads every cell value out of the page's word boxes using the existing geometry machinery" become: Tier 1 leans on the paired-rendition reader's reconstructed tables, and code fills every cell from the Source Segment that reader produced. Tier 2's "token-verified against OCR text" now names Textract as the source of that text and inherits the unconfirmed-reading rule above. Everything else in ADR-0006 — the three tiers, the verification gate on every tier, the loud fall to Tier 2, the rejected alternatives — is untouched.

### ADR-0064 stands

No transcription-review queue. Unresolved residue follows the bounded coordination process, such as requesting the structured source. Corroboration never silently replaces a material accepted UCM value; in adopted projects, captured source information and accepted Project Record changes stay separate (ADR-0076). ADR-0064's reading harness enumerates its reads by identity; Textract becomes a read identity under a declared profile, and diverse reads remain candidate generation, never proof.

### Evidence retention for cloud OCR, under ADR-0073

Token layers remain Class B artifacts with PostgreSQL manifests, and a durable citation never depends on a token row. For a cloud provider the class rules apply as follows: retain the provider observation and provenance necessary to verify a promoted reading (provider identity, the provider-reported model version, request identity, the raw-response digest and the normalized-reading digest); let unreferenced debug outputs and intermediates expire under existing policy; never claim an expired provider response can be regenerated identically by calling today's Textract; never overwrite an old OCR reading with a new response. A token layer is keyed by its content digest, so a later response is a later layer with its own manifest and its own expiry, and exact replay means replaying the retained response, never a fresh call.

### Locators

A native PDF cell locator binds the rendition identity and the reader/configuration identity, not only page, table, row and column, because a later reconstruction version may enumerate tables differently. A Fact continues to cite one rendition; cross-document corroboration remains a separate relationship or decision (ADR-0068, ADR-0069). The PDF cell locator is a new segment kind (#736); ADR-0068's workbook-cell and prose-span locators are unchanged.

### Provider posture versus customer authorization

The reusable Textract posture records retention, region, permissions, permitted purposes and the effective AWS AI-services opt-out configuration (without the opt-out, inputs may be used for service improvement and stored outside the selected region; a private bucket establishes nothing). The customer-specific #522 authorization accepts that posture per project, source class, purpose and region. No customer page may be transmitted without it.

The adapter enforces this at the outbound boundary itself (#732), before the network call, because routing is not the only caller: diagnostics, retries, shadow runs or a future caller could invoke it directly. A missing or mismatched authorization is a Processing Failure with the reason and zero outbound requests.

### Replacement licensing

pypdfium2 and PDFium are permissively licensed, and PDFium binary distributions carry dependency notices that must accompany distribution. #741 verifies the selected builds and notices.

## Considered options

**Procure the commercial Artifex licence (the 2026-09-03 disposition).** Paused, not rejected on its merits. It would have kept the incumbent engine and its fidelity, and a licence would leave Tesseract's separate question unanswered. The standalone measurements justify staged integration and reduce uncertainty about the native reader. They do not establish production routing, historical citation compatibility, deployed processing, or end-to-end correctness. Those remain subjects of the integration and selection gates. Procurement resumes only by a new decision, for instance if the selection gate refuses the replacement.

**A documented AGPL compliance posture.** Rejected. It is viable only if source-availability obligations are genuinely acceptable for the product's distribution model, and the maintainer chose against that twice: the licence on 2026-09-03 and replacement on 2026-09-05.

**The permissive fallback stack #461 originally listed (pypdfium2 for rendering, pdfplumber or pdfminer.six for characters, lines and tables).** Rejected as listed: fragmented, unmeasured, and slower. The paired-rendition reader is the measured form of the same permissive choice — one reader, one page shape, a deterministic reconstructor, and an exact gate it has passed.

**Keep Tesseract as a local OCR engine beside Textract.** Rejected. The role has one declared provider so that provenance, the posture record and the customer authorization name one thing, and two engines whose results were never measured against each other would invite exactly the head-to-head claim the experiments cannot support.

**Treat a high Textract confidence, a re-read of the cached response, or a model agreeing with the words it was given as corroboration.** Rejected, for the reason in the table above. ADR-0064 already says model agreement is never a Record Inclusion predicate; this ADR extends the same rule to the provider's own score and to every reading that merely repeats the provider.

**A transcription-review queue for Textract values.** Rejected; ADR-0064 forbids it and nothing here reopens it.

**Lower the native exactness gate so the scanned route can be described as passing it.** Rejected. The Textract route's failure to reach native exactness must not stall native integration, and the native gate is not lowered to describe the scanned route as passing.

## Consequences

- ADR-0006's engine clauses are amended as stated above, and ADR-0006 carries `amended_by: ADR-0094`. ADR-0064, ADR-0068 and ADR-0073 are cited, not amended.
- #461's disposition is replacement, not procurement; commercial procurement is paused; the issue stays open as the deployment gate, blocked by #741, and closes by decision with #741's audit receipt. The roadmap records the same.
- `tests/test_architecture.py` holds `ENGINE_ALLOWLIST`, the exact set of modules that still import PyMuPDF (`pymupdf` or its `fitz` alias) or Tesseract (`pytesseract`, or the engine named in a string literal: the executable, the routing engine identity, the harness read identity). The guard fails on an importer outside the list and on a listed module that no longer imports the engine named for it, so the list can only shrink honestly. Each ticket in #727 deletes the lines for the modules it moved; #741 empties it.
- No production default changes because this ADR is accepted, or because any child ticket merges. #447 owns native selection; #739 owns scanned selection through the same mechanism; the legacy path stays intact, and PyMuPDF and Tesseract leave source, dependencies, CI and runtime images last, after the selection gate.
- Old immutable runs, source records and released bytes are preserved. Old OCR readings are never overwritten by new responses.
- Implemented: the reader package (#729), independent fixture construction (#730), frozen paired-rendition measurement (#731), disabled Textract/native/inventory/render adapters (#732 to #735), and remaining-call-site migration (#740). Recorded compatibility and raster comparisons keep their limitations and exclusions; implementation is not production qualification.
- What remains unresolved: new reader-backed prose and PDF cell segmentation (#736), cell-ID semantic mapping (#737), method-specific reference integration (#738), native qualification and explicit selection (#447), scanned integration/qualification and explicit selection (#739), operational provider-posture acceptance (#732), customer authorization (#522), and verified retirement with historical citations preserved (#741). Retained rollback imports belong to #741, not to the completion of disabled adapters.
