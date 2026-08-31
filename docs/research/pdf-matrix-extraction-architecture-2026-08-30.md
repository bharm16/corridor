# PDF utility-conflict matrix extraction architecture

**Date:** 2026-08-30
**Scope:** Research and architecture recommendation only. No product behavior is changed by this report.

## Recommendation

Corridor should keep its central design decision from [ADR-0006](../adr/0006-the-model-reads-structure-the-document-supplies-values.md): a model may interpret document structure, but document-derived tokens must supply field values. The current implementation does not enforce that boundary strongly enough on hard pages. When PyMuPDF does not return table geometry, Corridor falls back to a vision model transcribing every value. That is exactly the error surface the tiered architecture was intended to eliminate.

The target should be a **multi-representation, cell-first pipeline**:

1. prefer a verified structured original as the **Preferred Source File**;
2. preserve the exact PDF **Document Rendition** and inspect its native text, glyph coordinates, images, vector paths, page boxes, and rotation;
3. render pinned page and region images for OCR, layout analysis, and reviewer display, using separate resolutions for those jobs;
4. fuse native tokens and OCR tokens by coordinates instead of choosing one page-wide text string;
5. detect tables and reconstruct cells with at least two interchangeable engines during evaluation;
6. let a model map source header cells and page labels to Corridor's canonical fields, but require it to return source-cell identifiers rather than copied values;
7. validate and materialize values deterministically from those source cells;
8. preserve field-level page, bounding-box, token, cell, engine, configuration, and **Extraction Run** provenance;
9. emit a lossless derived table artifact plus source-cited **Extracted Proposals**; and
10. leave **Current Production Run** selection and **Record Inclusion** outside extraction and outside any agent.

The leading implementation hypothesis is **PyMuPDF for native PDF facts and rendering, plus an adapter-based table/layout layer that evaluates Docling and PaddleOCR PP-StructureV3 side by side**. Azure Document Intelligence Layout and Amazon Textract are credible cloud benchmark lanes if project data rules permit them. None of those products has demonstrated Corridor accuracy. Vendor feature lists and vendor benchmark numbers are not evidence that utility-conflict matrices will be read correctly.

## Claim discipline

This report separates three kinds of statements:

- **Documented capability** means the owning specification, project, or service documents the interface or behavior.
- **Architecture inference** means the capability appears useful for Corridor given the current code and ADRs.
- **Corpus hypothesis** means Corridor must measure it on its own matrices before choosing or shipping it.

No model, OCR engine, table recognizer, or cloud service should be described as “accurate for Corridor” until it passes the staged corpus evaluation below.

## Corridor language and authority

The relevant boundaries are already explicit in the context map and glossaries:

- A **Document** can have several file representations of the same revision; each is a **Document Rendition** with exact file identity. A spreadsheet and PDF are not automatically independent sources ([Corridor Operations glossary](../operations/CONTEXT.md#document)).
- The **Preferred Source File** is the registered rendition used for citations when several verified renditions exist; the structured original is preferred to its printed rendering, but this is not a legal-authority designation ([Project Record glossary](../../CONTEXT.md#preferred-source-file), [ADR-0005](../adr/0005-the-structured-original-is-the-document-of-record.md)).
- An **Extraction Run** is one preserved attempt over exact input and configuration. A **Current Production Run** is explicitly selected, not inferred from “latest” or from apparent success ([Corridor Operations glossary](../operations/CONTEXT.md#extraction-run)).
- An **Extracted Proposal** is source-cited proposed information, not an accepted Project Record fact. A **Source Passage Check** establishes only that a cited passage is present under a stated matching method; it does not prove the claim true ([Corridor Operations glossary](../operations/CONTEXT.md#extracted-proposal)).
- **Record Inclusion** is the separate act that records a supported fact through a permitted human decision or an exact deterministic rule. Model confidence, model agreement, and an agent's judgment are not Record Inclusion predicates ([ADR-0042](../adr/0042-authority-follows-proof-policy-writes-exact-cases-agents-assist-and-humans-decide-ambiguity.md#confidence-is-not-proof)).
- A tool timeout, invalid output, exhausted budget, or incomplete read is a **Processing Failure**, not a successful abstention and not an empty matrix ([Corridor Operations glossary](../operations/CONTEXT.md#processing-failure)).
- **Supporting Documentation** is the verified part of an identified source Document used to support a recorded fact, with exact page or row reference; an extraction artifact is not Supporting Documentation by itself ([Project Record glossary](../../CONTEXT.md#supporting-documentation)).

Those boundaries should remain unchanged.

## 1. Why render a PDF to pixels when the bytes already exist?

The original bytes and a rendered image answer different questions. The bytes are the preserved source identity. A render is a derived view of what a conforming viewer paints.

PDF is primarily a page-description format. Its content streams can contain text-showing and text-positioning operators, vector paths, raster images, and marked content; the standard does not require a visible table to be an embedded spreadsheet or even to carry logical table structure. Adobe's PDF reference separates text-showing, text-positioning, path, image, and marked-content operators, which is why extracting characters is not equivalent to recovering rows and cells ([Adobe PDF Reference, operator categories](https://opensource.adobe.com/dc-acrobat-sdk-docs/pdfstandards/pdfreference1.3.pdf#page=138)). ISO 32000-2 is the current core specification and is available through the PDF Association ([PDF Association, ISO 32000-2](https://pdfa.org/resource/iso-32000-2/)).

PyMuPDF's own documentation makes the practical consequence explicit: plain PDF text may not be in normal reading order, while word and block extraction adds position information; a visible table is usually ordinary text plus graphical arrangement, and table recovery requires locating boundaries and assigning text to them ([PyMuPDF text extraction](https://pymupdf.readthedocs.io/en/latest/recipes-text.html#how-to-extract-all-document-text), [PyMuPDF table explanation](https://pymupdf.readthedocs.io/en/latest/recipes-text.html#how-to-extract-table-content-from-documents)).

### Page classes

| Page class | What the PDF bytes can supply directly | Why pixels are still needed | Correct Corridor treatment |
| --- | --- | --- | --- |
| Digital text PDF | Character strings, fonts, spans/characters/words, coordinates, vector rules, page geometry, and sometimes tagged logical structure | A layout or vision model may need the painted arrangement; a reviewer needs the visible page; native content order may differ from visual order | Use native tokens for values. Render for layout interpretation, overlays, and review. Do not OCR merely because a render exists. |
| Image-only scan | Embedded raster image(s), placement, crop, and page geometry, but no authoritative character tokens | OCR and image-based layout engines require pixels. Tesseract accepts images, and OCRmyPDF rasterizes pages before OCR ([Tesseract command-line usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html), [OCRmyPDF processing](https://ocrmypdf.readthedocs.io/en/latest/introduction.html#what-ocrmypdf-does)) | Render or safely extract the page image at an evaluated resolution, then OCR with word/polygon coordinates. Keep the original PDF unchanged. |
| Vector-drawn or outlined text | Paths and small vector graphics may paint letters without recoverable text characters | OCR sees the painted letters after rasterization. PyMuPDF itself lists “thousands of small vector graphics” as a signal that OCR may be useful ([PyMuPDF OCR guidance](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html#how-to-ocr-a-document-page)) | Retain vector rules for possible table boundaries, but render the text regions for OCR. Do not classify the page as blank merely because native text is absent. |
| Mixed page | Some regions have good native text; other regions are scans, stamps, rasterized table fragments, or bad OCR layers | One page-wide choice discards useful evidence. Pixels are needed only for the non-native or suspect regions and for visual layout | Maintain native and OCR tokens together with origin and coordinates. Route by region when possible; fuse at cell reconstruction. |
| Reviewer display | The exact source bytes remain the authority, but reviewers do not reason over content streams | A visible page, crop, zoom, and source highlight are needed to inspect the cited value in context | Store a stable review rendition or render on demand from pinned bytes. A low-resolution thumbnail is not a sufficient zoom source for tiny table text. |

Rendering is therefore necessary in three cases: **machine vision**, **OCR**, and **human display**. Those are separate products of the same source and should not be forced to share one fixed 150-DPI PNG. PyMuPDF supports direct DPI-controlled rendering and cropped high-resolution pixmaps ([PyMuPDF image recipes](https://pymupdf.readthedocs.io/en/latest/recipes-images.html#how-to-increase-image-resolution)). OCRmyPDF likewise warns that OCR quality depends on input resolution and offers oversampling, while image preprocessing can alter content and must be reviewed ([OCRmyPDF cookbook](https://ocrmypdf.readthedocs.io/en/latest/cookbook.html#improving-ocr-quality)). The correct DPI and preprocessing profile for Corridor are corpus hypotheses, not constants that can be selected from documentation.

### What rendering does not do

Rendering does not make the image a new independent source, recover hidden workbook structure, or prove that OCR text is correct. It creates a derived rendition tied to the same Document Revision. The pipeline must retain the source PDF digest, renderer/version, page boxes, rotation, crop, DPI, color mode, render digest, and coordinate transform so every pixel bounding box maps back to source-page coordinates.

## 2. Assessment of Corridor's current route

### What the implementation actually does

Current ingestion:

- hashes and preserves the source file, never writing back to it;
- renders every PDF page once at `RENDER_DPI = 150`;
- calls `page.get_text()`;
- invokes Tesseract through `pytesseract.image_to_string()` only when stripped native text is under 50 characters;
- replaces native text with OCR only when the OCR string is longer; and
- silently returns an empty OCR result on any OCR exception ([`ingest.py`](../../src/corridor/ingest.py#L31-L39), [`_extract_pages`](../../src/corridor/ingest.py#L320-L363)).

Current matrix extraction:

- reopens the stored PDF and runs geometry only on pages whose stored `text_source` is exactly `text_layer`;
- uses PyMuPDF `find_tables()` plus rotated word boxes to reconstruct cells;
- asks a strict-output model to identify the matrix table, header row, page-scoped External Organization, and canonical column mapping;
- derives row values from reconstructed cells when geometry exists;
- sends the page image to a model that transcribes all row fields and quotes when geometry does not exist; and
- records structure/transcription tier, unmapped columns, quote checks, field-token checks, and row accounting ([`extract_matrix.py`](../../src/corridor/extract_matrix.py#L276-L445), [`_read_geometry`](../../src/corridor/extract_matrix.py#L449-L471)).

Current verification normalizes text and performs a fuzzy row-quote check at 0.9 for print-derived text, then checks whether every field token of at least four characters appears somewhere in the page token set. It deliberately does not prove that a value came from the intended cell ([`verify.py`](../../src/corridor/verify.py#L53-L76), [`unverified_fields`](../../src/corridor/verify.py#L163-L195)).

### What should be preserved

The current route has several strong properties:

- exact input hashing and immutable source bytes;
- one-based human page numbers;
- a rendered page beside cited text;
- rotation-aware word-box geometry;
- a model mapping header meaning rather than retyping ordinary Tier 1 cell values;
- explicit unknown/unmapped fields rather than synonym guessing;
- no extractor write to the Project Record;
- field-token and quote checks that demote suspect Extracted Proposals;
- recorded extraction tier, model/prompt lineage, and complete row dispositions; and
- fail-closed distinction between no matrix, page failures, blank rows, skipped rows, and extracted rows.

The existing measurements justify keeping the division of labor. On Project A, full-page vision transcription produced 164 rows with values not on the page, versus 3 for word-box geometry; the tiered path reproduced all 3,235 parser rows exactly and still reached the 66-row FDOT SR 789 layout the synonym parser could not map ([M6 validation gate](../m6-validation-gate.md#the-finding-the-same-rows-less-accurately-transcribed), [second gate](../m6-validation-gate.md#second-gate-the-tiered-extractor-adr-0006)). Those results are historical corpus evidence for this repository, not a general benchmark.

### What is wrong

| Current choice | Problem | Consequence | Required change |
| --- | --- | --- | --- |
| “Under 50 native characters” means OCR | Character count does not measure visible-text coverage, correctness, extractability, or table-region quality. A scan with a 60-character digital header is missed; a valid sparse page may be OCRed; a broken or invisible OCR layer may exceed 50 characters; vector-outlined text may yield zero. | Wrong page-wide route, especially on mixed pages. | Build a deterministic page/region inventory from native glyph coverage, image coverage, vector density, Unicode quality, overlap, visibility, and table-region evidence. |
| One 150-DPI image serves OCR, model vision, and reviewer display | These jobs have different resolution and storage needs. Small matrix characters may need higher effective resolution; a reviewer needs tiled zoom, not a permanently low-resolution image. | Avoidable OCR/model misses and weak evidence display. | Keep a compact review thumbnail if useful, but generate pinned OCR/layout renders and high-resolution crops under versioned profiles. Select profiles through evaluation. |
| OCR replaces native text only when it is longer | More text is not better text. Length ignores confidence, coordinates, character error, duplication, and whether OCR covers the table. | A longer wrong OCR stream can win; a shorter accurate table-region read can lose. | Keep native and OCR token sets separately. Fuse them at cell level using geometry and quality rules. |
| OCR exceptions return `""` | Missing dependencies, engine crashes, corrupt images, and timeouts disappear into an apparently thin native-text page. | A technical failure can masquerade as a successful ingest path. | Record a Processing Failure with engine/configuration and page scope. Never convert engine unavailability into “no OCR needed.” |
| Geometry runs only when `text_source == "text_layer"` | A mixed page whose OCR string happened to replace native text loses all usable native word boxes. OCR pages can still contain native labels and vector table rules. | The entire page falls to model transcription even when many cells could be reconstructed from native tokens. | Inventory all representations independently. Table reconstruction should consume native words, OCR words, vector rules, and image layout together. |
| Default `find_tables()` is the sole table-structure gate | PyMuPDF documents line-, strict-line-, and text-based strategies plus many tolerances; the current call uses only defaults. More importantly, no single geometric detector covers borderless, merged, raster, or irregular tables. ([PyMuPDF `find_tables`](https://pymupdf.readthedocs.io/en/latest/page.html#pymupdf.Page.find_tables)) | “No table found” conflates detector limitation with absent structure. | Treat PyMuPDF as one table hypothesis generator. Add dedicated layout/table recognition and compare or select hypotheses deterministically. |
| No geometry means generative transcription of all values | This reintroduces the measured 5.1% page-transcription error class. Schema-conformant output can still contain wrong digits, wrong cell assignments, or copied values from another row. | Source-looking but incorrect Extracted Proposals on the hardest pages. | Use OCR engines for character tokens and table recognizers for cells. A model may map structure, but it must not author field strings. |
| Strict output is treated as part of extraction safety | OpenAI Structured Outputs ensures JSON-schema conformance, and strict function tools enforce argument shape; neither proves semantic correctness ([OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [strict function calling](https://developers.openai.com/api/docs/guides/function-calling#strict-mode)). | A perfectly valid JSON mapping can be wrong for every row beneath a header. | Validate mappings against repeated headers, source dictionaries, allowed field cardinality, and corpus gold. Keep unknown mappings unknown. |
| Field validation is a page-level bag-of-tokens check | A token can exist in the wrong row, wrong cell, repeated header, legend, or adjacent table. Tokens shorter than four characters are intentionally not checked. | Wrong association can pass even when every string appears somewhere on the page. | Require every materialized field value to trace to token IDs geometrically assigned to its source cell, with explicit transformations for combined cells. |
| Candidate provenance is page/quote oriented | The current Candidate does not retain the exact source cell, cell polygon, contributing tokens, table hypothesis, render, OCR/native origin, or reconstruction rule for each field. | Review and later comparison cannot distinguish “right text, wrong cell” from a sound read. | Add field-level provenance in the derived table artifact and make Extracted Proposals reference it. |
| A page silently degrades from structure to transcription | Tier recording makes degradation visible after the fact but does not decide whether the lower-assurance path is eligible for a production run. | Novel layouts can produce apparently completed runs through the least trustworthy route. | A versioned eligibility profile must decide whether a fallback can emit verified values, unconfirmed readings, or only a Processing Failure. |

There is also a conceptual problem with the claim that model vision and page text are “independent representations.” They are different renderings of the same source, which is useful, but not independent evidence. On Tier 1, the same native tokens supply and verify field values. On Tier 2, OCR and the vision model read the same pixels and may share image-quality failure modes. ADR-0064 has the right boundary: a degraded read becomes an **unconfirmed reading** unless a readable source supplies corroboration; agreement between machine readers is not Supporting Documentation ([ADR-0064](../adr/0064-unreadable-cells-resolve-by-corroboration-not-review.md)).

## 3. Recommended modern pipeline

### A. Source and rendition selection

1. Register the Document, revision, exact Document Rendition bytes, digest, source, retrieval facts, and declared relationship to other renditions.
2. If a verified equivalent spreadsheet or other structured original exists, designate it the Preferred Source File and read cells natively. Do not rasterize it to recover values already present in cells.
3. Do not infer equivalence or supersession from filename, upload time, row similarity, or format. A PDF and spreadsheet can be separate revisions or incomplete exports.

The prior source-availability research found that Corridor must retain a full PDF path: current data-carrying matrices are PDFs, while the one registered matrix spreadsheet is a blank form; structured files are realistic to request but not safe to expect from public portals ([spreadsheet source availability](spreadsheet-source-availability-2026-08-28.md#what-this-means-for-the-proposed-onboarding-policy)).

### B. PDF preflight and page inventory

For every page, preserve:

- media/crop/trim boxes, rotation, user-unit scale, and page label;
- native blocks, lines, spans, characters or words with quads/bounding boxes and font/Unicode information;
- embedded images and their placement;
- vector paths and line segments;
- tagged structure when present, without assuming it is complete;
- native text coverage over visible page area;
- suspicious-text signals: invalid or replacement characters, extreme overlap, invisible text, duplicated layers, implausible coordinates, and very low visible coverage;
- raster-image coverage and vector-density signals; and
- all extractor and library versions.

PyMuPDF is a good fit for this stage. It exposes positioned words/blocks, page rendering, drawings, images, rotation transforms, and table hypotheses. Its documentation explicitly says plain extraction may not be in reading order and that words/blocks carry positions ([PyMuPDF text recipes](https://pymupdf.readthedocs.io/en/latest/recipes-text.html)). It should remain a low-level PDF adapter, not be treated as a complete document-understanding pipeline.

### C. Purpose-specific rendering

Produce derived renders from pinned source bytes under versioned profiles:

- a review rendition or tiles suitable for visual zoom;
- an OCR/layout page render at an evaluated effective DPI; and
- higher-resolution region crops for tiny or degraded cells.

Every render must preserve a transform between PDF page coordinates and image pixels. Do not preprocess the reviewer image destructively. Deskewing, denoising, background removal, and contrast transforms should create additional identified derivatives, because OCRmyPDF warns that cleanup can remove desirable content or introduce artifacts ([OCRmyPDF image processing](https://ocrmypdf.readthedocs.io/en/latest/cookbook.html#image-processing)).

### D. Native and OCR token layers

Maintain separate token layers rather than one `DocPage.text` winner:

```text
source_token_id
document_rendition_id
page_no
origin = native_text | ocr
engine + version + configuration
raw_text
normalized_text
polygon_in_pdf_coordinates
polygon_in_render_coordinates (when applicable)
confidence_or_quality_signals
render_id (for OCR)
```

For image-only, vector-text, and suspect regions, run one or more OCR adapters. Tesseract can emit hOCR and TSV with word coordinates and confidence information, and can create a searchable text layer ([Tesseract output formats](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)). OCRmyPDF is useful when Corridor wants a minimally altered derived searchable PDF, page analysis, rotation/deskew, sidecar text, or a controlled OCR layer; it is not a table-structure recognizer, and its documentation states that Tesseract provides text and bounding boxes but not headings or document structure ([OCRmyPDF limitations](https://ocrmypdf.readthedocs.io/en/latest/introduction.html#limitations)).

OCRmyPDF output, if retained, is a derived Document Rendition for processing or display. It does not replace the exact source PDF and is not automatically the Preferred Source File.

### E. Layout and table hypothesis generation

Run layout/table adapters over the pinned page render and native PDF facts. During evaluation, at minimum compare:

- PyMuPDF `find_tables()` under relevant strategies;
- Docling's standard PDF pipeline with native cells, OCR, layout detection, and TableFormer; and
- PaddleOCR PP-StructureV3 / table-recognition pipeline.

The adapters should return a common page-local contract:

```text
table_hypothesis_id
page_no
table_polygon
engine + model/version + configuration
rows, columns
cell_id
row_index, column_index, row_span, column_span
cell_polygon
header/body/group classification
contributing native/OCR token_ids
unassigned token_ids
quality signals and warnings
```

Documented capabilities make both dedicated open-source candidates credible, but not proven:

- PaddleOCR PP-StructureV3 combines document preprocessing, OCR, layout detection, and table recognition; its result includes table cell boxes, recognized cell text, predicted HTML, JSON, and Markdown exports ([PP-StructureV3 usage and result fields](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PP-StructureV3.html)).
- Docling's model catalog documents layout detection, multiple OCR engines, and TableFormer fast/accurate table-cell recognition; `DoclingDocument` supports tables, reading order, bounding boxes, and provenance, with lossless JSON export ([Docling model catalog](https://github.com/docling-project/docling/blob/main/docs/usage/model_catalog.md), [DoclingDocument](https://github.com/docling-project/docling/blob/main/docs/concepts/docling_document.md)).

Do not collapse multiple hypotheses by “highest confidence.” Apply deterministic structural checks: token coverage, non-overlap, row/column consistency, repeated-header agreement, plausible cell topology, table boundary coverage, and complete row accounting. If no hypothesis meets a released profile, record a Processing Failure or unresolved table instead of invoking generative transcription.

### F. Cell reconstruction and source-value selection

1. Assign native and OCR tokens to candidate cells by polygon overlap and reading order within the cell.
2. Prefer trustworthy native tokens for values when they cover the cell. Use OCR tokens only for uncovered or deterministically suspect regions.
3. Preserve both raw token sequence and normalized display value. Normalization must be versioned and reversible.
4. Record every split or combination. For example, parsing `135+58.68, 236.85' LT` into station, offset, and side may be legitimate, but all output fields must reference the same source cell and named transformation.
5. Keep blank, absent, ditto/merged, unreadable, and unconfirmed distinct. Do not turn an empty OCR result into an absent source value.
6. When no source token reading is proven, apply ADR-0064: retain the best reading as unconfirmed with full provenance, exclude it from Ready, and seek corroboration in another readable Document rather than asking a person to transcribe it.

A generative model may never be the source of a materialized value. It may propose which token or cell is relevant; code copies the source string from the referenced token/cell.

### G. Cross-page reconstruction

Build page-local tables first. Then stitch fragments through deterministic evidence such as:

- matching repeated header-cell structure;
- explicit continuation labels;
- compatible column geometry after coordinate normalization;
- stable page-scoped labels;
- table boundary at page break; and
- row completeness.

Do not infer that adjacent pages are one table merely because they have the same width or similar values. Preserve page-local cell identities even after stitching.

### H. Semantic column and page-label mapping

Give a model the page image plus the reconstructed header/group cells and page-scoped labels. Its strict output should contain only references and canonical meanings, for example:

```json
{
  "is_utility_conflict_matrix": true,
  "table_hypothesis_id": "table:p14:1",
  "column_mappings": [
    {"source_header_cell_ids": ["cell:p14:1:r0:c3"], "canonical_field": "station_from"}
  ],
  "page_attribute_mappings": [
    {"source_token_ids": ["native:p14:w27", "native:p14:w28"], "canonical_field": "external_org"}
  ],
  "unmapped_header_cell_ids": [],
  "warnings": []
}
```

The model does not return `station_from: "1149+00"` or copy the organization name. The deterministic materializer dereferences the source IDs and builds fields. Strict output is still useful for schema integrity, but semantic mapping quality requires Corridor evaluation. Repeated-header consensus can detect disagreement; it cannot prove a systematically wrong mapping.

### I. Deterministic field validation

Validation should run before an Extracted Proposal is materialized:

- every field references one or more source cells/tokens on the same registered Document Rendition;
- source polygons lie inside the declared page and table/cell polygons;
- materialized strings equal the source strings under a named, replayable transformation;
- station, offset, date, mark-column, identifier, and controlled-vocabulary syntax checks are field-specific;
- required fields and minimum row shape are checked without treating page-inherited values as row evidence;
- duplicate identifiers are reported, not assumed impossible;
- unknown columns remain unmapped;
- row and cell accounting is complete;
- no Processing Failure is represented as an empty matrix; and
- unreadable source values remain unconfirmed rather than “verified by model agreement.”

The Source Passage Check should be strengthened from page-level token presence to a field-level source-cell check. A row quote remains useful for human context but should not be the sole machine proof of value placement.

### J. Outputs and provenance

One completed Extraction Run should preserve:

- exact source and render digests;
- all engine, model, prompt, schema, normalization, and routing versions;
- page inventory and routing decisions;
- native and OCR token layers;
- table hypotheses and the selected hypothesis with deterministic reasons;
- the reconstructed derived table artifact;
- semantic mappings by source IDs;
- every Extracted Proposal and its field-level provenance;
- all blank/skipped/unconfirmed/failed row and cell dispositions;
- latency, cost, retries, and resource budgets; and
- a Processing Failure when the contract did not complete.

Only an eligible explicitly declared Current Production Run supplies current work. Extraction completion alone must not select it and must not perform Record Inclusion.

## 4. Should a tool-using agent orchestrate PDF operations?

**Possibly, but only as a bounded read-only recovery/orchestration layer, and only if it beats a deterministic router in a corpus evaluation. It should not be the default architecture assumption.**

The Responses API supports custom function tools with strongly typed arguments, tool restrictions through `tool_choice`, and strict schemas ([OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create), [function calling](https://developers.openai.com/api/docs/guides/function-calling)). The Agents SDK supports a manager retaining ownership while calling specialists as bounded tools, plus tool guardrails and approval interruptions ([Agents orchestration](https://developers.openai.com/api/docs/guides/agents/orchestration), [guardrails and review](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)). OpenAI's PDF file-input path sends both extracted text and page images to vision-capable models, which is convenient but does not expose Corridor's required token/cell provenance contract by itself ([OpenAI file inputs](https://developers.openai.com/api/docs/guides/file-inputs#how-it-works)).

### Allowed tools

An agent may call only project-scoped, read-only functions over pinned bytes and immutable intermediate artifacts:

| Tool | Permitted result |
| --- | --- |
| `inspect_pdf_page(page_no)` | Page boxes, rotation, native-token summary, image/vector coverage, and quality signals |
| `render_page(page_no, profile_id)` | Identified render under a pre-approved profile; no arbitrary shell arguments |
| `render_region(page_no, bbox, profile_id)` | Identified crop bounded to the page and maximum resolution/area |
| `extract_native_tokens(page_no)` | Native token IDs, text, quads, fonts, and vector/table hints |
| `run_ocr(page_or_region, engine_profile_id)` | OCR token IDs, text, polygons, confidence/quality, and Processing Failure details |
| `detect_layout(page_no, engine_profile_id)` | Region hypotheses and polygons |
| `recognize_table(region_id, engine_profile_id)` | Table/cell hypotheses with source token assignments |
| `compare_table_hypotheses(ids)` | Deterministic coverage/topology differences, not a model winner |
| `validate_table_hypothesis(id, policy_version)` | Deterministic pass/fail/reasons |
| `read_related_sources(query_contract)` | Bounded registered Document facts or previous-revision cell candidates for ADR-0064 corroboration |
| `propose_semantic_mapping(table_id)` | Canonical-field mapping that references source header cells/tokens only |

The agent must not receive generic filesystem, shell, network, database-write, Extracted-Proposal-write, Current Production Run, or Record Inclusion tools. Text inside Documents is untrusted data, never instructions.

### Controls the agent does not own

The following remain deterministic application logic:

- source digest verification and the binding of every tool result to one registered Document Rendition;
- allowed engine profiles, tool arguments, page/region bounds, concurrency, retries, and budgets;
- coordinate transforms and source-token identity;
- native/OCR value selection and normalization;
- table-hypothesis eligibility;
- cell assignment and value dereferencing;
- field validation, Source Passage Check, and row/cell accounting;
- whether a result is verified, unconfirmed, absent, or a Processing Failure;
- and creation of immutable Extraction Run records and Extracted Proposals from validated source references.

The following remain separate attributable authority decisions under Corridor's existing rules, never agent decisions:

- Document/Document Revision/Document Rendition relationships and Preferred Source File designation;
- Current Production Run eligibility and explicit selection; and
- Record Inclusion and all Project Record writes.

### Agent output contract

The agent may return a non-authoritative orchestration result containing:

- ordered tool-call receipts;
- page/region processing choices with bounded reasons;
- selected table-hypothesis IDs plus deterministic validator results;
- semantic mappings from canonical fields to source header-cell/token IDs;
- unresolved cells and requested corroboration searches;
- budget usage; and
- one terminal status: `completed_for_materialization`, `unconfirmed_residue`, or `processing_failure`.

It must not return authoritative field strings. Application code dereferences source IDs to materialize values. It must not emit an “approved,” “include,” “record,” “verified by agreement,” or Current Production Run decision.

Structured Outputs and tool guardrails are implementation controls, not evidence. OpenAI's documentation says strict mode enforces schema adherence, and the Agents SDK documentation notes that guardrails run at specific workflow boundaries; Corridor must put deterministic checks next to every materializing function rather than assume agent-level guardrails cover nested work ([strict mode](https://developers.openai.com/api/docs/guides/function-calling#strict-mode), [workflow boundaries](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals#workflow-boundaries-matter)).

## 5. Should Corridor rebuild each matrix as a spreadsheet or canonical table?

**Yes: rebuild a derived, canonical table representation for computation and review. No: do not claim to have reconstructed the original spreadsheet.** “Derived table artifact” below is descriptive, not a proposed new Corridor glossary term.

The table artifact is the missing middle layer between page mechanics and Extracted Proposals. It should be immutable per Extraction Run, lossless with respect to what the pipeline observed, and exportable to spreadsheet-like views. The canonical record should be JSON or another nested format that can preserve spans and provenance; XLSX, CSV, HTML, and Markdown are views. A flat CSV cannot faithfully represent merged headers, multiple source tokens, polygons, or unconfirmed readings without a sidecar.

### Recoverable from a PDF when visibly present

- page-local row and column order;
- visible cell strings and marks;
- cell and table polygons;
- visible row/column spans and merged-header relationships, if the recognizer can prove them;
- repeated headers, page labels, group bands, and page-scoped values;
- visible row identifiers and row order;
- basic presentation needed for review; and
- source-page and cell-level provenance.

### Not safely inferable from a printed PDF

- original workbook formulas, types, named ranges, validations, dropdown lists, hidden rows/sheets, filters, comments, links, macros, and edit history;
- clipped or omitted print columns and off-page content;
- text that the print process truncated or replaced;
- whether a blank means empty, repeated/ditto, not applicable, or a merged value without visible evidence;
- semantic meaning conveyed only by color or typography unless the source defines it;
- original cell coordinates or row IDs in a workbook;
- whether adjacent pages are one table without continuation evidence;
- whether a PDF and spreadsheet are equivalent renditions of one revision;
- which revision is current or legally controlling; and
- any value no readable source supplies.

### Minimum artifact provenance

Each table and cell should retain at least:

| Level | Required provenance |
| --- | --- |
| Artifact | Document, Document Revision, Document Rendition digest, Extraction Run, artifact schema version |
| Page | Human page number, PDF page index, boxes, rotation, coordinate transform, render IDs/digests |
| Table | Page, polygon, hypothesis engine/model/configuration, selected-hypothesis reasons, cross-page fragment links |
| Cell | Stable cell ID, row/column index and spans, PDF polygon, render polygon, native/OCR token IDs, raw/normalized text, status (`verified`, `unconfirmed`, `absent`), warnings |
| Semantic mapping | Canonical field, source header-cell/token IDs, mapping model/prompt/schema, unmapped source headers |
| Extracted Proposal field | Source cell/token IDs, exact transformation rule, materialized value, field validator results, Source Passage Check result |

A reviewer-facing workbook can put the reconstructed matrix on one sheet and provenance on companion sheets, or use cell comments/links to open the exact page crop. The canonical artifact must remain the source of that workbook so spreadsheet export never strips provenance silently.

## 6. Credible component options

| Option | Documented capability | Corridor architecture inference | What the corpus must decide |
| --- | --- | --- | --- |
| PDF specification + native parser | PDF content streams preserve positioned text, graphics, and images; logical structure is separate and optional ([Adobe PDF Reference](https://opensource.adobe.com/dc-acrobat-sdk-docs/pdfstandards/pdfreference1.3.pdf), [ISO 32000-2 access](https://pdfa.org/resource/iso-32000-2/)) | The source bytes must stay canonical; extraction needs both native objects and the painted page | Which PDF constructs occur in Corridor matrices, including outlined text, malformed encodings, optional content, and mixed scans |
| PyMuPDF | Positioned words/blocks, drawings/images, rendering, OCR integration, and configurable table finding; plain text may not be in visual order ([text](https://pymupdf.readthedocs.io/en/latest/recipes-text.html), [`find_tables`](https://pymupdf.readthedocs.io/en/latest/page.html#pymupdf.Page.find_tables), [OCR](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html)) | Keep it as native PDF/rendering foundation and one table hypothesis engine | Best strategy/tolerances by page class; native token correctness; table topology and cell-assignment failure rate |
| Tesseract | OCR over images; text, searchable PDF, hOCR, and TSV outputs with word coordinates/confidence ([Tesseract usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)) | A transparent local OCR baseline and source-token generator, not a table semantic mapper | DPI/preprocessing, character and exact-field error, tiny stationing/offset performance, and confidence calibration |
| OCRmyPDF | Page rasterization, OCR text-layer integration, mixed born-digital/scan handling, rotation/deskew/cleanup, sidecar text, and minimally altered output ([introduction](https://ocrmypdf.readthedocs.io/en/latest/introduction.html), [cookbook](https://ocrmypdf.readthedocs.io/en/latest/cookbook.html)) | Useful for controlled derived searchable renditions and preprocessing orchestration | Whether it improves OCR while preserving required page geometry; which transforms are safe for the corpus |
| PaddleOCR PP-StructureV3 | Modular preprocessing, OCR, layout, wired/wireless table recognition, cell boxes, OCR polygons/text, predicted HTML, JSON, and Markdown ([PP-StructureV3](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PP-StructureV3.html)) | Strong first dedicated table/layout challenger, especially where PyMuPDF has no geometry | Utility-matrix table recall, cell topology, numeric text accuracy, rotation, merged/group headers, runtime and hardware needs |
| Docling | Native PDF backends, multiple OCR engines, layout detection, TableFormer, native-cell matching, unified document model with bounding boxes/provenance, and lossless JSON ([model catalog](https://github.com/docling-project/docling/blob/main/docs/usage/model_catalog.md), [CLI options](https://github.com/docling-project/docling/blob/main/docs/reference/cli.md), [document model](https://github.com/docling-project/docling/blob/main/docs/concepts/docling_document.md)) | Strong integrated local challenger and useful reference schema; may reduce custom plumbing | Whether cell matching helps or harms merged utility tables; native/OCR fusion quality; stability, resource use, and exact provenance completeness |
| Azure Document Intelligence Layout | Tables with row/column indices, row/column spans, header classification, polygons, text spans, rotation support, words and confidence ([Azure Layout](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0)) | Most directly documented cloud fit for a table-structure benchmark lane | Corridor accuracy, data-residency/contract constraints, cost, latency, repeatability across service versions, and exportability of raw responses |
| Amazon Textract AnalyzeDocument | TABLE/CELL blocks, merged cells, headers/titles/footers, geometry, lines and words for PDF/image input ([Textract tables](https://docs.aws.amazon.com/textract/latest/dg/how-it-works-tables.html), [`AnalyzeDocument`](https://docs.aws.amazon.com/textract/latest/APIReference/API_AnalyzeDocument.html)) | Credible second cloud benchmark and possible managed OCR/table adapter | Same corpus, governance, versioning, cost, exact-field, topology, and provenance tests |
| Google Document AI | Enterprise OCR; Form Parser tables/KVPs; Document object with table rows/cells/spans/bounding boxes; current Form Parser documentation limits tables to conventional non-spanning cells ([processor list](https://cloud.google.com/document-ai/docs/processors-list), [Form Parser](https://cloud.google.com/document-ai/docs/form-parser), [Document schema](https://cloud.google.com/document-ai/docs/reference/rest/v1/Document)) | Useful OCR/cloud comparison, but Form Parser's documented simple-table boundary is a poor fit for multi-row and merged matrix headers | Whether another Google processor/version materially handles Corridor topology; do not rely on vendor examples of “reduced hallucination” as proof |
| OpenAI Responses / Agents SDK | PDF inputs provide extracted text plus page images; function tools, strict schemas, allowed tool subsets, manager orchestration, guardrails, and resumable approvals are documented ([file inputs](https://developers.openai.com/api/docs/guides/file-inputs), [function calling](https://developers.openai.com/api/docs/guides/function-calling), [Agents SDK](https://developers.openai.com/api/docs/guides/agents/quickstart)) | Suitable for semantic mapping or bounded read-only orchestration, not value authority | Mapping accuracy, tool-route repeatability, failure handling, cost/latency, prompt-injection resistance, and whether an agent improves over deterministic routing |

The architecture should not bind Corridor's provenance model to any provider's object IDs. Persist raw provider responses, but normalize them into Corridor-owned token, region, table, and cell identifiers tied to exact input and configuration.

## 7. Staged evaluation plan

### Stage 0 — freeze the evaluation contract

Build a versioned, page- and cell-level gold set before changing production selection:

- current digital-text matrices from Project A and SH 99;
- FDOT SR 789's page-scoped owner and unfamiliar headers;
- WSDOT 9424's marked resolution columns;
- WSDOT 9540 only if separately authorized, preserving any access restriction;
- rotated pages, continuation pages, repeated headers, group-title bands, two-tables-on-one-page cases, merged/multiline headers, and retired/blank rows;
- real image-only, vector-text, and mixed pages if present; otherwise controlled degradations are supplemental tests, not substitutes for real sources; and
- negative pages with no matrix.

Gold labels must include table boundaries, cell grid/spans, exact visible cell text, page/cell polygons, header relationships, canonical mapping, page-scoped values, row disposition, and unreadable/unconfirmed status. Record who created and checked the gold data and from which exact bytes. This is an evaluation asset, not a production transcription queue.

Freeze metrics and acceptance ceilings before running challengers. The key metric is not vendor table score; it is wrong source-cited Project Record proposals avoided.

### Stage 1 — page inventory, routing, and rendering

Compare the 50-character heuristic with the proposed deterministic inventory.

Measure:

- image-only, vector-text, digital-text, and mixed classification confusion matrix;
- region coverage, not just page label;
- false “OCR not needed” rate;
- unnecessary OCR rate;
- render/crop coordinate round-trip error;
- OCR exact-field accuracy across evaluated DPI/preprocessing profiles;
- latency, storage, and cost; and
- Processing Failure visibility.

Do not choose a production DPI until this stage. Candidate values such as 200, 300, or 400 DPI are test parameters, not recommendations by authority.

### Stage 2 — table structure and cell reconstruction

Run the same pinned pages through:

- current PyMuPDF defaults;
- a small predeclared PyMuPDF strategy/tolerance set;
- Docling;
- PaddleOCR PP-StructureV3; and
- Azure/AWS lanes if permitted.

Measure per page and per table:

- table detection precision/recall;
- exact row and column count;
- exact cell adjacency and row/column-span accuracy;
- header/body/group classification;
- cell polygon overlap and token assignment;
- unassigned and multiply assigned tokens;
- cross-page stitch correctness;
- complete row/cell accounting; and
- repeatability under pinned versions.

No engine wins because it exports HTML or scores well on its own benchmark. It wins only on the Corridor gold set and operational constraints.

### Stage 3 — exact values and provenance

Measure:

- exact raw cell-value match;
- exact station, offset, identifier, date, and mark-column match;
- wrong-row and wrong-cell assignment rate;
- field-level provenance coverage;
- native/OCR disagreement rate;
- verified/unconfirmed/absent classification accuracy;
- false verified values, with a target of zero on the acceptance corpus; and
- whether every materialized value can be replayed from source token IDs without a model string.

This stage should reproduce the existing `vision-misreads.json` cases and prove that no equivalent generative transcription surface remains.

### Stage 4 — semantic mapping

Evaluate the current header mapper against the source-ID-only contract. Test:

- exact canonical field mapping;
- page-scoped External Organization attribution;
- merged/group header relationships;
- unknown/unmapped precision;
- repeated-header consistency without majority masking systematic error;
- source data-dictionary assistance when a structured template is registered; and
- downstream row impact of each mapping error.

Compare at least one non-agent single Responses call with any proposed agent route. Structured-output validity is tracked separately from semantic correctness.

### Stage 5 — end-to-end Extraction Runs

Create new non-production Extraction Runs over the full authorized corpus. Measure:

- Extracted Proposal recall and precision;
- exact complete-row match and exact per-field match;
- Source Passage Check and field-cell check results;
- unconfirmed residue and Processing Failure rates;
- row accounting and no-matrix discrimination;
- two-run semantic repeatability;
- latency, resource use, model/OCR calls, and cost; and
- quality by page class and source agency, not only aggregate quality.

Do not compare runs by database IDs or latest timestamps. Compare semantic payloads tied to exact Document Rendition bytes and configuration.

### Stage 6 — bounded agent A/B

Only after the deterministic pipeline works, let an agent orchestrate the hard-page subset under fixed tools, profiles, budgets, and stop rules. Compare it with the deterministic router on:

- successful recovery of previously unresolved table/cell structure;
- false verified values;
- invented or untraceable values (must remain zero);
- unnecessary tool calls and retries;
- repeatability;
- Processing Failure correctness;
- prompt-injection resistance from document text; and
- cost and wall time.

Reject the agent if it merely adds complexity, if it authors values, or if its gains disappear after deterministic retry profiles are added.

### Stage 7 — shadow selection and rollout gate

Run the new architecture in shadow beside the existing Current Production Run. It must not replace that run automatically. Review semantic differences, unresolved residues, and source overlays. Then require:

1. all stage-specific gates pass on the frozen corpus;
2. zero model-authored materialized values;
3. zero Extracted Proposal fields without cell/token provenance;
4. zero silent Processing Failures or unaccounted rows/cells;
5. zero model-agreement substitutions for Supporting Documentation;
6. reproducible pinned versions and receipts;
7. explicit attributable selection of the Current Production Run under existing authority; and
8. unchanged Record Inclusion authority.

After selection, retain the prior Extraction Run and derived artifacts for comparison and rollback. A selected extraction architecture can improve the evidence supplied to Record Inclusion; it does not expand what Record Inclusion is allowed to do.

## Concrete target architecture

The recommended implementation sequence, subject to the corpus gates, is:

1. **Introduce a Corridor-owned page/token/table/cell artifact contract** without changing Project Record or Record Inclusion behavior.
2. **Refactor ingestion conceptually from one page text string to representation inventory**: native tokens, OCR tokens, renders, and failures remain distinct. Keep compatibility projections only at the edges.
3. **Retain PyMuPDF** for native facts, vector/grid hints, rendering, and one table hypothesis.
4. **Add Docling and PaddleOCR adapters in an evaluation harness**, not directly to production. Choose one, both, or neither from Corridor results.
5. **Eliminate generative value transcription from the verified path.** OCR engines create tokens; models create source-reference mappings only.
6. **Strengthen Source Passage Check to field-cell provenance** while retaining row quotes for reviewer context.
7. **Persist the derived table artifact per Extraction Run** and generate spreadsheet-like review/export views from it.
8. **Evaluate a tool-using agent only for bounded hard-page orchestration** after deterministic contracts and validators exist.
9. **Shadow, compare, explicitly select, and preserve run history.** No “latest successful run” behavior.

The critical product choice is not PyMuPDF versus PaddleOCR versus Docling. It is whether Corridor owns a source-replayable cell and provenance contract. Once that contract exists, engines can be evaluated and replaced. Without it, a more modern parser would only produce a newer opaque answer.
