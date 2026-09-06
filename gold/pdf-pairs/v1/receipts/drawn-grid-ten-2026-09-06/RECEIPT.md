# Paired-rendition measurement: drawn-grid-ten-2026-09-06

Configuration `drawn-grid`: the reader's drawn-grid engine (`pdfium`) without Excel's structure tree: not the measured configuration and not a candidate; it reads less than the frozen reader so the gate is proven to fail. Engine `pdfium`, 36 dpi, 4 jobs; reader commit `c39363e`, package digest `7fd94c5fcaaa5531`; pypdfium2 5.13.0 (PDFium 153.0.7999.0), pypdf 6.17.0.

Reference Dataset `2026-09-06.1`, registration `46d5a66491d1ac04`, corpus manifest `ca68b55abfa583a1`, split `529c5ffb00bc96f5`; 10 development and 0 holdout pairs selected (a subset); answer keys 10 of 10 byte-identical to loop-reference-v6.

| Set | Pairs | Pages | Cells exact | Failing pairs |
|---|---:|---:|---:|---|
| development | 0 / 10 | 23 / 53 | 12,844 / 14,641 | 02753bf7e6549f5a, 075ad4da3647d7dc, 1ee4a00b8eb6173b, 50b23088b99df7c2, 5d36b973fa9393a8, 5e979146e9322953, 99e3871a86ca9971, 9d701a8ad492a6d7, b62895f03208009f, fcbe0ceb23e61b0f |
| holdout | not scored | | | |

## Gate

- development: 10 pair(s) fail, 30 page(s) fail

## Against the registered baseline

- development: 10 pairs compared with `frozen-reader-2026-09-06`; 9 regressed (02753bf7e6549f5a, 075ad4da3647d7dc, 1ee4a00b8eb6173b, 50b23088b99df7c2, 5d36b973fa9393a8, 5e979146e9322953, 99e3871a86ca9971, 9d701a8ad492a6d7, b62895f03208009f), 0 improved; pages passing 53 -> 23; cells exact 14,615 -> 12,844
- regression against the baseline: **yes**

## Holdout

- not included; the holdout was neither read nor scored

## Limits

Paired renditions are the measurement method, not a prerequisite for reading a production PDF. Cell comparison does not independently establish prose outside tables, raster fidelity, routing correctness, source-locator replay, malicious-document handling, production concurrency, or operational cost and coordinator burden. Those belong to their own tickets.
