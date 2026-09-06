# Loop score: reader-ten

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 9 / 10 |
| Pages passing | 53 / 53 |
| Reference cells exact | 14615 / 14641 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| value_mismatch | 23 | 8 | 3 |
| prose_row | 15 | 1 | 1 |
| uncovered | 6 |  | 1 |
| unverifiable | 1 | 1 | 1 |

## Subclasses

| Subclass | Cells |
|---|---:|
| prose_row/outside_workbook | 15 |
| value_mismatch/overflow_hashes | 12 |
| value_mismatch/clipped_evidence | 9 |
| uncovered/absent | 6 |
| value_mismatch/clipped_suffix | 1 |
| value_mismatch/clipped_middle | 1 |
| unverifiable/print_area_overflow | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| AdobePDFLibrary | 1/1 | 1/1 | 571/571 |
| Distiller | 1/1 | 2/2 | 105/105 |
| Excel | 4/4 | 33/33 | 12288/12303 |
| Ghostscript | 1/2 | 11/11 | 1334/1345 |
| PrintToPDF | 2/2 | 6/6 | 317/317 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| fcbe0ceb23e61b0f | PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2 | Ghostscript | 1 | 15 | 6 |
| 1ee4a00b8eb6173b | REVISED-McClean-December-SOV.pdf | Excel | 5 | 16 | 0 |
| b62895f03208009f | 85531_1010_Index.pdf | Ghostscript | 10 | 5 | 0 |
| 02753bf7e6549f5a | 19132-71-VE-June-2023.pdf | Excel | 13 | 2 | 0 |
| 99e3871a86ca9971 | status-mobility-fy2019.pdf | PrintToPDF | 2 | 1 | 0 |
| 075ad4da3647d7dc | 12.2025-Kenco-SOV.pdf | Excel | 6 | 0 | 0 |
| 50b23088b99df7c2 | Billing-Review-Comments-Disposition-Est-95.pdf | PrintToPDF | 4 | 0 | 0 |
| 5d36b973fa9393a8 | GEA_608-ROWEstimate_20170502.pdf | AdobePDFLibrary | 1 | 0 | 0 |
| 5e979146e9322953 | 5007_20-08-Billing.pdf | Excel | 9 | 0 | 0 |
| 9d701a8ad492a6d7 | status-mobility-fy2018.pdf | Distiller | 2 | 0 | 0 |

## Examples

### value_mismatch/clipped_suffix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 19, "j": 7, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined verison) and as directed by 

### value_mismatch/clipped_middle

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 7, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to com

### value_mismatch/clipped_evidence

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 1, "c": 2, "i": 0, "j": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "got": "ove the green shading in the data entry areas on the forms
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 1, "c": 2, "i": 0, "j": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "got": "ove the green shading in the data entry areas on the forms
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 1, "c": 2, "i": 0, "j": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "got": "ove the green shading in the data entry areas on the forms
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 1, "c": 2, "i": 0, "j": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "got": "ove the green shading in the data entry areas on the forms
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 112, "c": 4, "i": 20, "j": 2, "ref": "MAINLINE PROFILE - I.R. 90 WB; STA. 169+00 TO STA. 174+00; I.R. 90 WB TEMP STA. ", "got": "MAINLINE PROFILE - I.R. 90 WB; STA. 169+00 TO STA. 174+00; I.

### value_mismatch/overflow_hashes

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 132, "c": 8, "i": 6, "j": 8, "ref": " $9.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 133, "c": 8, "i": 7, "j": 8, "ref": " $10.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 134, "c": 8, "i": 8, "j": 8, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 135, "c": 8, "i": 9, "j": 8, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 136, "c": 8, "i": 10, "j": 8, "ref": " $11.00 ", "got": "#"}

### unverifiable/print_area_overflow

- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 2, "table": 0, "sheet": "FY2018 Revenue vs Appropriation", "r": null, "c": null, "i": 26, "j": 0, "got": "2The appropriations are for the entire fiscal year 2019"}

### prose_row/outside_workbook

- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "page": 1, "table": 0, "sheet": "3-4-20 SOV", "r": null, "c": null, "i": 0, "j": 2, "got": "CO 224: Phase 3 Marine Recovery - Billing Schedule of Values 3-4-2020 Sidebar Me"}
- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "page": 1, "table": 0, "sheet": "3-4-20 SOV", "r": null, "c": null, "i": 1, "j": 2, "got": "This Change Order amends the Contract to incorporate a negotiated cost increase "}
- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "page": 1, "table": 0, "sheet": "3-4-20 SOV", "r": null, "c": null, "i": 1, "j": 10, "got": "Amended -"}
- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "page": 1, "table": 0, "sheet": "3-4-20 SOV", "r": null, "c": null, "i": 2, "j": 2, "got": "provide full and final resolution f"}
- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "page": 1, "table": 0, "sheet": "3-4-20 SOV", "r": null, "c": null, "i": 2, "j": 3, "got": "or any and all cost and fe and"}

### uncovered/absent

- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "sheet": "3-4-20 SOV", "r": 1, "c": 2, "ref": "CO 224: Phase 3 Marine Recovery - Billing Schedule of Values 3-4-2020 Sidebar Me", "sub": "absent"}
- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "sheet": "3-4-20 SOV", "r": 2, "c": 2, "ref": "Description:\nThis Change Order amends the Contract to incorporate a negotiated c", "sub": "absent"}
- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "sheet": "3-4-20 SOV", "r": 2, "c": 3, "ref": "Amended Cost Allocation", "sub": "absent"}
- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "sheet": "3-4-20 SOV", "r": 2, "c": 4, "ref": "Amended Budget Allocation", "sub": "absent"}
- {"pair": "fcbe0ceb23e61b0f", "pdf": "PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2020.pdf", "sheet": "3-4-20 SOV", "r": 2, "c": 8, "ref": "Progress Estimate 38 Prior Payment", "sub": "absent"}

