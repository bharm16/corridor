# #740: the remaining PyMuPDF call sites, classified by risk

Each receipt records what was compared, on which bytes by SHA-256, with which
reader versions, and the result. An explained difference is recorded as a
difference; it is not a pass.

| Receipt | Tier | Call site(s) | Compared on | Result |
|---|---|---|---|---|
| `740-released-pdf-integrity.json` | high (ADR-0040) | `report_release._validate_party_statement_pdf_context`, `sh99_coordinator_rehearsal._require_report_pdf_contents` | the two retained sealed SH99 exports (`artifacts/product-proving/sh99-9a4342d-two-pass-passed`) | pass: the normalized visible text both checks read is byte-identical between PyMuPDF and pypdf on 154 of 154 pages; each check's outcome on the same bytes and inputs is the same |
| `740-acceptance-captures.json` | medium | `m8_acceptance._document_observation` (page sizes), `m8_acceptance._rid_rows` | the six retained NHHIP capture sources (`tests/fixtures/m8_acceptance/nhhip-five-revision-v1`) | pass: sizes identical on every source and equal to what the capture recorded; RID rows identical on every page but one glyph (soft hyphen against hyphen-minus) that the caller's normalization drops |
| `740-storage-baseline.json` | medium | `storage_baseline._freeze_pdf` | the two retained sealed SH99 exports the v1 baseline froze | pass under the v3 definition: the v1 digests are reproduced by the old reading on the same bytes; v1's MuPDF block-sorted text is reproduced by no other reader (0 of 154 pages), so v3 freezes whitespace-normalized content-order text, on which PyMuPDF and pypdf are byte-identical (154 of 154) |
| `740-intake-draft-page-texts.json` | medium | `source_intake_draft._page_texts` | 193 corpus PDFs (1,246 pages) by digest, the three plan sets over 30 MB skipped | mismatch: PDFium's page text equals PyMuPDF's after the validator's normalization on 744 of 1,246 pages (same words in any order on 956); pypdf on 693. The replacement reads customer PDFs differently from MuPDF; the receipt characterizes how |

The low tier (`product_proving_frontend_capture` PNG dimensions through
Pillow, `web/queue.locate_quote` highlights through PDFium text search) has
no corpus receipt: each is verified by a test against fixtures that declare
their own dimensions and word boxes. `current_record._pdf_text` (the
equivalence gate's release-text comparison) is likewise verified by a fixture
test and by the gate's own run on WeasyPrint bytes.

`gold.py` was not moved: both of its opens hand a PyMuPDF page to
`geometry.page_tables`, the table-detection seam #736 owns, and its output is
the `pymupdf-table-grid` machine-reference identity #738 owns.
