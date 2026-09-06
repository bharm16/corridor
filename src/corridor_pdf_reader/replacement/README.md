# Permissive PDF reader

This reader uses PDFium, PDFOxide, or an explicit combination. Its environment
does not contain PyMuPDF. The benchmark rejects challenger environments in which
PyMuPDF is installed.

From the parent `pdf-reader-comparison` folder:

```bash
make setup-replacement
make read ARGS="corpus/wsdot-9540-gas.pdf --pages 1 --engine pdfium --output results/my-document"
```

The result is `document.json` plus page PNGs. A new output directory is required.
Use `--engine oxide_pdfium` for PDFium glyph extraction and rendering plus
PDFOxide vectors and native table geometry, or `--engine pdf_oxide` for PDFOxide alone. No paid service or
model is called. To copy the reader elsewhere, retain this `replacement/` package
directory and run its module from its parent directory using its own environment.

## What improved

- PDFium calls its native character APIs directly; text is parsed once.
- Both readers reconstruct cells from vector rules, including thin filled
  rectangles commonly used by spreadsheet PDF exports.
- Missing internal borders join cells; spans remain explicit. Open borders
  cannot manufacture a closed cell.
- Text ordering preserves actual whitespace while removing overlapping generated
  space placeholders. Rotated text has its own reading frame.
- PDFOxide checks authentication before extraction. Its media-sized raster is
  cropped using the document's CropBox, rotation, and DPI.
- The combined configuration validates that parser and renderer agree on page
  count, CropBox, and rotation before joining their outputs.
- All benchmark configurations, including PyMuPDF, use the same layout code,
  pixel hashing, and Pillow PNG settings. Disk persistence is outside the timed
  pass. The baseline also parses text and vectors once.

## Boundaries

This is a tested prototype for these documents. The hybrid reconstructs drawn
and partially ruled grids, native logical rows, borderless aligned tables, and
repeated ledger regions. Inferred rows/columns carry separate method labels.
Continuation needs current-page alignment evidence; it remains a heuristic.
Native PDFium character indices are retained on every structured cell, and a
refinement cannot discard the glyphs already assigned to the original table.
The page text and native text objects remain available outside detected tables.

**Full document parity is not established for the complete paired corpus.**
Superscript markers, overlapping notes, source-pair revision differences and
unprinted workbook information remain explicit limitations. See
[the complete diagnosis](../parity_debug/DIAGNOSIS.md) and its retained audit.
Raster-only pages render but need a separate OCR stage for text. Cell indices are page-local. A continuation profile can supply column anchors
from an earlier requested page; it does not establish business relationships
or independently prove that the pages describe the same table.

`characters.box` is in unrotated crop coordinates; `display_box` and reconstructed
cell boxes are in rotated displayed-crop coordinates, all in PDF points. Render
coordinates multiply those displayed coordinates by DPI/72. PDFium exposes ink
bounds and separate font-advance bounds; PDFOxide sometimes exposes degenerate
rotated-character boxes. Their character rectangles are not interchangeable truth.

The crop adapter rounds its crop origin to the nearest pixel; it does not
rescale a wrong raster into agreement. Known-color location tests cover all four
rotations at 150 DPI. There is a 25-million-pixel raster bound and a 100,000-grid-
intersection bound. The benchmark also isolates each configuration with a timeout.

The new reference checks were visually transcribed by Codex from Poppler renders,
not copied from PyMuPDF's extracted text. They are limited spot checks and were
not independently adjudicated by a human. First-page development checks and
later-page validation checks are reported separately. FDOT remains unused.

## Licensing

The pinned packages declare permissive licenses:

- PDFOxide 0.3.77: MIT OR Apache-2.0.
- pypdfium2 5.13.0: Apache-2.0 OR BSD-3-Clause; PDFium and its bundled dependencies
  carry additional included open-source notices.
- Pillow 12.3.0: MIT-CMU.

The exact installed wheel notices are retained under `third-party-notices/`, with
a file/hash manifest. Keep applicable licenses and notices when redistributing
binaries. The parent benchmark still has PyMuPDF for comparison; the replacement
environment is the portion intended for an affordable alternative.

Primary sources: [PDFOxide MIT license](https://github.com/yfedoseev/pdf_oxide/blob/main/LICENSE-MIT)
and [PDFium/pypdfium2 licensing](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing).
These identify the supplied licenses; they are not a legal opinion about a future
deployment containing other dependencies.

## Semantics tier

`replacement/semantics.py` is the tier above the reader: it asks the OpenAI
API which printed column is which canonical field, and nothing else (ADR-0006
in Corridor: the model reads structure, the document supplies values). The
page's tables are listed to the model cell by cell, each with an ID
(`t0r7c2` is table 0, row 7, column 2; `o3` is the third string outside every
table), beside a render of the page. The model returns indexes and IDs: the
matrix table, the header row, one canonical field or null per column, and a
page attribute as the ID of the cell that states it. Code assembles every
conflict row from the reader's own cells, so each stored value is a cell the
page holds, with the cell's ID beside it as provenance. Answers are checked
before they are believed: an index or ID the listing does not hold, a field
outside `replacement/vocabulary.py`, a second column claiming a field, and a
page attribute pointing into the conflict rows are all refused and recorded on
the reading.

```bash
make semantics ARGS="--pdf matrix.pdf --output results/semantics-run --env-file /path/to/.env"
make semantics ARGS="--pdf matrix.pdf --output results/again --replay results/semantics-run/document.json --dpi 0"
make semantics-eval ARGS="--reading results/semantics-run/document.json --gold gold.csv"
```

`document.json` holds, per page, the raw structure the model returned, the
checked reading (mapping, page attributes, rows with dispositions and cell
IDs) and the listing the model saw. `--replay` assembles rows again from a
stored structure without calling the model. The client (`replacement/llm.py`)
is a cut-down port of Corridor's: Responses API, strict JSON schema,
reasoning effort `none`, images at original detail, nothing stored
server-side; the key comes from `OPENAI_API_KEY` or `--env-file`.
`bootstrap/tests/test_semantics.py` exercises the tier with recorded answers;
`SEMANTICS-RESULTS.md` records the runs against Corridor's machine gold.
