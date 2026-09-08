# #447 bounded render-outlier reconstruction

Only #735's 35 named outlier comparisons were reconstructed from 28 local, digest-matched original PDFs. All 70 new rasters and 70 complete returned manifests are retained beside receipt.json. Render implementation blobs, all engine/library versions and the frozen profile bundle match #735; execution used UV_OFFLINE=1 and UV_NO_SYNC=1. These are new observations, not claimed historical PNGs.

All 35 reproduce every original recorded metric exactly. All 35 still exceed the original tolerances. No threshold was widened. The historical full run remains 541/576 passing comparisons, page 1 only of 192/195 PDFs; three oversized PDFs remain excluded.

Outlier coverage: 10 review, 10 OCR-layout and 15 table-CV comparisons across 28 documents. Eight exceed only MAD, 25 only one-pixel ink coverage, and two both. The exhaustive 35-row inventory and the excluded source hashes are in the sibling retained-evidence receipt and outliers.csv.

Evidence now supports:

- The new manifests confirm the two distinct applied table-CV deskew angles reported for City of Houston Signal Agreement (fb748d4a) and METRO Transitway (d0f1a75d). Full-chain propagation of the reconstructed PDF-user CropBox corners differs by up to 3.6394 px for fb748d4a and 1.8197 px for d0f1a75d. Exact angles, matrices and inverses are in inspection-v2.json. They establish differences in applied geometry, not particular text loss or cell-boundary errors.
- Original affine step-name equality did not mean matrix equality: 11/576 historical rows had coefficient differences (nine source-origin rows and two deskew rows). The e619a4ab source-origin difference is 49.8417 px at 300 dpi for identical PDF-user coordinates. The bounded rerun includes four of these rows; remaining origin rows were not outliers and were not rerendered. Legacy manifests remain unchanged.
- Representative new pairs were viewed for the dense 329ab37f matrix, faint-text/linework 0c0cde2d cover sheet and fb748d4a table-CV agreement. PDFium's matrix text/rules and cover-sheet hairlines are visibly heavier/darker. No gross layout displacement is visible in the two review examples at the displayed scale. The table-CV pair retains differing sparse line fragments, not readable body text. This is limited visual evidence, not field-level verification or a harmlessness verdict.
- The old receipt's 5.5% unmatched-ink example names the wrong reference set: 0c0cde2d has 5.498756% PDFium ink without nearby MuPDF ink, versus 0.002478% in the reverse direction. Neither ratio independently establishes which rendering is correct.
- The surviving METRO legacy table-CV PNG is byte-identical to its new reconstruction. Other original primary raster hashes were never retained; exact scalar replay is not a substitute for those missing historical byte identities.

Residual qualification requires source-anchored regional checks for glyph visibility, table/rule topology and edge clipping through actual deskew; separately measured downstream native/model/OCR behavior; and explicit handling of later pages and the three excluded plans. None of this preflight selects a production configuration or claims all 35 outliers harmless.

The prior holdout governance concern is retained separately. These 35 outliers contain no gold/pdf/v1 holdout document; no source access was broadened and no reference or ledger was edited.
