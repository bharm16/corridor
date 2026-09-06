# Paired-rendition measurement: frozen-reader-2026-09-06

Configuration `frozen-reader`: the imported reader as loop-020 measured it: PDFium glyphs joined to Excel's structure tree through pypdf, the deterministic reconstructor on what is left, engine `tagged` at 36 dpi. Engine `tagged`, 36 dpi, 4 jobs; reader commit `c39363e`, package digest `7fd94c5fcaaa5531`; pypdfium2 5.13.0 (PDFium 153.0.7999.0), pypdf 6.17.0.

Reference Dataset `2026-09-06.1`, registration `46d5a66491d1ac04`, corpus manifest `ca68b55abfa583a1`, split `529c5ffb00bc96f5`; 263 development and 70 holdout pairs selected; answer keys 333 of 333 byte-identical to loop-reference-v6.

| Set | Pairs | Pages | Cells exact | Failing pairs |
|---|---:|---:|---:|---|
| development | 262 / 263 | 1589 / 1589 | 483,210 / 483,336 | fcbe0ceb23e61b0f |
| holdout | 70 / 70 | 409 / 409 | 125,498 / 125,529 | none |

## Gate

- development: 1 pair(s) fail, 0 page(s) fail
- holdout: every pair passes, 0 page(s) fail

## Against the registered baseline

- no registered baseline to compare with: this run is the baseline once retained

## Holdout

- included; actor `codex/731 lane for bharm16`, reason: register the baseline of the #731 harness for the frozen reader: a reproducibility and regression check of loop-020 and the 2026-09-06 reproduction, not a new generalization claim; nothing tuned before or after (ADR-0008); the access is appended to `gold/pdf-pairs/v1/holdout-access.jsonl`

## Limits

Paired renditions are the measurement method, not a prerequisite for reading a production PDF. Cell comparison does not independently establish prose outside tables, raster fidelity, routing correctness, source-locator replay, malicious-document handling, production concurrency, or operational cost and coordinator burden. Those belong to their own tickets.
