# Loop log

Dev set only (420 pairs, 3,123 pages, 1,392,498 reference cells); the 104-pair
holdout stays sealed. "Pages" and "pairs" are full passes under the gate in
`README.md`. Each row names what changed between runs.

| Run | Pairs | Pages | Cells exact | What changed |
|---|---:|---:|---:|---|
| loop-000 (first score) | 0 | 69 | 769,163 | Baseline: drawn-grid reader only, first answer key |
| loop-000 (key v2) | 0 | 914 | 816,752 | Reference corrections only: print-title rows, locale dates, SSF zero padding |
| loop-001 | 3 | 1,070 | 822,905 | Excel tag tree joined to glyphs by marked-content id; leftover glyphs cut by drawn rules |
| loop-002 | 7 | 1,119 | 822,937 | Alignment decides a cell's rule interval (overflow left or right) |
| loop-003 | 3 | 1,049 | 829,980 | Standalone paragraphs as cells; a loose "rule stops nearby" merge test that over-merged (reverted) |
| loop-004 | 11 | 1,157 | 829,966 | Glyphs outside a text object's clip box dropped; merges only when centred; scorer lets reader columns refine one sheet column |
| loop-005 | 46 | 1,224 | 830,197 | Edge-aligned sub-columns inside unruled stretches; numbers align right, text left |
| loop-006 (rescored) | 21 | 1,310 | 842,941 | Every leftover text run becomes a cell on any page; pagination cells in the margin explained |
| loop-007 | 21 | 1,342 | 843,781 | Clip judged per text object; hidden text reported as evidence; currency signs joined to amounts across foreign rules; hashes for too-wide numbers named |

The 589-page TxDOT Congestion Mitigation report (Reporting Services, one record
per page laid out as a form) is 19% of the dev pages and passes none of them;
it needs a transposed-record reader of its own and is counted, not hidden.
| loop-008 | 55 | 1,412 | 843,879 | Rule-gap merge inference for numbers removed (it fired on rows that merely lack a border); lone symbols such as the Symbol-font bullet joined to the run beside them; two-sided clipped evidence |

Holdout check on loop-007 reads (never tuned on): dev 1,342/3,123 pages (43.0%), holdout 316/712 (44.4%);
Excel family cells exact dev 99.52%, holdout 99.54%. No sign of overfitting to the dev set.
| loop-009, key v4 | 101 | 1,462 | 843,099 | Answer key rounds like Excel (15 significant digits, half away from zero); currency signs join only amounts or accounting dashes |

The one-cent class fell from 970 cells in 67 pairs to 4 cells in 2 pairs: those were the key's rounding, not workbook edits.

## Exact subset

`exactness.py` (extracted text only, no table structure) finds 351 of 524 pairs,
2,150 of 3,835 pages, whose PDF prints the workbook as it now stands; they are
cloned into `true-pairs/exact/`. Excluded: 173 pairs, of which 60 miss printed
cells, 36 print strings the workbook lacks, and 77 both; plus 119 duplicate
listings. Loop-009 against key v5 on the exact dev set (280 pairs, 1,730 pages):

| Set | Pairs | Pages | Cells exact |
|---|---:|---:|---:|
| exact dev, all families | 100 / 280 | 1,230 / 1,730 | 536,229 / 541,373 (99.05%) |
| exact dev, Excel exports | 97 / 236 | 1,175 / 1,501 | 524,390 / 526,640 (99.57%) |
| loop-009, key v6, exact set | 164 / 280 | 1,435 / 1,730 | 537,779 / 541,373 | Key keeps the sign of small values through rounding (Excel prints (0.00), not the zero section); scorer treats any column of a merged range as that cell |
| loop-010, key v6, exact set | 91 / 280 | 1,373 / 1,730 | 538,279 / 541,373 | Print-driver runs split at wide gaps; loose runs join a rule-framed band: Print To PDF rose from 38 to 150 pages, Excel fell because header blocks without inner rules were joined to the wrong band (fixed in loop-011 by requiring exactly one band in the frame) |
| loop-011 | 91 / 280 | 1,373 / 1,730 | 538,279 / 541,373 | Same reads as loop-010: the unique-band frame rule alone changed nothing on the corpus |
| loop-013, key v6, exact set | 192 / 280 | 1,583 / 1,730 | 539,039 / 541,373 | Untagged runs join the rule-framed band of their row only when the frame holds one band and no member overlaps them in x; bands inside a larger band merge |
| loop-017, key v6, exact set | 205 / 280 | 1,611 / 1,730 | 540,382 / 541,373 | Runs split per line and rejoined structurally: adjacent words, superscript marks on either side, stacked lines with no rule between and either a ruled row frame or a lower line standing alone; bands grow only on framed joins; merged cells vote at half weight; print-area overflow and page-edge cuts named |
| loop-018, key v6, exact set (350 pairs; one Word-authored pair excluded) | 235 / 279 | 1,671 / 1,723 | 540,242 / 541,087 | Orphan marked content grouped per MCID (Excel's untagged repeated print titles become whole cells); Artifact-marked text (page numbers) kept out of tables; thin dashes and hyphens matched to their line; adjacent words reach 0.55 of the taller run and a closed sentence reaches a full height; open stacking only for left-aligned lines that both stand alone; scorer: a row already matched on an earlier page loses ties, hidden runs on the cell's line explain a print-area cut, and text the workbook never held before or after the table is a prose row |
| loop-019, key v6, exact set (348 pairs; two ADF pivot revisions excluded) | 253 / 277 | 1,690 / 1,713 | 537,478 / 538,084 | Reader: tagged rows band only on substantial overlap; a row's tagged extent comes from its ordinary cells; row spans need half a band; edges unite before centres; a drawn-grid cell the page cuts at the top or bottom is kept. Scorer: rows split across pages match as clipped continuations; a row hidden behind a short row height is explained by hidden runs; hashes stand for a date or a number the mapping does not reach; page numbers never anchor rows but a lone sheet cell holding one still matches; footers drawn as several runs read as one line; a mid-word cut at the next cell or the table's edge is a clipped edge; a cell in a column the reader folded into its neighbour is noted, not failed; a partial vertical merge is an unreported merge |
| loop-020, key v6, exact set (335 pairs; twelve workbooks saved after printing and one converted workbook excluded) | 262 / 264 | 1,593 / 1,593 | 484,365 / 484,493 | Reader: drawn-grid cells band by their drawn row and span by their rectangle; a tagged row takes its ruled frame unless another TR sharing its columns lies in that frame; text stopping in a word within a glyph of a vertical rule carries a cut mark. Scorer: pages holding only cell fragments past a horizontal page break are continuations; a lone page number takes the nearest lone sheet row; letters-only hidden evidence; General-format width rounding; two rows drawn without a rule between them read as one cell; a page with no table and only cell fragments is a continuation; a column whose cells stop at one wall clips its text |

## Synthetic set (`exact/synthetic/out`, LibreOffice prints; split `bootstrap/pairs-synthetic.json`, key `results/syn-reference-v1`)

| Run | Pairs (dev) | Pages | Cells exact | What changed |
|---|---|---|---|---|
| syn-000 (loop-018 reader) | 186 / 281 | 1,311 / 1,627 | 535,664 / 536,967 | Baseline |
| syn-001 (loop-019 reader) | 233 / 281 | 1,560 / 1,627 | 535,678 / 536,967 | Footers drawn as several runs read as one line; cut mid-word at the next cell |
| syn-003 (loop-020 reader, final scorer) | 261 / 281 | 1,592 / 1,627 | 535,687 / 536,967 | Cut marks and column walls; day-zero dates (Excel 1/0/1900, LibreOffice 12/30/1899); a row's only cell whatever column its centred text landed in |

Residue the loop cannot close on the synthetic set: the 15 dev `.xls` twins
store 0 where LibreOffice's xls export should have cached formula strings
(their pages print text the key does not hold); H-S-I twins print pages of
retainage zeros that Excel never printed; the R06_TOC twin descends from a
converted workbook.
