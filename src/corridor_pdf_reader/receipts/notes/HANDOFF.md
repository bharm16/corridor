# Handoff: the PyMuPDF replacement for Corridor's PDF ingest

Written 2026-09-05 at the end of one long session. Read this before touching
anything under `replacement/` or `bootstrap/`. The short brief for a new
session is at the end.

## Decisions, in the order they were made

1. **No PyMuPDF in anything shipped.** The user cannot afford the Artifex
   licence. PyMuPDF may run only as the comparison baseline inside this
   benchmark repo (the root `.venv`). Nothing copied into Corridor may import
   it. Corridor's roadmap item #461 recorded the opposite decision (buy the
   licence); the replacement supersedes it and an ADR must say so.
2. **The semantics tier calls the OpenAI API, not Claude.** User correction:
   "replace semantics (claude) for openai api". The model is Corridor's
   `gpt-5.6-luna`, reasoning effort `none`, strict JSON schema, nothing stored
   server-side. The model returns indexes and cell IDs only; code copies
   every value from the reader's cells (Corridor ADR-0006).
3. **100% accuracy is the target and it is reached by a bootstrap loop, not
   by hand-tuning.** A sealed holdout is never tuned on. The gate enforces
   precision; coverage climbs per iteration; every miss has a named class.
4. **The answer key is the workbook's own display strings.** When the key was
   wrong, the key was corrected and the correction logged; the reader was
   never bent to a wrong key. Pairs whose workbook was edited after printing
   are excluded as revisions, never fixed.
5. **The replacement is frozen.** User instruction: "the system we have built
   to replace pymupdf in this conversation should not be altered unless I
   say. we just tested this endlessly for a reason." Add beside it; do not
   edit it. Propose any change first and wait.
6. **Textract is the last rung, to be tested before it is ticketed.** At most
   ten documents. Not built yet; the test design is below.

## The stack, tier by tier

| Tier | Engine | Licence | Status on 2026-09-05 |
|---|---|---|---|
| Native facts | pypdfium2 5.13 | Apache-2.0 / BSD-3 | Built and tested: glyphs with Unicode and boxes, rotation, page boxes, paths as rules, clip paths as hidden runs, marked-content ids, Artifact marks, rendering. Open: embedded image regions for Corridor's Page Inventory, and Corridor's three render profiles. |
| Structure A | Excel tag tree via pypdf 6.17, joined to PDFium glyphs by marked-content id | BSD-3 | Built and tested (`replacement/tags.py`). Orphan marked content (Excel's untagged repeated print titles) is one cell per id. |
| Structure B | Deterministic reconstructor: rules from paths, rows from tagged extents, ruled frames and drawn rows, columns from rule intervals and shared edges | ours | Built and tested; also runs on the leftover glyphs of tagged pages. One residue: PE-40's borderless header block. |
| OCR | AWS Textract AnalyzeDocument TABLES, one page per call, routed per region by the Page Inventory (the user's decision of 22:45: in place of the local rung for now) | pay per page | Built and tested on ten dev pairs (`textract/`, `TEXTRACT-RESULTS.md`): table geometry with native glyphs re-mapped 25 to 28 of 53 pages; Textract's own words 91.9% of cells exact on clean scans, 84.9% on degraded; 9424 semantics 162/162 from its pages. The local rung (Tesseract 5.5 with RapidOCR as check, `ocr/` on branch `codex/ocr-rung`) is parked at 96.0% clean and 73.0% degraded cells on its 40-page probe. |
| Last resort | Corridor's model transcription tier, verified against the OCR text | API | Not code in this repo; unchanged. Fired on 0 of 58 measured pages. |
| Semantics | OpenAI, structure only, by cell ID | API | Built and tested (`replacement/semantics.py`): WSDOT 9424 162/162 rows, WSDOT 9540 192/192 rows against Corridor's machine gold. |

## Where things are

- Corpus: `/Users/bryceharmon/Desktop/utility-conflict-matrices/PDF-Spreadsheet-Pairs/true-pairs`
  (524 unique pairs, 3,835 pages, `MANIFEST.csv`). The loop's corpus is the
  nested `true-pairs/exact/` (333 pairs whose PDF prints the workbook exactly;
  `EXCLUDED.csv` names every pair left out and why). Synthetic twins rendered
  by LibreOffice live in `true-pairs/exact/synthetic/out` (351 pairs, the
  user's generator; `README.md` there).
- Harness: `bootstrap/` (see `bootstrap/README.md`). Sealed splits:
  `bootstrap/pairs.json` (seed 720, 20% holdout, strata agency × producer
  family) and `bootstrap/pairs-synthetic.json`. `TRUE_PAIRS_ROOT` and
  `TRUE_PAIRS_SPLIT` select corpus and split; the Makefile exports both.
- References (answer keys): `results/loop-reference-v6` for the exact set,
  `results/syn-reference-v1` for the synthetic set. `results/` is gitignored;
  rebuild with `make loop-reference ARGS="--output results/<name>"`.
- Runs: `results/loop-020` (exact set), `results/syn-003` (synthetic),
  `results/semantics-9424-v2` and `results/semantics-9540-*` (semantics tier).
  The iteration table is `bootstrap/LOOP-LOG.md`; the semantics runs are in
  `SEMANTICS-RESULTS.md`.
- Reader: `replacement/reader.py --engine tagged`, `replacement/tags.py`,
  `replacement/layout.py`, `replacement/pages.py` (slim page with cell ids).
- Semantics tier: `replacement/semantics.py`, `replacement/llm.py` (client),
  `replacement/vocabulary.py` (copied from Corridor's `vocabulary.py`),
  `replacement/prompts/matrix_structure_ids_v1.md`.
- Environments: root `.venv` runs the harness and holds PyMuPDF for the
  baseline only; `replacement/.venv` (uv) holds pypdfium2, pdf-oxide, pillow,
  pypdf and httpx and never PyMuPDF. `bootstrap/fmt.mjs` needs node and
  imports `paired_trial/number_format.mjs` (SSF). Oddity: `xlrd` imports in
  the Homebrew `python3` but not in `.venv`; the reference builder reported
  no failures on `.xls` workbooks, so check which interpreter reads them
  before trusting an `.xls` key.
- Corridor: `/Users/bryceharmon/Desktop/corridor`. Its OpenAI key is in its
  gitignored `.env`; the semantics CLI reads it with `--env-file`. Its AWS
  profile is `corridor`, region us-east-2, no live session at handoff.

## Numbers that matter

| Measurement | Result |
|---|---|
| loop-020, exact set, dev | 262 / 263 pairs, 1,589 / 1,589 pages, 483,210 / 483,336 cells exact |
| loop-020, exact set, holdout | 70 / 70 pairs, 409 / 409 pages |
| syn-003, synthetic set, dev | 261 / 281 pairs, 1,592 / 1,627 pages |
| Semantics, WSDOT 9424 (11 pages) | 162 / 162 gold rows, recall and precision 100%, 71,207 prompt tokens (26,334 cached) |
| Semantics, WSDOT 9540 (6 listings, 9 pages) | 192 / 192 gold rows, recall and precision 100% |

Residue on the exact set: PE-40 (Ghostscript print, borderless header block
of side-by-side wrapped cells). Residue on the synthetic set: 15 `.xls` twins
where LibreOffice's export cached 0 for string formulas (data defect to report
to the generator), two Hoffman Structures twins that print pages of retainage
zeros Excel never printed, the R06_TOC twin descended from a converted
workbook, status-mobility-fy2020's footnote paragraph, and the DCW twin.

## How the gate works

A page passes when every reader cell lands on a reference cell with the same
display text, every reference cell in the mapped columns is present, spans
agree, and every string outside a table is a header or footer. A pair passes
when every page passes and every reference cell is exact on some page. Recorded
but not failing, because the PDF cannot hold them: clipped text (with hidden
runs, page edges, rules and column walls as evidence), uncached formulas,
print-area overflow, hashes over a too-narrow number or date, day-zero dates,
General-format width rounding, refined or folded columns, merged rows drawn
without a rule, prose rows around a table, and rows split across pages. The
full list with reasons is in `bootstrap/README.md`.

## Answer-key corrections made

SSF zero padding; Excel's 15-significant-digit half-away-from-zero rounding
with sign-preserving epsilon; locale date formats 14 and 22; print-title rows
outside the print area; sheet names with trailing spaces; workbook type by
magic bytes; zero-height rows hidden; text outside the print area kept for
explanation; a look-alike row already matched on an earlier page loses ties.
Each is logged in `bootstrap/README.md`.

## Exclusions from the exact set, 2026-09-05

One Word-authored PDF (WSF sign review), one workbook converted from its PDF
(R06_TOC, sheet `Table 1`), nine Hoffman Structures billings plus their holdout
sibling saved after printing (later rows 325–328 and retainage formulas the
print lacks), two ADF and one Hoffman and one Kenco pivot refreshes, one PCI
summary cell and one Colman Docks pair typed after printing. The text-only
exactness check could not see these because short numbers and amounts
present elsewhere in the workbook cannot prove a cell printed; the scorer
found them.

## Corridor port: the plan drafted, not yet ticketed

Corridor's PyMuPDF surface is sixteen product modules, the render worker
(`workers/render`), and about twenty test files that synthesize PDFs. The
constraints: ADR-0006 (model maps structure, document supplies values),
ADR-0023 (machine references are first-write-only), ADR-0068 (source segment
locators verify against page text), ADR-0073 (token-layer engine identity is
pinned), issue #461 (licence decision to supersede) gating #535 (live
activation). The drafted breakdown is eleven tickets in expand–contract order:

1. ADR that PDF facts come from permissive engines, plus an import guard with
   an allowlist that later tickets shrink.
2. The reader lands in Corridor as the PDF adapter package.
3. Stage 0 Extraction Measurement of the challenger through `pdf_evaluation`
   before any seam moves.
4. Native page text and Token Layer from the reader (`native-pdfium-v1`);
   Source Segment locators re-verified on the corpus.
5. Page Inventory and routing from the reader, routing decisions unchanged.
6. Render derivatives from PDFium under the same profiles.
7. Tier 1 structure mapping on the reader's cells by ID, measured against the
   machine references.
8. Machine references re-authored beside the old ones under a new method id.
9. The small PyMuPDF call sites move to pypdf, pypdfium2 or Pillow.
10. Test fixtures synthesize PDFs without PyMuPDF.
11. PyMuPDF leaves both environments; #461 closes by decision.
12. Textract rung (added by the 22:45 decision below; sits after 7): the
    `textract/` adapter in Corridor, AnalyzeDocument TABLES per page for
    regions the Page Inventory routes to OCR and for text-layer pages that
    fail the structural gate; block-to-page mapping; native-glyph re-mapping
    with run-mate rescue; the response cache; cost accounting; pages sent to
    AWS under the data-handling gate (#522); Textract-only values enter as
    unconfirmed readings until corroborated (policy still open).

Corridor's OCR route today: the Page Inventory routes native, image and mixed
regions (unicode quality below 0.98 or a suspicious signal means both; an image
covering 2% or more means OCR on that region; vector-only pages OCR whole),
Tesseract 5 reads OCR regions at 300 dpi, an unreadable page (fewer than the
profile's minimum readable characters) enters the ADR-0064 rescue-then-
corroborate harness, and pages with no text layer fall to model transcription
verified against OCR text. The only recorded OCR measurement is the five-page
routing receipt in Corridor's `gold/pdf/v1` (no misses, no unnecessary OCR).
The transcription tier fired on 0 of 11 and 0 of 47 measured pages.

## Textract test: designed here, built and run on 2026-09-05 (branch `codex/textract-rung`, merged; `TEXTRACT-RESULTS.md`)

Ten documents from the exact set: Holmberg 5007_20-08, McClean December,
Kenco 12.2025, Valley 19132-71 (Excel); Billing Review Est-95 and
status-mobility-fy2019 (PrintToPDF); PE-40 and 85531_1010 (Ghostscript);
GEA_608 (Adobe PDF Library); status-mobility-fy2018 (Distiller). Three lanes
per page: A, the page as a single-page PDF where Textract supplies only table
geometry and PDFium glyphs are re-mapped into its cell polygons so values stay
the document's own text; B, a clean 300 dpi scan twin where Textract's words
are the values; C, a degraded twin (200 dpi, one degree of skew, JPEG
compression, noise). Optional lane D: Tesseract on the same twins. Score every
lane with the existing scorer; run the semantics tier on WSDOT 9424 lane B
against the machine gold. Textract facts: synchronous AnalyzeDocument takes
one page or image up to 10 MB, 10 transactions per second in us-east-2,
Tables at $0.015 per page with Layout free, Free Tier 100 pages a month.
About 170 pages, under $3. Blockers: `aws login --profile corridor` or the
reconnected AWS connector, and the user's yes on the budget. Build it as new
files only (adapter, scan-twin maker, harness read script).

The open design question the test must settle: what "fails the gate" means at
runtime without a workbook. Candidates: an inventory table region with no
table read, a matrix page with no header and no carried mapping, the
unreadable-page check.

## Handoff brief for a new session

Copy this to start:

> You are continuing the PyMuPDF replacement for Corridor's PDF ingest, in
> `/Users/bryceharmon/Desktop/pdf-reader-comparison`. Read `HANDOFF.md`
> first, then `bootstrap/README.md`, `replacement/README.md`,
> `bootstrap/LOOP-LOG.md` and `SEMANTICS-RESULTS.md`. Hard rules: never
> import or propose PyMuPDF for anything shipped; the semantics tier uses the
> OpenAI API and returns cell IDs only; do not alter `replacement/` or the
> scorer without the user's explicit yes, add beside them. State: the reader
> passes every page of the exact set (262/263 dev pairs, 70/70 holdout) and
> the semantics tier matches Corridor's machine gold (162/162, 192/192).
> Next: build and run the ten-document Textract last-resort test as new files
> (design in `HANDOFF.md`), which needs a live AWS session on profile
> `corridor` (us-east-2) and a budget of about $3; then ticket the eleven-step
> Corridor port. Corridor is at `/Users/bryceharmon/Desktop/corridor`; its
> OpenAI key is in its `.env`, read with `--env-file`, never printed.

## Next session prompt: the OCR rung, looped to parity

Decision 2026-09-05 evening: build our own OCR layer before Textract and loop
it like the reader, with scan twins of the exact set as the answer key. The
stack: pypdfium2 render at 300 dpi, OpenCV deskew and Sauvola binarisation,
rules detected from pixels by morphology, two text readings (Tesseract 5.5
with character boxes, RapidOCR PP-OCRv4 on onnxruntime) whose agreement marks
confidence, then the frozen reconstructor `replacement.tags.tag_tables` on
those tokens and lines, then the unchanged semantics tier. Holdout carries
over by document identity; parity is reached on clean twins first, then
degraded twins, and the degraded residue specifies the Textract rung.

Copy this to start the session:

> You are building the OCR rung of Corridor's PyMuPDF replacement, in
> /Users/bryceharmon/Desktop/pdf-reader-comparison. Read HANDOFF.md first,
> then bootstrap/README.md, replacement/README.md and bootstrap/LOOP-LOG.md.
> Hard rules: never import or propose PyMuPDF for anything that ships;
> permissive licences only (no Surya, no Docling TableFormer, no cloud by
> default); do not edit replacement/ or bootstrap/score.py, read.py,
> reference.py, import them and add beside them in a new package ocr/ with
> its own uv environment; the semantics tier is OpenAI by cell ID and is
> unchanged.
> Goal: an OCR layer that turns a scanned page into the reader's inputs:
> rasterise with pypdfium2 at 300 dpi (upscale when x-height is under about
> 20 px); OpenCV deskew, Sauvola binarisation, denoise, all recorded;
> detect rules from pixels by morphology and emit them as the reconstructor's
> `lines`; read text twice, Tesseract 5.5 (tessdata at
> /opt/homebrew/share/tessdata, character boxes and confidences) and RapidOCR
> (rapidocr-onnxruntime), words as glyph-like tokens, agreement marks
> confidence; call replacement.tags.tag_tables(chars, lines, []) and write
> the slim page shape of bootstrap/read.py in the harness run layout so
> bootstrap.score scores it unchanged, plus per-cell confidence.
> Corpus: make image-only scan twins of true-pairs/exact in
> true-pairs/exact/scans-clean (300 dpi) and scans-degraded (200 dpi, one
> degree skew, JPEG 70, light noise) with the workbooks, SOURCE.md and
> MANIFEST.csv rows carrying the twins' sha256 (ocr/twins.py). Derive
> bootstrap/pairs-scans-*.json from bootstrap/pairs.json by folder and PDF
> name so every holdout document stays holdout; set TRUE_PAIRS_ROOT and
> TRUE_PAIRS_SPLIT for every command. Build keys with make loop-reference
> ARGS="--output results/ocr-reference-clean" and -degraded.
> Loop: runs results/ocr-NNN, log bootstrap/LOOP-LOG-OCR.md in the shape of
> bootstrap/LOOP-LOG.md, tally with bootstrap.tally; iterate on a probe set
> of about 15 pairs across families between checkpoints, without committing.
> A checkpoint is a full dev-set run whose cells exact and pairs passing are
> at least as good as the previous checkpoint's: at a checkpoint write the
> log row, score the holdout for the report only, and commit the code and
> the log with the run name in the message. A full run that regresses is not
> a checkpoint: revert to the last checkpoint's code and try a different fix.
> Never tune on the holdout. Three lanes until the engine is chosen
> (Tesseract, RapidOCR, agreement), then iterate on agreement; each
> iteration fixes the largest failing class in ocr/, never the scorer's gate. Parity: every dev page passes on
> clean twins, then the same target on degraded twins; the degraded residue,
> classified per cell, is the Textract specification. Sanity-check the two
> scanned City of Houston agreement pages in
> /Users/bryceharmon/Desktop/corridor/corpus (sha 0462b167…, 6cf9abd1…).
> Budgets and policies, decided in advance so you never stop to ask:
> Disk: the twins are about 2,000 image-only pages per lane; budget up to
> 10 GB under true-pairs/exact and keep every run's raw OCR output cached in
> results/ so re-scoring costs nothing. Time: a full lane is one to two hours
> of CPU on this machine; iterate on the probe set (minutes), run the full dev
> set only at checkpoints, use all cores with --jobs, and run long jobs in the
> background. Scorer artifacts: the gate stays exact; an OCR confusion (0 and
> O, 1 and l, 5 and S, rn and m) is a miss to fix in ocr/ through agreement,
> preprocessing or resolution, never a new non-failing class; list any
> artifact you believe is genuinely unreadable in OCR-RESULTS.md for the user
> and keep looping without it. Parity policy: clean twins are a hard target,
> every dev page passing; on degraded twins loop until two consecutive full
> dev runs improve cells exact by less than 0.1 percentage points, then stop,
> classify the residue per cell and page, and write it up as the Textract
> specification, not as work left undone.
> Deliverables: ocr/ with fixture tests, make targets ocr-twins, ocr-read,
> ocr-test, OCR-RESULTS.md (table per lane and family, engine order, the
> runtime Textract trigger, pinned model digests). Checkpoint commits go on
> codex/standalone-comparison with the trailer
> "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"; results/ stays
> gitignored. Stop when clean-twin parity is reached and the degraded
> residue is named.

## Decision 2026-09-05 22:45: Textract is the scanned-page layer for now

The user, after seeing both results, said: "ocr is a dumpster fire, for
now, this should be the layer in place of our own ocr." So the ladder is:
structured original, native reader, Textract for image regions and pages
the Page Inventory routes to OCR, then the model transcription tier. The
local OCR rung (`ocr/` on branch `codex/ocr-rung`, Tesseract 5.5 and
RapidOCR with an agreement lane, pipeline v12) is parked as committed
work in progress, not deleted; its scan twins under `true-pairs/exact/
scans-clean` and `scans-degraded`, its splits and its answer keys stay
useful for measuring any scanned-page layer.

What the evidence says, so the next session does not re-argue it:

- Textract test (branch `codex/textract-rung`, worktree
  `pdf-reader-comparison-textract`, `TEXTRACT-RESULTS.md`): ten dev pairs,
  53 pages, 116 pages sent, $1.74. Lane A (Textract geometry, native
  glyphs re-mapped) 25 of 53 pages, 28 with run-mate rescue; lane B (clean
  scan, Textract words) 7 of 53 pages, 91.9% cells exact; lane C
  (degraded) 6 of 53, 84.9%. The frozen reader on the same pages: 53 of
  53. The semantics tier read WSDOT 9424 to 162 of 162 gold rows from
  Textract pages in both lanes.
- Textract's misreads are systematic and confident: dropped underscores
  and hyphens, lost accounting dashes, case flips; 695 of 901 misread cells
  carry confidence 95 or more. Its confidence cannot gate anything.
- Its structural limits are the same on every lane: the unruled title
  block of an Excel billing lands outside every table, and spans widen
  across merged ranges.
- Re-mapping the document's own glyphs into Textract's cell polygons beats
  Textract's words wherever a text layer exists (892 of 1,191 lost cells
  recovered); with the run-mate rescue rule it is the variant to ship.
- The local OCR rung, when parked (`bootstrap/LOOP-LOG-OCR.md`): clean probe
  3 of 40 pages, 7,580 of 7,893 cells (96.0%); degraded probe 2 of 40 pages,
  5,765 cells (73.0%); no full dev run finished (about 1.8 hours per lane,
  the first interrupted at 47 of 263 pairs). Better than Textract's words on
  clean scans, worse on degraded ones, on a different set of pairs: bounds,
  not a head-to-head. (The recommendation in `TEXTRACT-RESULTS.md` quotes
  96.0% as the local rung's degraded figure; that is its clean figure.)

What this changes in the Corridor port plan: the OCR-related tickets read
"Textract" where they read "Tesseract"; the Page Inventory's routing rules
stay; a Textract rung ticket adds AnalyzeDocument TABLES per page for OCR
regions, the block-to-page mapping, native-glyph re-mapping with run-mate
rescue for text-layer pages that fail the structural gate (an inventory
table region with no table read), the response cache, cost accounting,
and the customer data-handling gate (#522) covering pages sent to AWS.

Open policy question for an ADR: a value read only by Textract has no
second source and its confidence proves nothing, so it should enter as an
unconfirmed reading (ADR-0064's state) until corroborated, not as a
verified value. The user has not decided this yet.

Housekeeping: the scratch bucket `corridor-textract-rung-9593` in
us-east-2 still holds the 116 PNGs and responses; the cache under the
worktree's `results/textract-cache` is complete, so the bucket can go
when the user says.
