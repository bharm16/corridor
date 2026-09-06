# Textract last-rung test (2026-09-05)

AWS Textract `AnalyzeDocument` with `FeatureTypes ["TABLES"]`, tested as the
last rung of the PyMuPDF replacement on ten dev pairs of the exact set
(53 pages, 14,641 reference cells) in three lanes, scored with the frozen
scorer against `results/loop-reference-v6` and compared with the frozen
reader on the same pages. Code: `textract/` (see `textract/README.md`);
branch `codex/textract-rung`, worktree `pdf-reader-comparison-textract`.

## What was run

- Documents: 5007_20-08-Billing, REVISED-McClean-December-SOV,
  12.2025-Kenco-SOV, 19132-71-VE-June-2023 (Excel);
  Billing-Review-Comments-Disposition-Est-95, status-mobility-fy2019
  (PrintToPDF); PE-40_Change-Order-224, 85531_1010_Index (Ghostscript);
  GEA_608-ROWEstimate_20170502 (Adobe PDF Library); status-mobility-fy2018
  (Distiller). Keys in `results/ten-keys.txt`; all ten are dev pairs.
- Lane A: the original page rendered by pypdfium2 at 300 dpi in grayscale;
  Textract supplies cell geometry only, the reader's glyphs (`read_pdf`,
  engine `tagged`) are assigned to the cell polygon holding each glyph's
  centre and read with `ordered_text`; Textract's words are never stored.
- Lane B: the clean scan twin under `true-pairs/exact/scans-clean` rendered
  at its native 300 dpi; Textract's words are the values.
- Lane C: the degraded twin under `scans-degraded` (200 dpi, one degree of
  skew, JPEG 70, noise) rendered at its native 200 dpi.
- Lanes B and C keep the exact root's pair keys and score against the same
  answer key: the twins' workbooks are APFS clones of the originals and the
  twins keep the page size in points, so one key scores all three lanes and
  the same pages compare. The twin roots were complete for all ten
  documents; nothing was re-rendered.
- The grayscale 300 dpi render of an original is pixel-identical to the
  render of its clean twin, so lanes A and B share one Textract call per
  page and differ only in where the text comes from.
- Transport: the local `corridor` profile's login session had expired, so
  the calls ran through the Claude AWS connector's sandbox. PNGs went to a
  private scratch bucket `corridor-textract-rung-9593` (us-east-2, public
  access blocked) by presigned URL, the sandbox called AnalyzeDocument on
  each S3 object and wrote the cache entry back to S3, and the entries came
  down by presigned URL into `results/textract-cache`
  (`textract/transport.py`). The bucket still holds the 116 PNGs and 116
  entries; delete it when the cache here is enough.
- Cost: 116 pages sent, one call per distinct raster: 52 for lanes A and B
  (85531's two blank pages render identically), 53 for lane C, 11 for
  WSDOT 9424. At $0.015 a page that is $1.74 at list price; the account's
  Free Tier, if it applies, covers 100 of them. Every re-run and re-score
  since has cost nothing.
- Environment facts not in the repo: the root `.venv` needed `openpyxl`
  3.1.5 and `xlrd` 2.0.2 (`uv pip install`) and `paired_trial` needed
  `npm install` (ssf) before `make loop-reference` would build the key,
  which then matched the main checkout's key file for file on all ten
  pairs; pytest needs `tmp/` to exist (`make textract-test` creates it);
  `botocore[crt]` is needed for the profile's login credential provider.

## Results per lane

Pairs, pages and reference cells exact, dev keys, gate unchanged.

| Lane | Pairs | Pages | Cells exact | Excel (4 pairs, 33 pages) | Ghostscript (2, 11) | PrintToPDF (2, 6) | Adobe PDF Library (1, 1) | Distiller (1, 2) |
|---|---:|---:|---:|---|---|---|---|---|
| Frozen reader, engine tagged (baseline) | 9 / 10 | 53 / 53 | 14,615 / 14,641 | 4/4 pairs, 33/33 pages, 12,285/12,303 | 1/2, 11/11, 1,339/1,345 | 2/2, 6/6, 317/317 | 1/1, 1/1, 571/571 | 1/1, 2/2, 103/105 |
| A: Textract geometry, native glyphs | 0 / 10 | 25 / 53 | 14,342 / 14,641 | 0/4, 17/33, 12,123/12,303 | 0/2, 8/11, 1,311/1,345 | 0/2, 0/6, 284/317 | 0/1, 0/1, 556/571 | 0/1, 0/2, 68/105 |
| A + run-mate rescue (variant, off by default) | 0 / 10 | 28 / 53 | 14,349 / 14,641 | 0/4, 19/33, 12,129/12,303 | 0/2, 9/11, 1,312/1,345 | 0/2, 0/6, 284/317 | 0/1, 0/1, 556/571 | 0/1, 0/2, 68/105 |
| B: clean twin, Textract words | 0 / 10 | 7 / 53 | 13,450 / 14,641 | 0/4, 3/33, 11,816/12,303 | 0/2, 4/11, 749/1,345 | 0/2, 0/6, 262/317 | 0/1, 0/1, 556/571 | 0/1, 0/2, 67/105 |
| C: degraded twin, Textract words | 0 / 10 | 6 / 53 | 12,427 / 14,641 | 0/4, 2/33, 10,844/12,303 | 0/2, 4/11, 710/1,345 | 0/2, 0/6, 260/317 | 0/1, 0/1, 553/571 | 0/1, 0/2, 60/105 |

The baseline's one failing pair is PE-40, the known residue (six cells of
its borderless header block uncovered); its cells short of 14,641 are the
clipped and folded artifacts the gate ignores. The Adobe PDF Library and
Distiller family cells above come from the baseline's own score files
(`results/reader-ten`).

### Failing classes, pages touched

Lane A (the gate's failing classes only):

| Class | Pages | What it is |
|---|---:|---|
| outside_table/cell_text_outside | 17 | Workbook cells Textract left outside every TABLE: the unruled title block of every Excel billing (`BILLING SUMMARY`, `Sub: Valley Electric ...`, `Progress Billing No.`, dates), which the sheet holds as cells and the reader's tag tree reads as cells. |
| span_mismatch/wider | 16 | Textract cells spanning more columns than the sheet's merged range (`Sub: Holmberg Co. ...`, `Install Electrical Utility Racks ...`, `Source of Revenue`). |
| unexplained_text/numeric | 14 | Accounting-format cells whose `$` and `-` (or `$` and amount) are separate text objects; Textract's tight cell polygon holds one and the other lands outside as its own string. |
| unexplained_text/edge | 11 | Margin text the reader normally explains through the tag tree: an author's hidden instruction line (`BEFORE FINAL PRINTING ...`), a Symbol-font mark, stray `,.` runs. |
| unexplained_text/text | 8 | PE-40's borderless header sentences and McClean fragments (see below). |
| value_mismatch/different | 3 | `00.220000` read as `00220000`, ` $2,851.64 ` as `$ 285164`: the period and comma glyphs sit at the baseline just below Textract's cell polygon and fall outside it. |
| merged_cells, missing_cell, unaligned_row, others | 1–2 each | A cell polygon covering only part of a text run leaves fragments (`ubcontracto. 55-`, `ngerod`) as rows of their own. |

Lane B:

| Class | Pages | Cells | What it is |
|---|---:|---:|---|
| value_mismatch/different | 34 | 726 | 355 underscores dropped from file paths in the 85531 index (`1010_Roadway_Gateway\...` read as `1010 Roadway Gateway\...`), 261 hyphens dropped or moved (`BID ITEM #01 - Valley Electric` read without the first ` - `; accounting `$ -`), 24 one-or-two-character confusions, 76 other. |
| unexplained_text/edge, text, numeric | 22, 10, 10 | 161 | Title-block LINEs that merge two cells' text (`Project: ... • Seattle, WA`) or split one (`$`, `-`), and the contractor's logo text. |
| outside_table/cell_text_outside | 18 | 142 | The same unruled title blocks as lane A, now as Textract LINEs. |
| value_mismatch/format_only | 17 | 137 | ` $-   ` read as `$`: the accounting dash lost. |
| span_mismatch/wider | 14 | 23 | As lane A. |
| value_mismatch/unicode_variant | 9 | 34 | Case: `systems c` read as `systems C`. |
| others | 1–2 each | 15 | Merged title cells, a value moved out of its cell. |

Lane C adds to lane B's classes: value_mismatch/format_only on 28 pages
(856 cells: amounts without their `$`, `$` alone), extra_value on 9 pages
(162 cells, mostly a `$` that became a cell of its own), reader_has_more
on 18 pages (88 cells: two rows or a header and a value read as one cell,
`2\n300`, `#\nRef Doc`), missing_value/in_table_elsewhere on 10 pages
(142 cells: values shifted into a neighbouring cell, whole columns of
`00.260000` cost codes on 19132 page 7), and 876 `different` cells
(319 underscores, 243 hyphens, 145 punctuation only). Under one degree of
skew Textract's table structure degrades as much as its words: cells
absorb neighbours across rows and columns.

Textract returned only a PAGE block for 85531's pages 6 and 9, which are
blank, and one to eleven blocks for its pages 7, 8 and 10, which hold a
few lines; those reads are correct.

### Lane A versus lane B on the same pages

Same pixels, same Textract response, different text source. Every page
lane B passes lane A passes too (19132 page 2, 5007 pages 2 and 9, 85531
pages 6, 7, 9, 10); lane A passes 18 more. Cells exact 14,342 against
13,450: re-mapping the document's glyphs into Textract's geometry recovers
892 of the 1,191 cells Textract's words lose, and the 299 it still misses
are all structural (title blocks outside any table, widened spans,
punctuation outside tight polygons), none a value misread. Re-mapping
native glyphs beats Textract's words wherever a text layer exists; Textract
should never supply words for a page that has one.

Two lane A variants were measured, both off by default so the specified
lane stands. Growing every cell polygon by a margin: 1 pt gives 27 pages
and 14,343 cells, 2 pt 16 pages and 14,025, 4 pt 11 pages and 8,554; the
margin captures neighbouring cells' glyphs faster than it recovers the
baseline punctuation. Run-mate rescue, where a glyph no polygon holds
joins the cell holding most of its own text object, gives 28 pages and
14,349 cells with no new class: it fixes the period-and-comma cells and
the fragment rows and touches nothing else, and would be the rule to
adopt if lane A were ever shipped.

### Does Textract's confidence see its misreads?

No. In lane B, 695 of the 901 mismatched cells (value classes) carry a mean
word confidence of 95 or more; the median confidence of mismatched cells
is 96.7 against 99.8 for cells read exactly. A threshold of 99 flags 662 of
the 901 misreads but also 2,893 of 13,877 good cells; a threshold of 95
flags 206 of 901. Lane C is the same shape (1,352 of 1,841 mismatches at
95 or more). Dropped underscores and hyphens are read with high
confidence. Confidence is not a gate.

## Semantics check, WSDOT 9424 (11 pages)

The unchanged OpenAI tier (`replacement.semantics`, cell IDs only) over the
Textract pages of the WSDOT 9424 utility listing, with the page image
downscaled to 110 dpi from the raster each page was read from
(`results/semantics-9424-textract-B`, `-A`), against Corridor's machine
gold:

| Lane | Gold rows | Extracted | Matched | Recall | Precision | Prompt tokens (cached) | Completion |
|---|---:|---:|---:|---:|---:|---:|---:|
| B: Textract words | 162 | 162 | 162 | 100% | 100% | 74,214 (23,940) | 3,355 |
| A: Textract geometry, native glyphs | 162 | 162 | 162 | 100% | 100% | 75,370 (26,334) | 3,355 |

The model mapped the same five fields and the four marked resolution
columns on every page in both lanes, chose the same header row and table,
and extracted the same rows page by page. Lane B declined 99 retired rows
and two rows missing a required field; lane A declined 101 retired rows.
The reader's own run scored 162 of 162 on the same document.

## Recommendation

"Fails the gate" at runtime should mean the reader and the OCR rung could
not produce a table where the Page Inventory says one is, and never a
confidence figure: Textract's own confidence does not separate its
misreads, the exact gate is never met by Textract on any of the ten pairs
(0 of 10 in every lane, against the reader's 9 of 10), and its structural
limits are the same on every lane (unruled title blocks outside every
table, spans widened across merged ranges), so the trigger has to be
structural too. Of the three candidates, an inventory table region with no
table read is the one to adopt, evaluated after the OCR rung and before
the model transcription tier; a matrix page with no header and no carried
mapping is already handled by the semantics tier's own refusal and needs
no Textract call, and the unreadable-page check belongs to ADR-0064's
rescue path, which Textract cannot pass through unverified. On what
Textract earns: as a source of values it does not earn the last rung on
this evidence, reading 91.9% of cells exactly on clean scans and 84.9% on
degraded ones with confidently wrong underscores, hyphens and accounting
dashes, below the local OCR rung's 96.0% on its own degraded probe set
(a different set of pairs, so a bound, not a head-to-head); as a source of
table geometry it is good, and its cell polygons carried the semantics
tier to 162 of 162 on 9424 in both lanes, so the narrow role it can earn
is table-region geometry on a scanned page where the rule detector finds
no table, with values always taken from the document's glyphs when a text
layer exists and from the OCR rung's agreed tokens when none does, and
every Textract read still corroborated before it is believed.
