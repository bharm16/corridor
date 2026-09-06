# Loop score: textract-A-margin2

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 0 / 10 |
| Pages passing | 16 / 53 |
| Reference cells exact | 14025 / 14641 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| uncovered | 587 |  | 10 |
| value_mismatch | 346 | 26 | 6 |
| outside_table | 202 | 17 | 8 |
| unexplained_text | 140 | 17 | 9 |
| span_mismatch | 27 | 16 | 8 |
| extra_value | 27 | 8 | 2 |
| unaligned_row | 4 | 2 | 1 |
| merged_cells | 4 | 4 | 1 |
| prose_row | 3 | 2 | 2 |
| missing_cell | 3 | 1 | 1 |
| missing_row | 2 | 1 | 1 |
| merged_rows | 2 | 1 | 1 |

## Subclasses

| Subclass | Cells |
|---|---:|
| uncovered/value_mismatch | 313 |
| outside_table/cell_text_outside | 202 |
| uncovered/outside_table | 176 |
| value_mismatch/format_only | 144 |
| uncovered/absent | 98 |
| value_mismatch/different | 93 |
| value_mismatch/reader_has_more | 79 |
| unexplained_text/numeric | 70 |
| unexplained_text/text | 49 |
| span_mismatch/wider | 27 |
| extra_value/not_in_workbook | 24 |
| unexplained_text/edge | 21 |
| value_mismatch/overflow_hashes | 12 |
| value_mismatch/clipped_edge | 10 |
| value_mismatch/clipped_evidence | 5 |
| unaligned_row/not_in_workbook | 4 |
| merged_cells/text_intact | 4 |
| prose_row/outside_workbook | 3 |
| missing_cell/absent | 3 |
| extra_value/wrong_column | 3 |
| missing_row/absent | 2 |
| merged_rows/text_intact | 2 |
| value_mismatch/reader_has_less | 1 |
| value_mismatch/clipped_suffix | 1 |
| value_mismatch/clipped_middle | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| AdobePDFLibrary | 0/1 | 0/1 | 556/571 |
| Distiller | 0/1 | 0/2 | 68/105 |
| Excel | 0/4 | 7/33 | 11811/12303 |
| Ghostscript | 0/2 | 9/11 | 1308/1345 |
| PrintToPDF | 0/2 | 0/6 | 282/317 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| 5e979146e9322953 | 5007_20-08-Billing.pdf | Excel | 9 | 239 | 209 |
| 075ad4da3647d7dc | 12.2025-Kenco-SOV.pdf | Excel | 6 | 129 | 114 |
| 02753bf7e6549f5a | 19132-71-VE-June-2023.pdf | Excel | 13 | 109 | 102 |
| 1ee4a00b8eb6173b | REVISED-McClean-December-SOV.pdf | Excel | 5 | 136 | 44 |
| 9d701a8ad492a6d7 | status-mobility-fy2018.pdf | Distiller | 2 | 34 | 37 |
| b62895f03208009f | 85531_1010_Index.pdf | Ghostscript | 10 | 40 | 24 |
| 99e3871a86ca9971 | status-mobility-fy2019.pdf | PrintToPDF | 2 | 28 | 33 |
| 5d36b973fa9393a8 | GEA_608-ROWEstimate_20170502.pdf | AdobePDFLibrary | 1 | 18 | 15 |
| fcbe0ceb23e61b0f | PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2 | Ghostscript | 1 | 21 | 8 |
| 50b23088b99df7c2 | Billing-Review-Comments-Disposition-Est-95.pdf | PrintToPDF | 4 | 6 | 1 |

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

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 118, "c": 14, "i": 25, "j": 13, "ref": " $(14,400.08)", "got": "$ (14,400.08"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 337, "c": 5, "i": 33, "j": 4, "ref": "WSF CO 239", "got": "WSF C"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 66, "c": 10, "i": 10, "j": 9, "ref": " $(1,477.00)", "got": "$ (1,477.00"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 169, "c": 14, "i": 4, "j": 13, "ref": " $(9,571.61)", "got": "$ (9,571.61"}
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 3, "table": 0, "sheet": "Billing Issues", "r": 25, "c": 6, "i": 9, "j": 5, "ref": "Excel row 415, CO 430, Remove Pedestrian Bridge – Steelkorr bridge removal, item", "got": "Excel row 415, CO 430, Remove Pe

### value_mismatch/format_only

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 118, "c": 15, "i": 25, "j": 14, "ref": "100%", "got": ") 100%"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 270, "c": 9, "i": 1, "j": 8, "ref": " $159,260.00 ", "got": "$ 159260.00"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 270, "c": 10, "i": 1, "j": 9, "ref": " $159,260.00 ", "got": "$ 159260.00"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 270, "c": 14, "i": 1, "j": 13, "ref": " $159,260.00 ", "got": "$ 159260.00"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 271, "c": 9, "i": 2, "j": 8, "ref": " $4,807.00 ", "got": ",\n$ 4,807.00"}

### value_mismatch/reader_has_more

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 303, "c": 6, "i": 34, "j": 5, "ref": "5", "got": "5 i"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 11, "table": 0, "sheet": "SOV", "r": 348, "c": 5, "i": 8, "j": 4, "ref": "WSF CO 246", "got": "WSF CO 246 i"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 384, "c": 8, "i": 11, "j": 7, "ref": "53", "got": "r\n53"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 58, "c": 7, "i": 2, "j": 6, "ref": "bond", "got": ",,,,\nbond"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 74, "c": 1, "i": 18, "j": 0, "ref": "00.074215", "got": ".\n00.074215"}

### value_mismatch/different

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 303, "c": 7, "i": 34, "j": 6, "ref": "Replace 480V three phase inverter as shown on drawings with a 277V single phase ", "got": "Replace 480V three phase inverter as shown on drawings with a 
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 11, "table": 0, "sheet": "SOV", "r": 348, "c": 7, "i": 8, "j": 6, "ref": "Provide and install new W23 fixture for sign 3.A06b.220 per WSF SL 437 and as ou", "got": "Provide and install new W23 fixture for sign 3.A06b.220 per WS
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 384, "c": 7, "i": 11, "j": 6, "ref": "Supply and install Electrical Package complete, including all electrical equipme", "got": "Supply and install Electrical Package complete, including all
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 8, "c": 7, "i": 3, "j": 6, "ref": "ITEM #15 - PASSENGER ONLY FERRY GLAZING                                       $1", "got": "ITEM #15 - PASSENGER ONLY FERRY GLAZING $1,233,87000"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 22, "c": 7, "i": 17, "j": 6, "ref": "ITEM #15 - OHL GLAZING                                                          ", "got": "ITEM #15 - OHL GLAZING $305,05600"}

### value_mismatch/reader_has_less

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 337, "c": 5, "i": 33, "j": 5, "ref": "WSF CO 239", "got": "O 239"}

### value_mismatch/clipped_suffix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 19, "j": 6, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined verison) and as directed by 

### value_mismatch/clipped_middle

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 6, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to com

### span_mismatch/wider

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 398, "c": 7, "i": 25, "j": 6, "ref": "Install Electrical Utility Racks at Entry Building per RFI 1653 and as outlined ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 14, "c": 7, "i": 9, "j": 6, "ref": "ITEM #15 - TERMINAL BUILDING GLAZING                                            ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 69, "c": 7, "i": 13, "j": 6, "ref": "BID ITEM #12 - METAL PANELS TOTAL                                               ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201", "expected": [1], "got_columns": [1, 2, 3, 4, 5, 6, 7]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 16, "i": 0, "j": 15, "ref": "12/31/25", "expected": [16], "got_columns": [16, 17]}

### uncovered/outside_table

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 1, "ref": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 9, "ref": "Bid Package No.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 15, "ref": "Date:", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 16, "ref": "06/20/23", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 3, "c": 1, "ref": "Contractor: Hoffman-Pacific LLC, A Joint Venture • H-P Job No. 5290015", "sub": "outside_table"}

### uncovered/value_mismatch

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 118, "c": 15, "ref": "100%", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 270, "c": 9, "ref": " $159,260.00 ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 270, "c": 10, "ref": " $159,260.00 ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 270, "c": 14, "ref": " $159,260.00 ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 271, "c": 9, "ref": " $4,807.00 ", "sub": "value_mismatch"}

### uncovered/absent

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 19, "c": 3, "ref": " $-   ", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 21, "c": 6, "ref": " $38,556,711.08 ", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 22, "c": 6, "ref": " $119,223.00 ", "sub": "absent"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "sheet": "SOV", "r": 1, "c": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "sub": "absent"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "sheet": "SOV", "r": 34, "c": 7, "ref": "ITEM #15 - GLAZING TOTAL                                                        ", "sub": "absent"}

### unexplained_text/edge

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "BEFORE FINAL PRINTING enter any digit to remove the green shading in the data en"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "¬"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "got": "ove the green shading in the data entry areas on the forms."}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "got": "Sb MCl I Wk 2102 R A E WA 98201"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "got": "ove the green shading in the data entry areas on the forms."}

### extra_value/not_in_workbook

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 9, "c": 8, "i": 4, "j": 7, "got": "."}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 23, "c": 8, "i": 18, "j": 7, "got": "."}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 29, "c": 8, "i": 24, "j": 7, "got": "."}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 37, "c": 8, "i": 32, "j": 7, "got": "."}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 43, "c": 8, "i": 38, "j": 7, "got": "."}

### unaligned_row/not_in_workbook

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 29, "j": 6, "got": "ITEM #15 - GLAZING TOTAL $6,650,63900"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 30, "j": 7, "got": "."}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 31, "j": 6, "got": "ITEM #11 - PASSENGER ONLY FERRY METAL PANELS $838,66100"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 6, "j": 0, "got": "."}

### missing_row/absent

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 34, "c": 7, "i": null, "j": null, "ref": "ITEM #15 - GLAZING TOTAL                                                        "}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 36, "c": 7, "i": null, "j": null, "ref": "ITEM #11 - PASSENGER ONLY FERRY METAL PANELS                              $838,6"}

### prose_row/outside_workbook

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 1, "j": 6, "got": "ITEM #11 - PED VIEWSTAIRSELEV METAL PANELS $102,240.00"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 0, "sheet": "DGN Files", "r": null, "c": null, "i": 0, "j": 0, "got": "asemapesgnes"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 0, "sheet": "DGN Files", "r": null, "c": null, "i": 20, "j": 1, "got": "____"}

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

### merged_rows/text_intact

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 2, "c": 1, "i": 0, "j": 0, "ref": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.", "got": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 3, "c": 14, "i": 1, "j": 13, "ref": "39", "got": "39\n07/12/20", "rows": [3, 4]}

### missing_cell/absent

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 3, "c": 12, "i": 1, "j": null, "ref": "Progress Billing No."}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 10, "i": 2, "j": null, "ref": "5290015-39648"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 12, "i": 2, "j": null, "ref": "Billing Period  "}

### extra_value/wrong_column

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 209, "c": 6, "i": 15, "j": 5, "got": "1"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 218, "c": 6, "i": 24, "j": 5, "got": "1"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 222, "c": 6, "i": 28, "j": 5, "got": "1"}

### value_mismatch/clipped_evidence

- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 112, "c": 4, "i": 20, "j": 2, "ref": "MAINLINE PROFILE - I.R. 90 WB; STA. 169+00 TO STA. 174+00; I.R. 90 WB TEMP STA. ", "got": "MAINLINE PROFILE - I.R. 90 WB; STA. 169+00 TO STA. 174+00; I.
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 113, "c": 4, "i": 21, "j": 2, "ref": "MAINLINE PROFILE - EX I.R. 90; STA. 58+00 TO STA. 63+00; I.R. 90 WB STA. 174+00 ", "got": "MAINLINE PROFILE - EX I.R. 90; STA. 58+00 TO STA. 63+00; I.R.
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 149, "c": 4, "i": 57, "j": 2, "ref": "PLAN AND PROFILE; ORANGE AVE. STA. 32+00 TO STA. 35+00; ONTARIO ST. STA. 25+00 T", "got": "PLAN AND PROFILE; ORANGE AVE. STA. 32+00 TO STA. 35+00; ONTAR
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 3, "table": 0, "sheet": "DGN Files", "r": 270, "c": 4, "i": 87, "j": 2, "ref": "PARKING LOT PLAN; CONSTRUCTION DETAILS; CUYAHOGA COMMUNITY COLLEGE PARKING", "got": "PARKING LOT PLAN; CONSTRUCTION DETAILS; CUYAHOGA COMMUNITY COLLEGE 
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 5, "table": 0, "sheet": "DGN Files", "r": 386, "c": 4, "i": 21, "j": 2, "ref": "STRUCTURE GENERAL NOTES - 1; BRIDGE NO. CUY-90-1524; EXISTING RAMP E1 OVER GCRTA", "got": "STRUCTURE GENERAL NOTES - 1; BRIDGE NO. CUY-90-1524; EXISTING

