# #736: versioned native Source Segment integration

Implementation measured: `46c6e25c093dd7923cb62018597a247a9f64a2a5`.
The frozen reader code and its original loop-020 artifacts were unchanged.
Both new runs verified all 333 reference keys byte for byte and recorded
separate spent-holdout accesses in `gold/pdf-pairs/v1/holdout-access.jsonl`.

The actual `read_native_pdf -> native_segment_values` handoff reproduced the
fresh frozen-reader run: all 333 scorer page payloads (excluding receipt
annotations) and all 333 complete score objects are identical. See the
[comparison proof](736-native-handoff-comparison.json),
[frozen receipt](../../gold/pdf-pairs/v1/receipts/736-frozen-reader-46c6e25/RECEIPT.md),
and [typed handoff receipt](../../gold/pdf-pairs/v1/receipts/736-native-segments-v1-46c6e25/RECEIPT.md).
The registered baseline comparison reports no regression.

| Both configurations | Pairs passing | Pages passing | Exact reference cells |
|---|---:|---:|---:|
| Development | 262 / 263 | 1,589 / 1,589 | 483,210 / 483,336 |
| Spent holdout | 70 / 70 | 409 / 409 | 125,498 / 125,529 |

This reproduces the frozen baseline, including its pre-existing failure
`fcbe0ceb23e61b0f`; it is not an all-pairs-green or all-cells-exact result.
The baseline's error classes and exclusions remain in the unchanged scorer.

The typed handoff generated and materialized all **621,990 nonempty reader
cells**, with zero missing, ambiguous, invalid, unused or omitted cell
records. These are reader cell counts, distinct from the 608,865 reference
cells in the table. It generated 136,307 page spans and 203 clipped spans.
All 6,738,920 visible and 4,871 clipped source glyphs occur in those source
spans. The 1,657 glyphs outside reader cells remain separately citable in
page spans. Clipped spacing is the recorded reader projection; unavailable
whitespace is not reconstructed as a verified source value.

This full dataset run measures the in-memory typed handoff, not database
persistence for all 621,990 cells. Real PostgreSQL integration tests separately
prove the SECURITY DEFINER append path, unchanged historical rows, reading
idempotency, configuration changes, ID selection, foreign document/page/table
refusals, literal-construction refusals, and replay from original bytes.
Outside/clipped scorer evidence comes from reader metadata; generated spans
and their full glyph-coverage counts are retained separately.

Measured local wall time was **158.4 seconds** for the frozen harness and
**476.1 seconds** for the production-isolated native handoff driver, each with
four workers. Those are observed local execution costs, not deployment
latency or a customer-savings claim. No model or AWS call was made.

Commands used `make pdf-pairs-measure` with `--configuration frozen-reader`
and `--configuration native-segments-v1`, respectively, distinct new output
directories, `--include-holdout --holdout-actor codex:issue-736`,
`--holdout-reason '#736 disabled source-segment integration reproduces frozen loop-020 and measures actual typed segment handoff'`,
and `--retain measurement`. The native run reused the frozen run's verified
reference keys. Full logs, source reads and comparison evidence are retained
outside the checkout under
`/Users/bryceharmon/Desktop/corridor-evidence/2026-09-08-program727/`.

Local validation at the measured implementation:

- `make check`: 40 architecture tests passed; Ruff, mypy and compile checks passed.
- Focused reader/segment/ingest/measurement/audit seams: 132 passed, two missing-corpus tests initially skipped. Both were then rerun against the shared read-only corpus and passed: **134 distinct executed tests**.
- `make test-migrations`: 11 passed, including the populated supported-predecessor transition, exact historical prose preservation, and atomic refusal to downgrade away a native reading.
- Native source geometry tests inspect actual pixels through asymmetric boxes, non-zero crop origins, all four rotations, selected detail crops, and actual non-zero deskew. The declared grayscale deskew proof profile preserves text pixels after deskew, while the ordinary morphology profile intentionally removes text.
- Independent core and measurement reviews found no remaining actionable findings.

The [clarified historical audit](736-prose-text-location-clarification.md)
remains separate: 159,531 corpus-generated spans, 152,882 exact strings present,
and all physical locations unknown. It is not a persisted-citation audit and
emits no cross-reader replacement locator. The original #733 receipt bytes
remain unchanged.

Production defaults remain disabled. Matrix semantic mapping remains #737;
native Minutes/statement mapping and source-class qualification remain #447;
retained engine removal and customer-citation preservation remain #741.
The native-only Minutes handoff refuses explicitly until its mapping exists.
