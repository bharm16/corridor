# Scanned twin measurement: 739-textract-degraded-twins

Corridor's record of the scanned reading measured on the degraded twins (200 dpi, one degree of skew, JPEG 70, noise), recorded from the retained standalone lane rather than re-run: the responses are retained, the corpus twins are outside this repository, and no provider call was made to write this receipt. Every number below is recomputed from the retained per-pair score files and checked against the lane's own summary (#739).

Configuration `textract-scanned-degraded`: Textract AnalyzeDocument with FeatureTypes [TABLES] over the degraded twins (200 dpi, one degree of skew, JPEG 70, noise); Textract's words are the values, which makes every one of them an Unconfirmed reading. Rendered at 200 dpi; twins under `true-pairs/exact/scans-degraded`; reader commit `c39363e`.

Reference Dataset `2026-09-06.1`, registration `46d5a66491d1ac04`, corpus manifest `ca68b55abfa583a1`, split `529c5ffb00bc96f5`; ten development pairs, no holdout.

## Measures

Three readings, kept apart.

| Measure | Result |
|---|---:|
| Pairs exact | 0 / 10 |
| Pages exact | 6 / 53 |
| Reference cells exact | 12,427 / 14,641 |

Three readings that must never be collapsed into one number: a pair passes only when every one of its pages passes, so the cell count says nothing about the pair count and neither says anything about the page count.

| Document | Family | Pair passes | Pages | Cells exact |
|---|---|---|---:|---:|
| 19132-71-VE-June-2023.pdf | Excel | no | 1 / 13 | 4,357 / 4,858 |
| 12.2025-Kenco-SOV.pdf | Excel | no | 0 / 6 | 1,826 / 2,106 |
| REVISED-McClean-December-SOV.pdf | Excel | no | 0 / 5 | 1,745 / 1,925 |
| Billing-Review-Comments-Disposition-Est-95.pdf | PrintToPDF | no | 0 / 4 | 194 / 216 |
| GEA_608-ROWEstimate_20170502.pdf | AdobePDFLibrary | no | 0 / 1 | 553 / 571 |
| 5007_20-08-Billing.pdf | Excel | no | 1 / 9 | 2,916 / 3,414 |
| status-mobility-fy2019.pdf | PrintToPDF | no | 0 / 2 | 66 / 101 |
| status-mobility-fy2018.pdf | Distiller | no | 0 / 2 | 60 / 105 |
| 85531_1010_Index.pdf | Ghostscript | no | 4 / 10 | 593 / 1,218 |
| PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf | Ghostscript | no | 0 / 1 | 117 / 127 |

## Cost

53 distinct rasters at $0.015 a page, $0.795 at list price. One call per page; the degraded rasters are shared with no other lane. Every re-read and re-score since has been served from the retained cache and cost nothing.

| Document | Pages | List charge |
|---|---:|---:|
| 19132-71-VE-June-2023.pdf | 13 | $0.195 |
| 12.2025-Kenco-SOV.pdf | 6 | $0.090 |
| REVISED-McClean-December-SOV.pdf | 5 | $0.075 |
| Billing-Review-Comments-Disposition-Est-95.pdf | 4 | $0.060 |
| GEA_608-ROWEstimate_20170502.pdf | 1 | $0.015 |
| 5007_20-08-Billing.pdf | 9 | $0.135 |
| status-mobility-fy2019.pdf | 2 | $0.030 |
| status-mobility-fy2018.pdf | 2 | $0.030 |
| 85531_1010_Index.pdf | 10 | $0.150 |
| PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf | 1 | $0.015 |

## Evidence

Recorded from `src/corridor_pdf_reader/receipts/textract/textract-C` (role `imported-textract-lane`), recomputed from its per-pair `scores/*.json` and checked against its own `summary.json`. The responses are the 116-entry experiment cache retained in the standalone worktree, read-only; four of its entries are committed as fixtures and replayed in CI (src/corridor_pdf_reader/receipts/textract/fixture-replay.json).

## What this is not

This is a measurement of the declared unconfirmed-reading role, not a qualification of it and not a selection of it. Textract has not earned verified scanned-cell transcription: 695 of the 901 mismatched cells in the clean lane carried a mean word confidence of 95 or more (ADR-0094). Native promotion never implies that OCR reached native-cell accuracy.

## Limits

Paired renditions are the measurement method, not a prerequisite for reading a production PDF. Cell comparison does not independently establish prose outside tables, raster fidelity, routing correctness, source-locator replay, malicious-document handling, production concurrency, or operational cost and coordinator burden. Those belong to their own tickets.
