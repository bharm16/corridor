# PDF gold set v1

This directory is the frozen Stage 0 evaluation asset for #439. Geometry is
stored as integer thousandths of a PDF point, not render pixels. Each document
is bound to the SHA-256 in its checked-in corpus lock. Labels are deliberately
layered: a table engine may be scored on geometry without being scored on text,
and a text engine may be scored without being granted permission to produce an
Extracted Proposal.

Document families, not individual copies, are assigned to development,
regression, or holdout. Any run that includes the holdout is refused unless the
CLI appends an actor and reason to `holdout-access.jsonl`. Do not inspect the
holdout for tuning; a holdout buys one predeclared measurement.

The cells here are a representative evaluation sample, not a request to
transcribe the corpus. Unreadable and unresolved cases remain labelled as such.
The acceptance ceilings live in `dataset.json` and were frozen with the labels,
before any challenger configuration is measured.

## Stage 1 routing receipt

`stage1-routing-gold.json` binds independently checked OCR-needed labels to the
exact v1 document/page identities. `stage1-routing-run.json` records the native
text length and inventory-router decision observed from the locked PDF bytes;
`stage1-routing-evaluation.json` is the deterministic comparison with the
retired character-count rule. The five-page slice contains three native matrix
pages, one image-only agreement page, and one mixed native/scanned agreement
page. The inventory router records no misses and no unnecessary OCR; the retired
rule misses the mixed page, for a 50% false "OCR not needed" rate over the two
positive pages. Synthetic tests exercise both error directions but contribute
to no quality claim. FDOT holdout access for this predeclared measurement is
recorded in `holdout-access.jsonl`.

## Stage 1 routing under the reader-backed inventory (#734)

The same five pages were decided again from the paired-rendition reader's
facts and scored through the same evaluator. No page's routing changed: the
false "OCR not needed" rate and the unnecessary-OCR rate are both 0, as they
are for the incumbent inventory above, and the by-page-class breakdown is
identical. The run, its evaluation, the per-page comparison and the prose that
explains the inventory differences behind those identical decisions are under
`artifacts/pdf-reader-page-inventory/`, reproducible with
`make page-inventory-routing-replay`. The FDOT holdout access is the
2026-09-07 line of `holdout-access.jsonl`. The replay is a seam regression on
five pages, not a selection: #447 owns native selection and #739 owns scanned
selection.

## Render profile selection

`render-profile-measurement.json` records the five real Stage 1 pages and every
candidate DPI measurement used by #441. Its predeclared minimum-sufficient
rules select the values in `render-profiles.json`; the application refuses a
bundle whose measurement digest or replayed selection does not match. Review is
an unprocessed 200-DPI derivative, OCR/layout and deterministic table CV use
separately identified 300-DPI derivatives, and the 600-DPI profile is limited to
bounded cell crops. OpenCV is absent from the application lock and lives only in
`workers/render/uv.lock`.

Every derivative manifest records its source digest, page, profile identity,
rasterizer, library versions, parameters, artifact digest, Class B retention
label, and the forward/inverse affine chain from PDF user space through page
rotation, clip, raster scale, and any deskew. The source bytes plus that
manifest are sufficient to regenerate the artifact; reviewer pixels are never
overwritten by a preprocessed derivative.

The profiles are the same under either rasterizer. #735 put PDFium beside
MuPDF in the worker (ADR-0094); the DPI selection, the preprocessing and the
gold measurement above are untouched by it, because the engine decides how the
page is turned into pixels and nothing about which pixels are asked for. The
engine is a deployment setting that is off, the manifest names the engine that
ran, and a PDFium derivative is a new identity beside the MuPDF one rather
than a replacement of it. `artifacts/render-rasterizer-comparison/` holds what
the two engines produce on the corpus, with the tolerances that comparison
declared.

## Frozen paired-rendition reader baseline (#731)

The imported paired-rendition reader (`src/corridor_pdf_reader`, commit
`c39363e`, engine `tagged` at 36 dpi) was scored once through this contract
by `make pdf-reader-gold-eval`; the engine run, the evaluation JSON and
Markdown, the adapter's frame record and the per-threshold applicability
record are retained under `gold/pdf-pairs/v1/pdf-v1/frozen-reader-2026-09-06/`
and read in `gold/pdf-pairs/v1/README.md`. The FDOT holdout access is the
2026-09-06 line of `holdout-access.jsonl`. The run is a predeclared baseline,
not a pass: layers the frozen reader does not produce are marked absent and
labelled not applicable there, never scored as reader results.
