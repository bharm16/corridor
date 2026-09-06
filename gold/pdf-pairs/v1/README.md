# Paired-rendition Reference Dataset, v1

This directory registers the Extraction Measurement that the paired-rendition
reader was built against, so that every integration ticket of #727 measures
its change on one registered basis (#731). A Reference Dataset is the
identified comparison data of an Extraction Measurement, with its origin, its
coverage and whether its expected values were independently established
(`docs/operations/CONTEXT.md`); this one is the 333-pair corpus the reader
package (`src/corridor_pdf_reader`, imported unchanged from commit `c39363e`
by #729) was measured on as loop-020.

Everything here is machine-readable and checked by
`tests/test_pdf_pairs_registry.py` without the corpus:

| File | What it holds |
|---|---|
| `dataset.json` | The registration (`corridor.pdf-pairs-dataset.v1`): every pair with its digests, agency, producer family, page count and holdout flag; every exclusion with its reason; the split; the grouping rule; the gate; the scorer's taxonomy; the holdout policy; the limits paragraph. Built by `python -m corridor_pdf_reader.registry` from the corpus manifests and the sealed split, never retyped. Its digest is cited by every receipt. |
| `holdout-access.jsonl` | Every access to the spent holdout (`corridor.pdf-pairs-holdout-access.v1`): the three that predate this harness and every run of the measurement command that included the holdout, each with its date, actor, reason, purpose, configuration identity and result. Append-only. |
| `receipts.json` | The index of baseline receipts (`corridor.pdf-pairs-receipts.v1`): the receipts #729 imported, referenced by path and digest rather than copied, and the receipts this harness retained. |
| `receipts/<run>/` | Receipt sets the measurement command retained: `receipt.json`, `RECEIPT.md`, the scorer's `SUMMARY.md` and `summary.json`, `tally.txt`, the read receipts and digests, and every per-pair score under `scores/`. |
| `pdf-v1/<run>/` | The frozen reader scored through the existing PDF evaluation contract (`gold/pdf/v1`): the engine run, the evaluation JSON and Markdown, the adapter's frame record and the per-threshold record. |

## The dataset: origin, exclusions, split

**Origin.** The corpus is `utility-conflict-matrices/PDF-Spreadsheet-Pairs/true-pairs`, 524 unique
pairs of a workbook and a PDF printed from it, gathered from public agency
records (WSDOT, ODOT-Ohio, TxDOT, IowaDOT). The registered set is its
`exact/` subset: the 333 pairs whose PDF prints the workbook as it now stands
under the reader-independent text check in `bootstrap/exactness.py`, less the
revisions the scorer later found. The corpus's `MANIFEST.csv` (digest
`ca68b55a…`) is the only source of pair identity; a pair's key is the first
sixteen hex digits of the SHA-256 of its two file digests concatenated, as
`bootstrap.corpus` derives it. The producer family of each PDF (Excel, Print
To PDF, Ghostscript, Adobe PDF Library, Distiller) was read once from
Poppler's `pdfinfo` when the split was sealed and lives in the split file.

**Exclusions.** `EXCLUDED.csv` (digest `5f2e1d49…`) names all 310 rows left
out of the exact set with a reason each: 119 duplicate listings of the same
bytes under another folder, and 191 pairs whose print and workbook disagree
(workbook cells not printed, printed strings the workbook lacks, workbooks
saved after printing with later billing rows and retainage formulas, pivot
tables refreshed after printing, a workbook converted from its PDF, a
Word-authored PDF whose table the workbook copied). Every row is in
`dataset.json` under `exclusions`.

**Split.** `src/corridor_pdf_reader/bootstrap/pairs.json` (digest
`529c5ffb…`) seals the split: seed 720, one fifth held out, stratified by
agency and producer family, over all 524 unique pairs; the keys and holdout
flags carry over to the exact subset unchanged. On the 333 pairs that is 263
development pairs (1,589 pages) and 70 holdout pairs (409 pages).

**Grouping rule.** Originals, clean scan twins, degraded twins and closely
related revisions stay together across development and holdout. A scan twin
of a development workbook is not holdout evidence. The synthetic set and the
scan twins keep the exact set's pair keys and therefore its holdout flags.

**Expected values.** The answer key is the workbook's own display strings
under its own number formats, built by `bootstrap.reference` from the
workbook bytes with openpyxl, xlrd and SSF; the reader reads the PDF with
pypdfium2 and pypdf and never sees the workbook, and no parser, table
detector or engine is shared between the two. The key's corrections are
logged in `bootstrap/README.md` and each changed the score without touching
the reader. The key is what the workbook displays, not human gold: a pair
whose workbook was edited after printing is excluded as a revision, never
repaired from the PDF side (ADR-0023). The keys are not committed; the
measurement command rebuilds them and refuses any key whose digest is not
the one retained in `src/corridor_pdf_reader/receipts/loop-reference-v6.manifest.json`.

## The holdout: spent, and every access on the ledger

The holdout was scored at loop-007 (316 of 712 pages, on the full 524-pair
corpus, before the exact subset existed), at loop-020 (70 of 70 pairs, 409
of 409 pages), by #729's reproduction on 2026-09-06 (70 of 70, 409 of 409,
125,498 of 125,529 cells exact), and by this harness's baseline run the same
day, with the same numbers. Each is a line of `holdout-access.jsonl`.

Imported holdout cases are spent for the frozen reader. Re-running the
frozen implementation is a reproducibility and regression check, not a new
generalization claim. Changing the reader after inspecting those outcomes
requires a new held-out evaluation for a new generalization claim
(ADR-0008). The measurement command refuses a holdout run without
`--include-holdout`, `--holdout-actor` and `--holdout-reason`, and appends
every access it makes, with its result, to the ledger, whether the run
finished or failed.

## The gate and the taxonomy

A page passes when every reader cell lands on a reference cell with the same
display text, every reference cell in the columns the page maps is present,
spans agree with the workbook's merged ranges, and every string outside a
table is header or footer text. A pair passes when every page passes and
every reference cell is exact on some page. Nothing is a percentage; every
miss has a class.

`dataset.json` registers, under `taxonomy`, every class and subclass
`bootstrap/score.py` emits with its meaning and whether the gate counts it,
and `tests/test_pdf_pairs_registry.py` holds that table to the scorer's
source both ways. The failing classes are `value_mismatch` (different,
format only, unicode variant, reader has more, reader has less),
`missing_value`, `missing_cell` and `missing_row` (each as outside the
table, elsewhere in the table, or absent), `extra_value` (wrong column, wrong
row, misplaced, not in the workbook), `unaligned_row`, `unaligned_table`,
`span_mismatch` (wider, narrower, rows), `merged_cells`, `outside_table` and
`unexplained_text` (numeric, edge, text). Recorded but never failing, because
the PDF cannot hold them: the clipped prefix, suffix, middle, evidence and
edge; overflow hashes; rounded to width; epoch zero; uncached formulas;
print-area overflow; folded columns; merged rows with the text intact; and
prose rows. Refined columns and unreported merges are counted on each table;
continuation pages, pagination lines and lone page-number cells are
explained rather than failed; and a reference cell never found exact on any
page is `uncovered` (as a value mismatch, outside a table, or absent) and
fails the pair. The unmatched regions on either side are therefore
`unexplained_text` on the page and `uncovered` in the workbook.

## Baseline receipts

`receipts.json` indexes every receipt by path and digest with its
configuration identity. Pair, page and cell measures are three readings that
must never be collapsed into one number.

| Run | Role | Configuration | Development | Holdout |
|---|---|---|---:|---:|
| `loop-020` | imported baseline (`src/corridor_pdf_reader/receipts/loop-020`) | engine `tagged`, 36 dpi, commit `c39363e` | 262 / 263 pairs, 1,589 / 1,589 pages, 483,210 / 483,336 cells | 70 / 70 pairs, 409 / 409 pages (cells not retained) |
| `reproduction-2026-09-06` | imported reproduction (#729) | the same, from the declared environment | identical | 70 / 70, 409 / 409, 125,498 / 125,529 |
| `syn-003` | imported synthetic set (LibreOffice twins; not the registered dataset) | the same reader, final scorer | 261 / 281 pairs, 1,592 / 1,627 pages, 535,687 / 536,967 cells | never scored |
| `reader-ten` | imported Textract lane baseline, ten development pairs | engine `tagged` | 9 / 10 pairs, 53 / 53 pages, 14,615 / 14,641 cells | none |
| `textract-A` | imported lane A: Textract geometry, native glyphs | `textract-A` | 0 / 10, 25 / 53, 14,342 / 14,641 | none |
| `textract-B` | imported lane B: clean scan twins, Textract words | `textract-B` | 0 / 10, 7 / 53, 13,450 / 14,641 | none |
| `textract-C` | imported lane C: degraded twins, Textract words | `textract-C` | 0 / 10, 6 / 53, 12,427 / 14,641 | none |
| `textract-A-margin1`, `-margin2`, `-margin4`, `-runs` | imported lane A variants, off by default | `textract-A` | 27, 16, 11 and 28 of 53 pages | none |
| `frozen-reader-2026-09-06` | **this harness's baseline** (`receipts/frozen-reader-2026-09-06`) | `frozen-reader`: engine `tagged`, 36 dpi, commit `c39363e`, pypdfium2 5.13.0 (PDFium 153.0.7999.0), pypdf 6.17.0 | 262 / 263, 1,589 / 1,589, 483,210 / 483,336 | 70 / 70, 409 / 409, 125,498 / 125,529 |
| `drawn-grid-ten-2026-09-06` | **failure proof** (`receipts/drawn-grid-ten-2026-09-06`) | `drawn-grid`: engine `pdfium`, no structure tree, ten development pairs | 0 / 10, 23 / 53, 12,844 / 14,641; 9 pairs regressed, 30 pages fail | not read |

The synthetic set's data defects are named on its index entry: the fifteen
development `.xls` twins store 0 where LibreOffice's export should have
cached formula strings, the Hoffman Structures twins print pages of retainage
zeros Excel never printed, and the R06_TOC twin descends from a converted
workbook. The Textract lanes' cells are Textract-only values and remain
unconfirmed readings (ADR-0094).

## The command

```bash
make pdf-pairs-measure ARGS="--configuration frozen-reader --output out/pdf-pairs/<run>"
make pdf-pairs-measure ARGS="--configuration drawn-grid --output out/pdf-pairs/<run> --keys <key> ..."
make pdf-pairs-measure ARGS="--configuration <name> --output out/pdf-pairs/<run> --include-holdout --holdout-actor <who> --holdout-reason <why> --retain measurement"
```

`corridor_pdf_reader.measurement` checks that the corpus manifest and the
split are the registered ones and that the package matches commit `c39363e`,
hashes every corpus file, builds the answer keys for the selected pairs and
refuses any whose digest differs from the retained one, reads every selected
PDF with the named configuration through `bootstrap.read` (a process pool,
one PDFium per process), scores each read with the imported scorer, tallies,
and writes `receipt.json` and `RECEIPT.md`. The receipt keeps pair, page and
cell measures apart and development and holdout apart, names the
configuration identity (engine, dpi, jobs, commit, package digest, every
engine version, the machine) and the registration digest, compares every
pair with the registered baseline's retained scores (a pair the baseline
passed and this run fails is a regression, as is a page or an exact cell
lost), and carries the limits paragraph. `--retain baseline|failure-proof|measurement`
copies the receipt set into `receipts/<run>/` and indexes it. Needs the
corpus at `TRUE_PAIRS_ROOT`, `make pdf-reader-node`, and about two and a
half minutes for the whole set; an explicit experiment outside CI. A new
configuration is an entry in `measurement.CONFIGURATIONS`; measuring it on
the development set is how an integration ticket checks its change, and
retaining the receipt is how the result enters this registry.

## The frozen reader through `gold/pdf/v1`

`make pdf-reader-gold-eval` reads the six gold documents (eight labelled
pages) from the content store through the isolated reader process, writes an
engine run in the contract's shape with `corridor_pdf_reader.gold_evaluation`,
and evaluates it with `corridor.pdf_evaluation_cli`. The run is retained under
`pdf-v1/frozen-reader-2026-09-06/`; the FDOT holdout access is the
2026-09-06 line of `gold/pdf/v1/holdout-access.jsonl`. Nothing was tuned on
what the run showed.

The adapter carries the reader's own geometry and marks absent what the
reader does not produce. Three facts about the two shapes decide most of the
numbers and are recorded in `adapter.json`:

- The gold declares its pages in a nominal frame that is not the document's
  crop box for the three tabloid documents (792 by 612 points declared, 1,224
  by 792 in the PDF; the NHHIP document is declared rotated 90 degrees and is
  not). The adapter scales the reader's displayed frame onto the declared
  frame and records the scale per page.
- The reader's cell geometry is the text's box, not the ruled cell rectangle;
  rows and columns are bounding boxes of those text boxes. The gold's cells
  are rectangles, and it labels a representative sample of each table's cells,
  so every unlabelled reader cell counts as a false positive.
- The frozen reader has no page classifier, no disposition, header, canonical
  mapping or state tier, and emits no proposal or page-scoped value; those
  belong to #734 and #737.

| Threshold | Frozen reader | Applies | Why |
|---|---|---|---|
| `page_coverage` | met (8 of 8 pages) | yes | every labelled page was read |
| `page_class_accuracy` | not met (0 of 8) | no | every page is the sentinel `unclassified` |
| `table_f1` | not met (0.727: 4 matched, 3 unlabelled tables found on a cover page, an ambiguous page and an agreement) | yes | |
| `row_f1` | not met (0 of 12 gold rows matched; 216 reader rows) | yes | text-box rows against labelled bands |
| `column_f1` | not met (1 of 13 matched; 127 reader columns) | yes | text-box columns against labelled bands |
| `cell_f1` | not met (0 of 29 gold cells matched; 1,976 reader cells) | yes | text boxes against cell rectangles, on a sample |
| `cell_text_exact`, `cell_span_exact`, `cell_topology_exact` | met vacuously (0 of 0 matched cells) | yes, over matched cells | no cell matched, so nothing was compared |
| `row_disposition_exact`, `header_relationships_exact`, `canonical_mapping_exact`, `cell_state_exact` | met vacuously (0 of 0) | no | placeholders or absent layers |
| `page_scoped_values` | not met (the one labelled value was not emitted) | no | no semantics tier was run |
| `wrong_source_cited_proposals` | met vacuously (7 opportunities, 0 emitted) | no | no proposal was emitted |
| `failure_rate`, `abstention_rate` | met (0 of 6 documents failed, 0 of 8 pages abstained) | yes | |
| `latency`, `memory` | met (5,406 ms over six documents, 901 ms average; 181 MB peak) | yes | measured in the isolated reader process |

The contract scores the run as a fail, and the informative thresholds are
not all met. That is the record: the frozen reader's exactness is measured by
the paired-rendition gate above, and the gold/pdf/v1 geometry layers will
measure the reader once #736 gives its cells rectangles and #737 gives its
rows dispositions and mappings.

## Limits, stated in every receipt

Paired renditions are the measurement method, not a prerequisite for reading
a production PDF. Cell comparison does not independently establish prose
outside tables, raster fidelity, routing correctness, source-locator replay,
malicious-document handling, production concurrency, or operational cost and
coordinator burden. Those belong to their own tickets.
