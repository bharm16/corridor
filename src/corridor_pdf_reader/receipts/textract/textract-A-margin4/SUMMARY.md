# Loop score: textract-A-margin4

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 0 / 10 |
| Pages passing | 11 / 53 |
| Reference cells exact | 8554 / 14641 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| uncovered | 5846 |  | 10 |
| value_mismatch | 5506 | 39 | 9 |
| missing_row | 1509 | 8 | 4 |
| extra_value | 1080 | 30 | 7 |
| merged_rows | 204 | 17 | 5 |
| outside_table | 192 | 16 | 8 |
| unexplained_text | 143 | 18 | 9 |
| missing_value | 132 | 10 | 3 |
| unaligned_row | 97 | 15 | 6 |
| prose_row | 35 | 7 | 4 |
| span_mismatch | 25 | 13 | 5 |
| merged_cells | 4 | 4 | 1 |
| missing_cell | 3 | 1 | 1 |
| columns_folded | 2 | 2 | 2 |

## Subclasses

| Subclass | Cells |
|---|---:|
| uncovered/value_mismatch | 5025 |
| value_mismatch/format_only | 2669 |
| missing_row/absent | 1275 |
| value_mismatch/reader_has_more | 1087 |
| extra_value/not_in_workbook | 1006 |
| value_mismatch/different | 825 |
| value_mismatch/reader_has_less | 668 |
| uncovered/absent | 636 |
| missing_row/in_table_elsewhere | 234 |
| value_mismatch/clipped_edge | 214 |
| merged_rows/text_intact | 204 |
| outside_table/cell_text_outside | 192 |
| uncovered/outside_table | 185 |
| unaligned_row/not_in_workbook | 93 |
| missing_value/in_table_elsewhere | 72 |
| unexplained_text/numeric | 71 |
| extra_value/misplaced | 60 |
| missing_value/absent | 60 |
| unexplained_text/text | 48 |
| prose_row/outside_workbook | 35 |
| span_mismatch/wider | 25 |
| unexplained_text/edge | 24 |
| extra_value/wrong_row | 13 |
| value_mismatch/overflow_hashes | 12 |
| value_mismatch/rounded_to_width | 11 |
| value_mismatch/clipped_suffix | 9 |
| value_mismatch/clipped_prefix | 6 |
| value_mismatch/clipped_evidence | 5 |
| unaligned_row/misplaced | 4 |
| merged_cells/text_intact | 4 |
| columns_folded/unmapped_column | 2 |
| missing_cell/absent | 2 |
| missing_cell/in_table_elsewhere | 1 |
| extra_value/wrong_column | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| AdobePDFLibrary | 0/1 | 0/1 | 551/571 |
| Distiller | 0/1 | 0/2 | 68/105 |
| Excel | 0/4 | 2/33 | 6577/12303 |
| Ghostscript | 0/2 | 9/11 | 1165/1345 |
| PrintToPDF | 0/2 | 0/6 | 193/317 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| 5e979146e9322953 | 5007_20-08-Billing.pdf | Excel | 9 | 3046 | 2037 |
| 02753bf7e6549f5a | 19132-71-VE-June-2023.pdf | Excel | 13 | 2354 | 1917 |
| 075ad4da3647d7dc | 12.2025-Kenco-SOV.pdf | Excel | 6 | 1696 | 1323 |
| b62895f03208009f | 85531_1010_Index.pdf | Ghostscript | 10 | 1189 | 165 |
| 1ee4a00b8eb6173b | REVISED-McClean-December-SOV.pdf | Excel | 5 | 432 | 213 |
| 50b23088b99df7c2 | Billing-Review-Comments-Disposition-Est-95.pdf | PrintToPDF | 4 | 105 | 91 |
| 9d701a8ad492a6d7 | status-mobility-fy2018.pdf | Distiller | 2 | 35 | 37 |
| 99e3871a86ca9971 | status-mobility-fy2019.pdf | PrintToPDF | 2 | 28 | 33 |
| 5d36b973fa9393a8 | GEA_608-ROWEstimate_20170502.pdf | AdobePDFLibrary | 1 | 24 | 20 |
| fcbe0ceb23e61b0f | PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2 | Ghostscript | 1 | 23 | 10 |

## Examples

### outside_table/cell_text_outside

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "B I L L I N G S U M M A R Y"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Contractor: Hoffman-Pacific LLC, A Joint Venture • H-P Job No. 5290015"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Sub: Valley Electric 1100 Merrill Creek Parkway Everett WA 98203"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Bid Package No."}

### unexplained_text/numeric

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "00000000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "-"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "38,556,711.08"}

### prose_row/outside_workbook

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": null, "c": null, "i": 0, "j": 0, "got": ".\nMod Numbers t"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": null, "c": null, "i": 0, "j": 1, "got": "Sum of Completed Work Billed\nhis Period"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 17, "j": 6, "got": "et Amount of Invoice"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 17, "j": 10, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 17, "j": 11, "got": "119,223.00"}

### unexplained_text/edge

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "got": "Subcontract No"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "got": "Billin Period"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "BEFORE FINAL PRINTING enter any digit to remove the green shading in the data en"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "¬"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "Sub: Kenco Construction 19874 141st Pl NE Woodinville WA 98072"}

### value_mismatch/different

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 5, "c": 1, "i": 0, "j": 0, "ref": "Job Area # \nCost Code", "got": "Job Area #\nCt Cd"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 5, "c": 2, "i": 0, "j": 1, "ref": "Mod", "got": "Md"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 5, "c": 4, "i": 0, "j": 3, "ref": "CE Item #", "got": "CE\nIt #"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 5, "c": 5, "i": 0, "j": 4, "ref": "Ref Doc", "got": "Rf D"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 5, "c": 6, "i": 0, "j": 5, "ref": "Sub Ref", "got": "Sb Rf"}

### value_mismatch/clipped_edge

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 5, "c": 13, "i": 0, "j": 12, "ref": "% billed this Per.", "got": "%\nbilled\nthis\nP"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 5, "c": 13, "i": 0, "j": 12, "ref": "% billed this Per.", "got": "%\nbilled\nthis\nP"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 57, "c": 10, "i": 8, "j": 9, "ref": " $(10,746.53)", "got": "$ (10,746.53"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 57, "c": 14, "i": 8, "j": 13, "ref": " $(10,746.53)", "got": "$ (10,746.53"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 58, "c": 10, "i": 9, "j": 9, "ref": " $(78,432.59)", "got": "$ (78,432.59"}

### extra_value/not_in_workbook

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 1, "i": 1, "j": 0, "got": "osoe"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 2, "i": 1, "j": 1, "got": "o"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 4, "i": 1, "j": 3, "got": "em"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 5, "i": 1, "j": 4, "got": "eoc"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 6, "i": 1, "j": 5, "got": "ue\nB"}

### value_mismatch/reader_has_more

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 10, "i": 1, "j": 9, "ref": " $-   ", "got": "revouse\n$ -"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 14, "i": 1, "j": 13, "ref": " $-   ", "got": "ae\n$ -"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 16, "i": 1, "j": 15, "ref": " $-   ", "got": "ns\n$ -"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 7, "c": 6, "i": 2, "j": 5, "ref": "1", "got": "1 M"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 8, "c": 6, "i": 3, "j": 5, "ref": "1", "got": "1 L"}

### value_mismatch/format_only

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 7, "c": 1, "i": 2, "j": 0, "ref": "00.260000", "got": "00.26000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 8, "c": 1, "i": 3, "j": 0, "ref": "00.260000", "got": "00.26000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 9, "c": 1, "i": 4, "j": 0, "ref": "00.260000", "got": "00.26000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 10, "c": 1, "i": 5, "j": 0, "ref": "00.260000", "got": "00.26000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 11, "c": 1, "i": 6, "j": 0, "ref": "00.260000", "got": "00.26000"}

### merged_rows/text_intact

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 7, "c": 2, "i": 2, "j": 1, "ref": "0", "got": "0 0", "rows": [7, 8]}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 8, "c": 2, "i": 3, "j": 1, "ref": "0", "got": "0 0", "rows": [8, 9]}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 9, "c": 2, "i": 4, "j": 1, "ref": "0", "got": "0 0", "rows": [9, 10]}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 10, "c": 2, "i": 5, "j": 1, "ref": "0", "got": "0 0", "rows": [10, 11]}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 11, "c": 2, "i": 6, "j": 1, "ref": "0", "got": "0 0", "rows": [10, 11]}

### value_mismatch/reader_has_less

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 7, "c": 7, "i": 2, "j": 6, "ref": "Mobilization", "got": "obilization"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 8, "c": 7, "i": 3, "j": 6, "ref": "Layout and Modeling", "got": "ayout and Modeling"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 9, "c": 7, "i": 4, "j": 6, "ref": "Temp Cabling", "got": "emp Cabling"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 10, "c": 7, "i": 5, "j": 6, "ref": "Temp Equipment", "got": "emp Equipment"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 11, "c": 7, "i": 6, "j": 6, "ref": "Demolition", "got": "emolition"}

### extra_value/wrong_row

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 20, "c": 6, "i": 15, "j": 5, "got": "1"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 38, "c": 6, "i": 33, "j": 5, "got": "1"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 55, "c": 6, "i": 6, "j": 5, "got": "1"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 79, "c": 6, "i": 30, "j": 5, "got": "1"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 99, "c": 6, "i": 6, "j": 5, "got": "1"}

### extra_value/misplaced

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 21, "c": 4, "i": 16, "j": 3, "got": "9"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 22, "c": 4, "i": 17, "j": 3, "got": "9"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 23, "c": 4, "i": 18, "j": 3, "got": "9"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 24, "c": 4, "i": 19, "j": 3, "got": "9"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 25, "c": 4, "i": 20, "j": 3, "got": "9"}

### value_mismatch/clipped_prefix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 5, "c": 12, "i": 0, "j": 11, "ref": "Completed Work Billed this Period", "got": "Completed Work"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 5, "c": 13, "i": 0, "j": 12, "ref": "% billed this Per.", "got": "%\nbilled\nthis"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 5, "c": 14, "i": 0, "j": 13, "ref": "Total Completed to Date", "got": "Total Completed to"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 5, "c": 15, "i": 0, "j": 14, "ref": "% compl to date", "got": "%\ncompl"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 5, "c": 16, "i": 0, "j": 15, "ref": "Balance To Finish", "got": "Balance To"}

### missing_value/absent

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 1, "i": 7, "j": 0, "ref": "00.260000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 2, "i": 7, "j": 1, "ref": "30"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 3, "i": 7, "j": 2, "ref": "441"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 5, "i": 7, "j": 4, "ref": "CO 185"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 9, "i": 7, "j": 8, "ref": " $119,924.70 "}

### missing_value/in_table_elsewhere

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 4, "i": 7, "j": 3, "ref": "2"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 11, "i": 7, "j": 10, "ref": "100%"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 15, "i": 7, "j": 14, "ref": "100%"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 311, "c": 16, "i": 7, "j": 15, "ref": " $-   "}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 384, "c": 4, "i": 11, "j": 3, "ref": "2"}

### unaligned_row/not_in_workbook

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 15, "j": 0, "got": "C\nN"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 15, "j": 12, "got": "."}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 0, "got": "Job Area #"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 6, "got": ".,"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 8, "got": "ucoaco."}

### uncovered/outside_table

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 1, "ref": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 9, "ref": "Bid Package No.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 15, "ref": "Date:", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 16, "ref": "06/20/23", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 3, "c": 1, "ref": "Contractor: Hoffman-Pacific LLC, A Joint Venture • H-P Job No. 5290015", "sub": "outside_table"}

### uncovered/value_mismatch

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 6, "c": 7, "ref": "BID ITEM #01 - Valley Electric - Mini MACC ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 6, "c": 10, "ref": " $-   ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 6, "c": 14, "ref": " $-   ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 6, "c": 16, "ref": " $-   ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 7, "c": 1, "ref": "00.260000", "sub": "value_mismatch"}

### uncovered/absent

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 311, "c": 1, "ref": "00.260000", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 311, "c": 2, "ref": "30", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 311, "c": 11, "ref": "100%", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 311, "c": 15, "ref": "100%", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 311, "c": 16, "ref": " $-   ", "sub": "absent"}

### unaligned_row/misplaced

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 3, "got": "CE"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 23, "j": 11, "got": "51,801.19"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 38, "j": 3, "got": "1\n1"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 13, "j": 3, "got": "1\n1"}

### value_mismatch/clipped_suffix

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 5, "c": 1, "i": 1, "j": 0, "ref": "Job Area # \nCost Code", "got": "Cost Code"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 5, "c": 4, "i": 1, "j": 3, "ref": "CE Item #", "got": "Item #"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 5, "c": 10, "i": 1, "j": 9, "ref": "Work Complete Previous Billed", "got": "Previous Billed"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 5, "c": 11, "i": 1, "j": 10, "ref": "% prev. billed", "got": "billed"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 5, "c": 12, "i": 1, "j": 11, "ref": "Completed Work Billed this Period", "got": "this Period"}

### missing_row/absent

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 8, "c": 7, "i": null, "j": null, "ref": "ITEM #15 - PASSENGER ONLY FERRY GLAZING                                       $1"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 34, "c": 7, "i": null, "j": null, "ref": "ITEM #15 - GLAZING TOTAL                                                        "}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 36, "c": 7, "i": null, "j": null, "ref": "ITEM #11 - PASSENGER ONLY FERRY METAL PANELS                              $838,6"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 105, "c": 7, "i": null, "j": null, "ref": "Elevated Pedestrian Connector / Ticketing"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 121, "c": 7, "i": null, "j": null, "ref": "Item 10 - South Trestle           $10,418.00"}

### columns_folded/unmapped_column

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 97, "c": 15, "i": 6, "j": 14, "ref": "100%", "got": "100%"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 261, "c": 16, "i": 11, "j": 9, "ref": " $-   ", "got": "$ -"}

### span_mismatch/wider

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201", "expected": [1], "got_columns": [1, 2, 3, 4, 5, 6, 7]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201", "expected": [1], "got_columns": [1, 2, 3, 4, 5, 6, 7]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201", "expected": [1], "got_columns": [1, 2, 3, 4, 5, 6, 7]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 16, "i": 0, "j": 15, "ref": "12/31/25", "expected": [16], "got_columns": [16, 17]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201", "expected": [1], "got_columns": [1, 2, 3, 4, 5, 6, 7]}

### merged_cells/text_intact

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}

### value_mismatch/overflow_hashes

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 132, "c": 8, "i": 3, "j": 7, "ref": " $9.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 133, "c": 8, "i": 4, "j": 7, "ref": " $10.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 134, "c": 8, "i": 5, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 135, "c": 8, "i": 6, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 136, "c": 8, "i": 7, "j": 7, "ref": " $11.00 ", "got": "#"}

### unexplained_text/text

- {"pair": "5d36b973fa9393a8", "pdf": "GEA_608-ROWEstimate_20170502.pdf", "page": 1, "got": "*Acquisition Service Cost Includes the following:"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "Source: USAS, cash basis. Includes revenue less expenses from previous fiscal ye"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "fund progress payments on highway projects and HB 122, passed by the 84th legisl"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "money bonds effective January 1, 2015."}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "There are no remaining Texas Mobility Fund bond proceeds for fiscal year 2019."}

### missing_cell/absent

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 3, "c": 12, "i": 1, "j": null, "ref": "Progress Billing No."}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 12, "i": 2, "j": null, "ref": "Billing Period  "}

### missing_cell/in_table_elsewhere

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 10, "i": 2, "j": null, "ref": "5290015-39648"}

### value_mismatch/rounded_to_width

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 18, "c": 14, "i": 16, "j": 13, "ref": " $1,029,377.12 ", "got": "1,029,377.1"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 21, "c": 14, "i": 19, "j": 13, "ref": " $191,091.72 ", "got": "191,091.7"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 24, "c": 14, "i": 22, "j": 13, "ref": " $6,253.11 ", "got": "6,253.1"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 26, "c": 14, "i": 24, "j": 13, "ref": " $725,595.11 ", "got": "725,595.1"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 38, "c": 14, "i": 36, "j": 13, "ref": " $866,389.24 ", "got": "866,389.2"}

### missing_row/in_table_elsewhere

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 79, "c": 14, "i": null, "j": null, "ref": " $-   "}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 79, "c": 16, "i": null, "j": null, "ref": " $-   "}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 95, "c": 2, "i": null, "j": null, "ref": "10"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 95, "c": 3, "i": null, "j": null, "ref": "278"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 95, "c": 4, "i": null, "j": null, "ref": "1"}

### extra_value/wrong_column

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 79, "c": 17, "i": 2, "j": 16, "got": "$ -"}

### value_mismatch/clipped_evidence

- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 112, "c": 4, "i": 20, "j": 2, "ref": "MAINLINE PROFILE - I.R. 90 WB; STA. 169+00 TO STA. 174+00; I.R. 90 WB TEMP STA. ", "got": "MAINLINE PROFILE - I.R. 90 WB; STA. 169+00 TO STA. 174+00; I.
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 113, "c": 4, "i": 21, "j": 2, "ref": "MAINLINE PROFILE - EX I.R. 90; STA. 58+00 TO STA. 63+00; I.R. 90 WB STA. 174+00 ", "got": "MAINLINE PROFILE - EX I.R. 90; STA. 58+00 TO STA. 63+00; I.R.
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 149, "c": 4, "i": 57, "j": 2, "ref": "PLAN AND PROFILE; ORANGE AVE. STA. 32+00 TO STA. 35+00; ONTARIO ST. STA. 25+00 T", "got": "PLAN AND PROFILE; ORANGE AVE. STA. 32+00 TO STA. 35+00; ONTAR
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 3, "table": 0, "sheet": "DGN Files", "r": 270, "c": 4, "i": 87, "j": 2, "ref": "PARKING LOT PLAN; CONSTRUCTION DETAILS; CUYAHOGA COMMUNITY COLLEGE PARKING", "got": "PARKING LOT PLAN; CONSTRUCTION DETAILS; CUYAHOGA COMMUNITY COLLEGE 
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 5, "table": 0, "sheet": "DGN Files", "r": 386, "c": 4, "i": 21, "j": 2, "ref": "STRUCTURE GENERAL NOTES - 1; BRIDGE NO. CUY-90-1524; EXISTING RAMP E1 OVER GCRTA", "got": "STRUCTURE GENERAL NOTES - 1; BRIDGE NO. CUY-90-1524; EXISTING

