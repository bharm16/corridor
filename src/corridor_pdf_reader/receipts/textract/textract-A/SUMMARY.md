# Loop score: textract-A

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 0 / 10 |
| Pages passing | 25 / 53 |
| Reference cells exact | 14342 / 14641 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| uncovered | 279 |  | 9 |
| outside_table | 209 | 17 | 8 |
| unexplained_text | 151 | 20 | 9 |
| span_mismatch | 35 | 16 | 9 |
| value_mismatch | 30 | 9 | 4 |
| unaligned_row | 5 | 1 | 1 |
| prose_row | 3 | 1 | 1 |
| missing_cell | 3 | 1 | 1 |
| merged_cells | 2 | 2 | 1 |
| merged_rows | 2 | 1 | 1 |
| extra_value | 1 | 1 | 1 |

## Subclasses

| Subclass | Cells |
|---|---:|
| outside_table/cell_text_outside | 209 |
| uncovered/outside_table | 179 |
| uncovered/absent | 92 |
| unexplained_text/numeric | 70 |
| unexplained_text/text | 48 |
| span_mismatch/wider | 35 |
| unexplained_text/edge | 33 |
| value_mismatch/overflow_hashes | 12 |
| uncovered/value_mismatch | 8 |
| value_mismatch/different | 7 |
| value_mismatch/clipped_evidence | 5 |
| unaligned_row/not_in_workbook | 4 |
| prose_row/outside_workbook | 3 |
| missing_cell/absent | 3 |
| merged_cells/text_intact | 2 |
| merged_rows/text_intact | 2 |
| value_mismatch/clipped_edge | 1 |
| value_mismatch/reader_has_less | 1 |
| value_mismatch/clipped_suffix | 1 |
| value_mismatch/clipped_middle | 1 |
| value_mismatch/format_only | 1 |
| unaligned_row/misplaced | 1 |
| value_mismatch/reader_has_more | 1 |
| extra_value/not_in_workbook | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| AdobePDFLibrary | 0/1 | 0/1 | 556/571 |
| Distiller | 0/1 | 0/2 | 68/105 |
| Excel | 0/4 | 17/33 | 12123/12303 |
| Ghostscript | 0/2 | 8/11 | 1311/1345 |
| PrintToPDF | 0/2 | 0/6 | 284/317 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| 1ee4a00b8eb6173b | REVISED-McClean-December-SOV.pdf | Excel | 5 | 152 | 45 |
| 075ad4da3647d7dc | 12.2025-Kenco-SOV.pdf | Excel | 6 | 52 | 43 |
| 02753bf7e6549f5a | 19132-71-VE-June-2023.pdf | Excel | 13 | 49 | 42 |
| 5e979146e9322953 | 5007_20-08-Billing.pdf | Excel | 9 | 46 | 35 |
| 9d701a8ad492a6d7 | status-mobility-fy2018.pdf | Distiller | 2 | 34 | 37 |
| 99e3871a86ca9971 | status-mobility-fy2019.pdf | PrintToPDF | 2 | 28 | 33 |
| b62895f03208009f | 85531_1010_Index.pdf | Ghostscript | 10 | 37 | 21 |
| 5d36b973fa9393a8 | GEA_608-ROWEstimate_20170502.pdf | AdobePDFLibrary | 1 | 18 | 15 |
| fcbe0ceb23e61b0f | PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2 | Ghostscript | 1 | 21 | 8 |
| 50b23088b99df7c2 | Billing-Review-Comments-Disposition-Est-95.pdf | PrintToPDF | 4 | 4 | 0 |

## Examples

### outside_table/cell_text_outside

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "B I L L I N G S U M M A R Y"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Contractor: Hoffman-Pacific LLC, A Joint Venture • H-P Job No. 5290015"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Sub: Valley Electric 1100 Merrill Creek Parkway Everett WA 98203"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Bid Package No."}

### unexplained_text/numeric

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "-"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "38,556,711.08"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}

### value_mismatch/clipped_edge

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 337, "c": 5, "i": 33, "j": 4, "ref": "WSF CO 239", "got": "WSF C"}

### value_mismatch/reader_has_less

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 337, "c": 5, "i": 33, "j": 5, "ref": "WSF CO 239", "got": "O 239"}

### value_mismatch/clipped_suffix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 19, "j": 6, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined verison) and as directed by 

### value_mismatch/clipped_middle

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 6, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to com

### span_mismatch/wider

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 398, "c": 7, "i": 25, "j": 6, "ref": "Install Electrical Utility Racks at Entry Building per RFI 1653 and as outlined ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 8, "c": 7, "i": 3, "j": 6, "ref": "ITEM #15 - PASSENGER ONLY FERRY GLAZING                                       $1", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 14, "c": 7, "i": 9, "j": 6, "ref": "ITEM #15 - TERMINAL BUILDING GLAZING                                            ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 22, "c": 7, "i": 17, "j": 6, "ref": "ITEM #15 - OHL GLAZING                                                          ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 28, "c": 7, "i": 23, "j": 6, "ref": "ITEM #15- ELEVATOR GLAZING                                                      ", "expected": [7], "got_columns": [7, 8]}

### value_mismatch/format_only

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": 420, "c": 12, "i": 17, "j": 11, "ref": "$119,223.00 ", "got": "$119223.00"}

### uncovered/outside_table

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 1, "ref": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 9, "ref": "Bid Package No.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 15, "ref": "Date:", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 16, "ref": "06/20/23", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 3, "c": 1, "ref": "Contractor: Hoffman-Pacific LLC, A Joint Venture • H-P Job No. 5290015", "sub": "outside_table"}

### uncovered/value_mismatch

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 420, "c": 12, "ref": "$119,223.00 ", "sub": "value_mismatch"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "sheet": "SOV", "r": 164, "c": 12, "ref": " $31,667.65 ", "sub": "value_mismatch"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "sheet": "SOV", "r": 2, "c": 10, "ref": "1", "sub": "value_mismatch"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "sheet": "SOV", "r": 194, "c": 1, "ref": "00.220000", "sub": "value_mismatch"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "sheet": "SOV", "r": 194, "c": 9, "ref": " $2,851.64 ", "sub": "value_mismatch"}

### uncovered/absent

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 19, "c": 3, "ref": " $-   ", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 21, "c": 6, "ref": " $38,556,711.08 ", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 22, "c": 6, "ref": " $119,223.00 ", "sub": "absent"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "sheet": "SOV", "r": 1, "c": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "sub": "absent"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "sheet": "Actng Only", "r": 19, "c": 3, "ref": " $-   ", "sub": "absent"}

### unexplained_text/edge

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "BEFORE FINAL PRINTING enter any digit to remove the green shading in the data en"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "¬"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "got": "ove the green shading in the data entry areas on the forms."}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "got": "S N"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "got": "Billi Pi"}

### unaligned_row/not_in_workbook

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 8, "got": "ubcontracto. 55-"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 11, "got": "ngerod"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 14, "got": "tru"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 15, "got": "35"}

### unaligned_row/misplaced

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 12, "got": "5"}

### merged_cells/text_intact

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}

### prose_row/outside_workbook

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 8, "got": "uconraco.-"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 11, "got": "ngero"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 14, "got": "ru"}

### unexplained_text/text

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "got": ",."}
- {"pair": "5d36b973fa9393a8", "pdf": "GEA_608-ROWEstimate_20170502.pdf", "page": 1, "got": "*Acquisition Service Cost Includes the following:"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "Source: USAS, cash basis. Includes revenue less expenses from previous fiscal ye"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "fund progress payments on highway projects and HB 122, passed by the 84th legisl"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "money bonds effective January 1, 2015."}

### value_mismatch/different

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201", "got": "u:ceanronorsossvevere"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 164, "c": 12, "i": 35, "j": 11, "ref": " $31,667.65 ", "got": "$ 3166765"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 194, "c": 1, "i": 39, "j": 0, "ref": "00.220000", "got": "00220000"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 194, "c": 9, "i": 39, "j": 8, "ref": " $2,851.64 ", "got": "$ 285164"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 194, "c": 10, "i": 39, "j": 9, "ref": " $2,851.64 ", "got": "$ 285164"}

### value_mismatch/overflow_hashes

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 132, "c": 8, "i": 3, "j": 7, "ref": " $9.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 133, "c": 8, "i": 4, "j": 7, "ref": " $10.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 134, "c": 8, "i": 5, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 135, "c": 8, "i": 6, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 136, "c": 8, "i": 7, "j": 7, "ref": " $11.00 ", "got": "#"}

### merged_rows/text_intact

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 2, "c": 1, "i": 0, "j": 0, "ref": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.", "got": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 3, "c": 14, "i": 1, "j": 13, "ref": "39", "got": "39\n07/12/20", "rows": [3, 4]}

### value_mismatch/reader_has_more

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 2, "c": 10, "i": 0, "j": 9, "ref": "1", "got": "1\n5290015-39648"}

### extra_value/not_in_workbook

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 2, "c": 12, "i": 0, "j": 11, "got": "Progress Billing No.\nBilling Period"}

### missing_cell/absent

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 3, "c": 12, "i": 1, "j": null, "ref": "Progress Billing No."}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 10, "i": 2, "j": null, "ref": "5290015-39648"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 12, "i": 2, "j": null, "ref": "Billing Period  "}

### value_mismatch/clipped_evidence

- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 112, "c": 4, "i": 20, "j": 2, "ref": "MAINLINE PROFILE - I.R. 90 WB; STA. 169+00 TO STA. 174+00; I.R. 90 WB TEMP STA. ", "got": "MAINLINE PROFILE - I.R. 90 WB; STA. 169+00 TO STA. 174+00; I.
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 113, "c": 4, "i": 21, "j": 2, "ref": "MAINLINE PROFILE - EX I.R. 90; STA. 58+00 TO STA. 63+00; I.R. 90 WB STA. 174+00 ", "got": "MAINLINE PROFILE - EX I.R. 90; STA. 58+00 TO STA. 63+00; I.R.
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 149, "c": 4, "i": 57, "j": 2, "ref": "PLAN AND PROFILE; ORANGE AVE. STA. 32+00 TO STA. 35+00; ONTARIO ST. STA. 25+00 T", "got": "PLAN AND PROFILE; ORANGE AVE. STA. 32+00 TO STA. 35+00; ONTAR
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 3, "table": 0, "sheet": "DGN Files", "r": 270, "c": 4, "i": 87, "j": 2, "ref": "PARKING LOT PLAN; CONSTRUCTION DETAILS; CUYAHOGA COMMUNITY COLLEGE PARKING", "got": "PARKING LOT PLAN; CONSTRUCTION DETAILS; CUYAHOGA COMMUNITY COLLEGE 
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 5, "table": 0, "sheet": "DGN Files", "r": 386, "c": 4, "i": 21, "j": 2, "ref": "STRUCTURE GENERAL NOTES - 1; BRIDGE NO. CUY-90-1524; EXISTING RAMP E1 OVER GCRTA", "got": "STRUCTURE GENERAL NOTES - 1; BRIDGE NO. CUY-90-1524; EXISTING

