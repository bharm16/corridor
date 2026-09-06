# Loop score: loop

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 0 / 10 |
| Pages passing | 23 / 53 |
| Reference cells exact | 12844 / 14641 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| uncovered | 1783 |  | 9 |
| outside_table | 1022 | 23 | 8 |
| unexplained_text | 370 | 20 | 9 |
| missing_row | 100 | 4 | 4 |
| span_mismatch | 15 | 7 | 3 |
| value_mismatch | 14 | 2 | 2 |
| missing_cell | 9 | 3 | 3 |

## Subclasses

| Subclass | Cells |
|---|---:|
| outside_table/cell_text_outside | 1022 |
| uncovered/outside_table | 1016 |
| uncovered/absent | 767 |
| unexplained_text/text | 217 |
| unexplained_text/numeric | 112 |
| missing_row/outside_table | 75 |
| unexplained_text/edge | 41 |
| missing_row/absent | 25 |
| span_mismatch/wider | 15 |
| value_mismatch/overflow_hashes | 12 |
| missing_cell/outside_table | 7 |
| missing_cell/absent | 2 |
| value_mismatch/clipped_suffix | 1 |
| value_mismatch/clipped_middle | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| AdobePDFLibrary | 0/1 | 0/1 | 540/571 |
| Distiller | 0/1 | 0/2 | 0/105 |
| Excel | 0/4 | 18/33 | 11965/12303 |
| Ghostscript | 0/2 | 5/11 | 123/1345 |
| PrintToPDF | 0/2 | 0/6 | 216/317 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| b62895f03208009f | 85531_1010_Index.pdf | Ghostscript | 10 | 818 | 1214 |
| 1ee4a00b8eb6173b | REVISED-McClean-December-SOV.pdf | Excel | 5 | 204 | 72 |
| 02753bf7e6549f5a | 19132-71-VE-June-2023.pdf | Excel | 13 | 95 | 98 |
| 9d701a8ad492a6d7 | status-mobility-fy2018.pdf | Distiller | 2 | 74 | 105 |
| 99e3871a86ca9971 | status-mobility-fy2019.pdf | PrintToPDF | 2 | 69 | 101 |
| 075ad4da3647d7dc | 12.2025-Kenco-SOV.pdf | Excel | 6 | 101 | 68 |
| 5e979146e9322953 | 5007_20-08-Billing.pdf | Excel | 9 | 83 | 86 |
| 5d36b973fa9393a8 | GEA_608-ROWEstimate_20170502.pdf | AdobePDFLibrary | 1 | 61 | 31 |
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

### value_mismatch/clipped_suffix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 19, "j": 6, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined verison) and as directed by 

### value_mismatch/clipped_middle

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 6, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to com

### span_mismatch/wider

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 398, "c": 7, "i": 25, "j": 6, "ref": "Install Electrical Utility Racks at Entry Building per RFI 1653 and as outlined ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 8, "c": 7, "i": 4, "j": 6, "ref": "ITEM #15 - PASSENGER ONLY FERRY GLAZING                                       $1", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 14, "c": 7, "i": 10, "j": 6, "ref": "ITEM #15 - TERMINAL BUILDING GLAZING                                            ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 22, "c": 7, "i": 18, "j": 6, "ref": "ITEM #15 - OHL GLAZING                                                          ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 28, "c": 7, "i": 24, "j": 6, "ref": "ITEM #15- ELEVATOR GLAZING                                                      ", "expected": [7], "got_columns": [7, 8]}

### uncovered/outside_table

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 1, "ref": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 9, "ref": "Bid Package No.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 15, "ref": "Date:", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 16, "ref": "06/20/23", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 3, "c": 1, "ref": "Contractor: Hoffman-Pacific LLC, A Joint Venture • H-P Job No. 5290015", "sub": "outside_table"}

### uncovered/absent

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 19, "c": 3, "ref": " $-   ", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 21, "c": 6, "ref": " $38,556,711.08 ", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "Actng Only", "r": 22, "c": 6, "ref": " $119,223.00 ", "sub": "absent"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "sheet": "SOV", "r": 1, "c": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "sub": "absent"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "sheet": "Actng Only", "r": 19, "c": 3, "ref": " $-   ", "sub": "absent"}

### missing_cell/outside_table

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 24, "c": 2, "i": 6, "j": null, "ref": "5"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 25, "c": 2, "i": 7, "j": null, "ref": "6"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 26, "c": 2, "i": 8, "j": null, "ref": "7"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 27, "c": 2, "i": 9, "j": null, "ref": "9"}
- {"pair": "5d36b973fa9393a8", "pdf": "GEA_608-ROWEstimate_20170502.pdf", "page": 1, "table": 0, "sheet": "RW", "r": 47, "c": 6, "i": 42, "j": null, "ref": "$2,372,400"}

### missing_cell/absent

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 25, "c": 3, "i": 7, "j": null, "ref": " $9,398.95 "}
- {"pair": "5d36b973fa9393a8", "pdf": "GEA_608-ROWEstimate_20170502.pdf", "page": 1, "table": 0, "sheet": "RW", "r": 56, "c": 5, "i": 44, "j": null, "ref": "*Acquisition Service Cost Includes the following: \n(per ODOT Cost Estimating Pro"}

### missing_row/outside_table

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 21, "c": 2, "i": null, "j": null, "ref": "00.074215"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 22, "c": 2, "i": null, "j": null, "ref": "2"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 23, "c": 2, "i": null, "j": null, "ref": "3"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 28, "c": 2, "i": null, "j": null, "ref": "10"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 61, "c": 2, "i": null, "j": null, "ref": "44"}

### missing_row/absent

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "table": 0, "sheet": "Actng Only", "r": 63, "c": 3, "i": null, "j": null, "ref": " $6,769.36 "}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 5, "table": 0, "sheet": "Actng Only", "r": 23, "c": 3, "i": null, "j": null, "ref": " $-   "}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 5, "table": 0, "sheet": "Actng Only", "r": 25, "c": 3, "i": null, "j": null, "ref": " $1,250.00 "}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 5, "table": 0, "sheet": "Actng Only", "r": 27, "c": 3, "i": null, "j": null, "ref": " $-   "}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 5, "table": 0, "sheet": "Actng Only", "r": 45, "c": 3, "i": null, "j": null, "ref": " $-   "}

### unexplained_text/edge

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "BEFORE FINAL PRINTING enter any digit to remove the green shading in the data en"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "got": "¬"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "got": "ove the green shading in the data entry areas on the forms."}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "got": "ove the green shading in the data entry areas on the forms."}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "got": "ove the green shading in the data entry areas on the forms."}

### value_mismatch/overflow_hashes

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 132, "c": 8, "i": 2, "j": 7, "ref": " $9.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 133, "c": 8, "i": 3, "j": 7, "ref": " $10.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 134, "c": 8, "i": 4, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 135, "c": 8, "i": 5, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 136, "c": 8, "i": 6, "j": 7, "ref": " $11.00 ", "got": "#"}

### unexplained_text/text

- {"pair": "5d36b973fa9393a8", "pdf": "GEA_608-ROWEstimate_20170502.pdf", "page": 1, "got": "*Acquisition Service Cost Includes the following:"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "Certificate of Title Fees 147,127,455.47"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "Driver Record Info Fees 69,758,639.89"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "Driver's License Fees 149,707,805.67"}
- {"pair": "99e3871a86ca9971", "pdf": "status-mobility-fy2019.pdf", "page": 1, "got": "Vehicle Inspection Fees 95,155,107.25"}

