# Textract rung

AWS Textract `AnalyzeDocument` with `FeatureTypes ["TABLES"]` as the last
rung of the PyMuPDF replacement, built beside the frozen reader. Nothing
under `replacement/` or in the scorer changes: this package imports
`replacement.reader.read_pdf`, `replacement.layout.ordered_text` and
`replacement.semantics`, and writes the harness run layout, so
`bootstrap.score` scores a Textract run unchanged. Results and the
recommendation are in `TEXTRACT-RESULTS.md`.

## Pipeline, per page

1. **Render** (`render.py`): pypdfium2 renders the displayed page (rotation
   applied) at a stated resolution, grayscale by default, as a
   deterministic PNG. Textract's geometry is ratios of that image, and
   pypdfium2 stretches the page onto the pixel box exactly, so a ratio times
   the displayed page size in points is a coordinate in the reader's own
   frame. Textract's synchronous limits (10 MB, 10,000 px a side) are
   checked before anything is sent.
2. **Call** (`client.py`): the raw response is cached as JSON under
   `results/textract-cache/<sha256 of the PNG bytes>.json` and the cache is
   read before every call, so re-runs and re-scoring cost nothing.
   `ThrottlingException`, `ProvisionedThroughputExceededException` and
   `InternalServerError` are retried with exponential backoff (eight
   attempts); anything else is a page failure. A page budget (default 333
   pages, the $5 cap at $0.015 a page) stops a process from sending more.
   An offline client never calls: it writes uncached pages as pending, for
   another transport (the AWS connector's sandbox, through S3) to fill the
   cache in the same entry shape.
3. **Blocks** (`blocks.py`): TABLE, CELL, MERGED_CELL, LINE and WORD blocks
   become the slim page `bootstrap.read` writes: tables with method
   `textract-analyze-document-tables-v1`, cells with 0-based row and column,
   spans, text (the child WORDs in Textract's order, a newline where the
   LINE changes) and box in points; a MERGED_CELL stands in for the CELLs it
   covers. Each cell records its mean word confidence, its block ids and its
   polygon. LINEs none of whose words a cell owns are the strings outside
   every table; a partly owned line leaves its free words outside.
   `clipped` is empty: pixels hide nothing.
4. **Lane A re-map** (`remap.py`): on a page with a text layer, Textract
   supplies geometry only. Every glyph from `read_pdf(..., engine="tagged")`
   goes to the cell whose polygon holds its centre (the nearer centre when
   cells overlap at a border), the cell's text is `ordered_text` over those
   glyphs, glyphs in no cell are outside strings grouped by text object, and
   the reader's hidden runs stay as clipped evidence. Textract's words are
   never stored on lane A.
5. **Run** (`read.py`): `reads/<key>.json` and `read-receipts.json` per
   lane. Lane A reads the original; lane B the clean scan twin under
   `scans-clean`; lane C the degraded twin under `scans-degraded` at its
   native 200 dpi. Lanes B and C keep the exact root's pair keys, so one
   answer key (`results/loop-reference-v6`) scores all three and the same
   pages compare. A page whose call fails after retries is written empty
   with its error and counted in the receipts; the run goes on.
6. **Semantics** (`semantics.py`): the unchanged OpenAI tier
   (`replacement.semantics.read_document`, cell IDs only) over a lane's
   page dicts and a scaled copy of the PNG each page was read from, written
   as the tier's own `document.json` for `bootstrap.semantics_eval`.

## Commands

```bash
make textract-setup                       # textract/.venv from textract/uv.lock
make textract-test                        # fixture tests, ruff, mypy; no network
aws sts get-caller-identity --profile corridor
TRUE_PAIRS_ROOT=$EXACT make textract-run ARGS="--lane A --output results/textract-A --keys <ten keys>"
TRUE_PAIRS_ROOT=$EXACT make textract-run ARGS="--lane B --output results/textract-B --keys <ten keys>"
TRUE_PAIRS_ROOT=$EXACT make textract-run ARGS="--lane C --output results/textract-C --keys <ten keys>"
TRUE_PAIRS_ROOT=$EXACT make textract-score RUN=results/textract-A KEYS="<ten keys>"
make textract-run ARGS="--lane B --pdf <corridor>/corpus/files/e6/e619a4ab....pdf --output results/textract-9424-B"
make textract-semantics ARGS="--reading results/textract-9424-B/document.json --output results/semantics-9424-textract-B --env-file <corridor>/.env"
make semantics-eval ARGS="--reading results/semantics-9424-textract-B/document.json --gold <corridor>/gold/wsdot-9424.machine.csv"
```

`--offline` on `textract-run` renders and writes every page without a call
and lists the pending PNG shas; the PNGs are under `results/textract-png`.

## Tests

`textract/tests` runs without a network: block mapping, merged cells,
coordinate scaling on a rotated page, the lane A glyph assignment, the
cache and its retries, the render frame, and a run in the harness layout.
`textract/tests/fixtures` holds recorded responses (each file: the
displayed page size and the raw `AnalyzeDocument` response) that the
mapping test checks for well-formed pages; `synthetic-grid-merged.json`
was built in the response shape before Textract had run.

## Licences

boto3 and botocore (Apache-2.0), pypdfium2 5.13.0 (Apache-2.0 / BSD-3),
Pillow 12.3.0 (MIT-CMU), pypdf 6.17.0 (BSD-3), httpx (BSD-3). No PyMuPDF.
Textract itself is a metered AWS service: AnalyzeDocument Tables costs
$0.015 a page in us-east-2 (Layout free with Tables), quota 10
transactions a second.
