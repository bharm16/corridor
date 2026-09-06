# Bootstrap loop

Read every true pair's PDF, score it against its workbook, name every failure,
fix the largest class, rerun. The workbook is used only by the scorer; the
reader under test never sees it.

Corpus: `/Users/bryceharmon/Desktop/utility-conflict-matrices/PDF-Spreadsheet-Pairs/true-pairs/exact`
(351 pairs, 2,150 pages), the pairs whose PDF prints the workbook as it now
stands under the reader-independent check in `exactness.py`; the parent
directory holds all 524 pairs and `exact/EXCLUDED.csv` names the rest with
reasons, most of them workbooks edited after the print. `TRUE_PAIRS_ROOT`
selects the corpus; the make targets default to the exact set. `pairs.json` is
the sealed split over all 524: a fifth of the pairs, stratified by agency and
PDF producer family, is holdout and is never scored unless
`--include-holdout` is passed. Keys and holdout flags carry over to any subset.
Do not tune on the holdout.

## Run one iteration

```bash
make loop-reference ARGS="--output results/loop-reference-v3"   # once per reference change
make loop-read ARGS="--output results/loop-007 --engine tagged"  # new directory every run
make loop-score ARGS="--run results/loop-007 --reference results/loop-reference-v3"
make loop-inspect ARGS="--run results/loop-007 --reference results/loop-reference-v3 --pdf NAME.pdf --page 3"
```

`SUMMARY.md` in the run directory is the taxonomy: pairs and pages passing,
cells exact, failure classes with the pages and pairs they touch, subclasses,
the producer-family breakdown, the worst pairs and five examples per subclass.

## What passing means

A page passes when every reader cell lands on a reference cell with the same
display text, every reference cell in the columns the page maps is present,
spans agree, and every string outside a table is a header or footer. A pair
passes when every page passes and every reference cell is exact on some page.
Nothing is a percentage; every miss has a class.

Some things are recorded but do not fail a page, because the PDF cannot
contain them: text Excel clipped from a too-short wrapped row or cut at the
print area's edge (`value_mismatch/clipped_*`), results of formulas the
workbook stores without a cached value (`unverifiable/uncached_formula`), text
that overflowed into the page from cells outside the print area
(`unverifiable/print_area_overflow`), reader columns that refine one sheet
column without ever colliding (`columns_refined`), and rows of text the
workbook never held, printed before the first matched row or after the last
(`prose_row`): a document's title block or footer around a table. Every
workbook cell must still be found, so prose hides no loss.

Four more are read whole and in the right row, and only the print's lack of a
border or width explains the difference: a cell whose sheet column no reader
column maps, because the print never showed that column beside its neighbour
(`columns_folded`); two rows' cells drawn with no rule between them and read
as one cell holding both texts (`merged_rows/text_intact`); a General-format
number the column was too narrow to show in full (`rounded_to_width`); and
text cut mid-word at a rule or the next cell by a print that draws only what
fits (`clipped_edge`, from the reader's `cut` mark). A row split across two
pages matches each part as a clipped prefix or suffix.

## Reference corrections, logged openly

The answer key is the workbook's display strings under its own number formats.
These corrections were needed before the key could be trusted, and each one
changed the score without touching the reader:

- `ssf` 0.11.2 drops integer zero padding when a format also has decimals
  (`00.000000` printed `0.260000`); `fmt.mjs` restores it.
- Excel rounds a value for display as its 15-significant-digit decimal, half
  away from zero: a stored 47647.854999999996 prints `$47,647.86`. SSF rounds
  the binary value to `.85`. `fmt.mjs` rounds the way Excel does first.
- Excel's built-in formats 14 and 22 are locale short dates stored as
  `mm-dd-yy` and `m/d/yy h:mm`; a US Excel prints `m/d/yyyy`.
- Rows to repeat at top print on every page even when the print area starts
  below them.
- Sheet names can carry trailing spaces the manifest trimmed.
- Old BIFF workbooks can hide behind an `.xlsx` suffix and zip workbooks behind
  `.xls`; the bytes decide the reader.
- A print continues where the previous page stopped: a look-alike row already
  matched on an earlier page loses ties to a fresh one, so a summary block
  that repeats the numbers of the rows above it aligns to its own rows.
- A PDF whose Creator is Word was never printed from its workbook; the
  workbook copies the document's table, so its cell partition is not the
  page's. `nest_exact.py` leaves such pairs out of the exact set.

Known residue the loop cannot close: pairs whose workbook was edited after the
print (values differing by cents, retitled rows). They surface as
`value_mismatch/different` and `extra_value/not_in_workbook` and should be
listed as reference incompatibilities, never repaired from the PDF side.

## Files

- `corpus.py` loads the manifest; `split.py` seals the holdout.
- `exactness.py` decides per pair whether the PDF prints the workbook exactly;
  `nest_exact.py` clones the exact pairs into a nested directory with a
  manifest and an exclusion list.
- `reference.py` builds the key (openpyxl, xlrd, SSF through `fmt.mjs`).
- `read.py` runs `replacement.reader` and keeps cells plus text outside tables.
- `score.py` aligns tables to sheet rows and columns and classifies every cell.
- `inspect.py` prints one page as the scorer saw it.
- `tests/` covers every failure class the scorer names.

Two ADF billings (`2017-281-January-2022-billing`, `Copie-de-2017-281-November-2022-billing.mb`)
print pivot-table values the workbook no longer holds; the text check could not
see it because the same amounts appear elsewhere in the workbook. They are
listed in the exact set's `EXCLUDED.csv` as revisions.

Later on 2026-09-05 the scorer found twelve more pairs whose workbook was saved
after printing (nine Hoffman Structures billings holding later rows, two Kenco
and Hoffman pivot refreshes, one PCI summary cell) and one workbook converted
from its PDF; all are listed in the exact set's `EXCLUDED.csv`, which leaves
335 pairs.

## Synthetic set

`exact/synthetic/out` holds LibreOffice-rendered twins of the exact pairs.
Point the harness at it with `TRUE_PAIRS_ROOT=.../exact/synthetic/out
TRUE_PAIRS_SPLIT=bootstrap/pairs-synthetic.json`; its key is
`results/syn-reference-v1`. Two renderer facts the scorer names: LibreOffice
shows serial 0 as 12/30/1899 where Excel shows 1/0/1900 (`epoch_zero`), and it
draws only the glyphs that fit a cell, so a cut leaves no hidden run; the cut
is proven by the reader's `cut` mark, the column's wall, or the missing
letters not fitting before the next cell. Data defects found in the set: the
`.xls` twins cache 0 for string formulas (LibreOffice's xls export), and the
Hoffman Structures twins print pages of retainage zeros Excel never printed.
