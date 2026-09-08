# WSDOT 9540 reference validity for the native challenger

Decision: **reused for the labeled historical retained-reference comparison**.
No new enumeration or fresh score was justified or produced for #738. The
reference remains method `pymupdf-table-grid`, version `1`, and remains spent.

The unchanged files are bound by these SHA-256 values:

| File | SHA-256 |
|---|---|
| `wsdot-9540.machine.csv` | `97323bebdedcf3bd6db2d43be3c55cdaa0e662b9125b5835a52962e0e18b5bd9` |
| `wsdot-9540.machine.md` | `0dda2b0170e86f3e7f463ab6da0774223ea096de307c0a3db7ce97ccf214dcf6` |
| `wsdot-9540.machine.scope.json` | `f6652482186d3e772b248979707720440168d583ee2a928b1a6bc6c2e9a43c7b` |

Its unchanged scope names six source PDFs. The CSV contains 192 entries:
120 critical, 56 noncritical and 16 blank labels. There are 190 distinct
source IDs: `COFI-W-1008` and `COMI-W-1001` each occur twice. Those repetitions
retain their multiplicities; no deduplication is applied. The two COFI entries
have different criticality labels (blank and yes), so a matched ID alone
cannot identify which physical occurrence was captured.

#737's [retained native replay](native-matrix/v1/receipts/2026-09-08-selection.json)
matches the 192 reference entries while separately checking its 201 body rows
(192 extracted, nine missing required fields). Per-field values, dispositions
and reasons come from that separate retained-reading comparison. The CSV's
three columns do not provide independent field/disposition gold or scoped
physical-occurrence verification. Its critical-subset measure concerns finding
reference IDs, not proving an extracted criticality determination.

The old PyMuPDF limitation and honest backfill provenance remain attached to
the original scope. A different extractor does not make the old enumeration
complete or independently true. No old files were regenerated, relabeled or
rescored. The original spent measurement remains protected; project renaming,
new filenames and new method selection cannot bypass the source-hash guard.
