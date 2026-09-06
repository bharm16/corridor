# Loop score: textract-B

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 0 / 10 |
| Pages passing | 7 / 53 |
| Reference cells exact | 13450 / 14641 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| uncovered | 1171 |  | 10 |
| value_mismatch | 923 | 38 | 9 |
| unexplained_text | 161 | 22 | 9 |
| outside_table | 142 | 18 | 9 |
| span_mismatch | 23 | 14 | 8 |
| missing_cell | 4 | 1 | 1 |
| missing_value | 3 | 2 | 1 |
| merged_cells | 3 | 3 | 1 |
| prose_row | 2 | 2 | 2 |
| extra_value | 1 | 1 | 1 |
| merged_rows | 1 | 1 | 1 |

## Subclasses

| Subclass | Cells |
|---|---:|
| uncovered/value_mismatch | 898 |
| value_mismatch/different | 726 |
| outside_table/cell_text_outside | 142 |
| uncovered/outside_table | 139 |
| value_mismatch/format_only | 137 |
| uncovered/absent | 134 |
| unexplained_text/edge | 77 |
| unexplained_text/text | 55 |
| value_mismatch/unicode_variant | 34 |
| unexplained_text/numeric | 29 |
| span_mismatch/wider | 23 |
| value_mismatch/overflow_hashes | 12 |
| value_mismatch/clipped_prefix | 4 |
| value_mismatch/reader_has_more | 4 |
| missing_cell/absent | 4 |
| missing_value/outside_table | 3 |
| value_mismatch/clipped_edge | 3 |
| value_mismatch/clipped_suffix | 2 |
| prose_row/outside_workbook | 2 |
| merged_cells/text_intact | 2 |
| value_mismatch/clipped_middle | 1 |
| merged_cells/text_differs | 1 |
| extra_value/not_in_workbook | 1 |
| merged_rows/text_intact | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| AdobePDFLibrary | 0/1 | 0/1 | 556/571 |
| Distiller | 0/1 | 0/2 | 67/105 |
| Excel | 0/4 | 3/33 | 11816/12303 |
| Ghostscript | 0/2 | 4/11 | 749/1345 |
| PrintToPDF | 0/2 | 0/6 | 262/317 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| b62895f03208009f | 85531_1010_Index.pdf | Ghostscript | 10 | 588 | 584 |
| 5e979146e9322953 | 5007_20-08-Billing.pdf | Excel | 9 | 223 | 211 |
| 1ee4a00b8eb6173b | REVISED-McClean-December-SOV.pdf | Excel | 5 | 147 | 73 |
| 075ad4da3647d7dc | 12.2025-Kenco-SOV.pdf | Excel | 6 | 98 | 102 |
| 02753bf7e6549f5a | 19132-71-VE-June-2023.pdf | Excel | 13 | 87 | 83 |
| 9d701a8ad492a6d7 | status-mobility-fy2018.pdf | Distiller | 2 | 33 | 38 |
| 99e3871a86ca9971 | status-mobility-fy2019.pdf | PrintToPDF | 2 | 28 | 35 |
| 50b23088b99df7c2 | Billing-Review-Comments-Disposition-Est-95.pdf | PrintToPDF | 4 | 27 | 20 |
| 5d36b973fa9393a8 | GEA_608-ROWEstimate_20170502.pdf | AdobePDFLibrary | 1 | 16 | 15 |
| fcbe0ceb23e61b0f | PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2 | Ghostscript | 1 | 16 | 10 |

## Examples

### unexplained_text/edge

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "HOFFMAN"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "PACIFIC"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "LLC"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "A JOINT VENTURE"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "got": "Project: Seattle Multimodal Terminal at Colman Dock Project Seattle, WA."}

### outside_table/cell_text_outside

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "BILLING SUMMARY"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Sub: Valley Electric 1100 Merrill Creek Parkway Everett WA 98203"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Progress Billing No."}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "70"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Billing Period"}

### unexplained_text/text

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Project: Seattle Multimodal Terminal at Colman Dock Project Seattle, WA"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Contractor: Hoffman-Pacific LLC, A Joint Venture H-P Job No. 5290015"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Bid Package No. 0"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Invoice No. 70"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Date: 06/20/23"}

### unexplained_text/numeric

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "-"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "119,223.00"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "got": "$"}

### value_mismatch/different

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 7, "i": 1, "j": 6, "ref": "BID ITEM #01 - Valley Electric - Mini MACC ", "got": "BID ITEM #01 Valley Electric - Mini MACC"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 16, "c": 7, "i": 11, "j": 6, "ref": "BID ITEM #04 - South Trestle - Under Pier - $4,632,180", "got": "BID ITEM #04 South Trestle Under Pier - $4,632,180"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 27, "c": 7, "i": 22, "j": 6, "ref": "BID ITEM #04 - North Trestle - Under Pier - $2,614,766", "got": "BID ITEM #04 North Trestle Under Pier - $2,614,766"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 45, "c": 7, "i": 40, "j": 6, "ref": "BID ITEM #05 - South Trestle - Above Pier - $5,842,046", "got": "BID ITEM #05 South Trestle Above Pier $5,842,046"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 67, "c": 7, "i": 18, "j": 6, "ref": "Bid Item #05 - At Grade Walkway - $54,256", "got": "Bid Item #05 At Grade Walkway - $54,256"}

### value_mismatch/format_only

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 287, "c": 14, "i": 18, "j": 13, "ref": " $-   ", "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 288, "c": 10, "i": 19, "j": 9, "ref": " $-   ", "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 288, "c": 14, "i": 19, "j": 13, "ref": " $-   ", "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 303, "c": 16, "i": 34, "j": 15, "ref": " $-   ", "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 304, "c": 16, "i": 35, "j": 15, "ref": " $-   ", "got": "$"}

### value_mismatch/clipped_prefix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 337, "c": 5, "i": 33, "j": 4, "ref": "WSF CO 239", "got": "WSF CO"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 4, "c": 12, "i": 0, "j": 11, "ref": "Billing Period  ", "got": "Billing"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 2, "table": 0, "sheet": "DGN Files", "r": 149, "c": 4, "i": 57, "j": 2, "ref": "PLAN AND PROFILE; ORANGE AVE. STA. 32+00 TO STA. 35+00; ONTARIO ST. STA. 25+00 T", "got": "PLAN AND PROFILE; ORANGE AVE. STA. 32+00 TO STA. 35+00; ONTAR
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 5, "table": 0, "sheet": "DGN Files", "r": 386, "c": 4, "i": 21, "j": 2, "ref": "STRUCTURE GENERAL NOTES - 1; BRIDGE NO. CUY-90-1524; EXISTING RAMP E1 OVER GCRTA", "got": "STRUCTURE GENERAL NOTES - 1; BRIDGE NO. CUY-90-1524; EXISTING

### value_mismatch/clipped_suffix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 337, "c": 5, "i": 33, "j": 5, "ref": "WSF CO 239", "got": "239"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 19, "j": 6, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined verison) and as directed by 

### value_mismatch/clipped_middle

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 6, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to com

### span_mismatch/wider

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 398, "c": 7, "i": 25, "j": 6, "ref": "Install Electrical Utility Racks at Entry Building per RFI 1653 and as outlined ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 28, "c": 7, "i": 23, "j": 6, "ref": "ITEM #15- ELEVATOR GLAZING                                                      ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201", "expected": [1], "got_columns": [1, 2, 3, 4, 5, 6, 7]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 16, "i": 0, "j": 15, "ref": "12/31/25", "expected": [16], "got_columns": [16, 17]}
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 1, "table": 0, "sheet": "Billing Issues", "r": 1, "c": 1, "i": 0, "j": 0, "ref": "Seattle Multimodal Terminal at Colman Dock", "expected": [1], "got_columns": [1, 2]}

### uncovered/absent

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 1, "ref": "Project: Seattle Multimodal Terminal at Colman Dock Project • Seattle, WA.", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 3, "c": 1, "ref": "Contractor: Hoffman-Pacific LLC, A Joint Venture • H-P Job No. 5290015", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 3, "c": 9, "ref": "Invoice No.", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 4, "c": 9, "ref": "Subcontract No.", "sub": "absent"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 4, "c": 10, "ref": "5290015-39632", "sub": "absent"}

### uncovered/outside_table

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 9, "ref": "Bid Package No.", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 15, "ref": "Date:", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 2, "c": 16, "ref": "06/20/23", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 3, "c": 10, "ref": "70", "sub": "outside_table"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 3, "c": 12, "ref": "Progress Billing No.", "sub": "outside_table"}

### uncovered/value_mismatch

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 6, "c": 7, "ref": "BID ITEM #01 - Valley Electric - Mini MACC ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 16, "c": 7, "ref": "BID ITEM #04 - South Trestle - Under Pier - $4,632,180", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 27, "c": 7, "ref": "BID ITEM #04 - North Trestle - Under Pier - $2,614,766", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 45, "c": 7, "ref": "BID ITEM #05 - South Trestle - Above Pier - $5,842,046", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 67, "c": 7, "ref": "Bid Item #05 - At Grade Walkway - $54,256", "sub": "value_mismatch"}

### prose_row/outside_workbook

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 1, "j": 6, "got": "ITEM #11 PED, VIEW,STAIRS,ELEV, METAL PANELS\n$102,240.00"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 11, "got": "Billing"}

### value_mismatch/unicode_variant

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 102, "c": 7, "i": 11, "j": 6, "ref": "Perform glazing work on OT at the Slip 3 OHL cab as outlined in Kenco COR 43.", "got": "Perform glazing work on oT at the Slip 3 OHL cab as outlined in Kenco
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 107, "c": 5, "i": 16, "j": 4, "ref": "WSF CO 300", "got": "WSF co 300"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 109, "c": 5, "i": 18, "j": 4, "ref": "WSF CO 300", "got": "WSF co 300"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 111, "c": 5, "i": 20, "j": 4, "ref": "WSF CO 300", "got": "WSF co 300"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 115, "c": 5, "i": 24, "j": 4, "ref": "WSF CO 300", "got": "WSF co 300"}

### missing_value/outside_table

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 4, "c": 13, "i": 0, "j": 12, "ref": "12/1/2025"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201"}

### merged_cells/text_differs

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "No. 5290015-40298", "columns": [9, 10]}

### merged_cells/text_intact

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}

### value_mismatch/overflow_hashes

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 132, "c": 8, "i": 3, "j": 7, "ref": " $9.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 133, "c": 8, "i": 4, "j": 7, "ref": " $10.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 134, "c": 8, "i": 5, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 135, "c": 8, "i": 6, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 136, "c": 8, "i": 7, "j": 7, "ref": " $11.00 ", "got": "#"}

### value_mismatch/reader_has_more

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 2, "c": 10, "i": 0, "j": 9, "ref": "1", "got": "1\n5290015-39648"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 31, "c": 10, "i": 29, "j": 9, "ref": " $-   ", "got": "$\n-\nI"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 34, "c": 10, "i": 32, "j": 9, "ref": " $-   ", "got": "$\n-\n-"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 48, "c": 10, "i": 46, "j": 9, "ref": " $-   ", "got": "$\n-\n-"}

### extra_value/not_in_workbook

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 2, "c": 12, "i": 0, "j": 11, "got": "Progress Billing No.\nBilling Period"}

### merged_rows/text_intact

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 3, "c": 14, "i": 1, "j": 13, "ref": "39", "got": "39\n07/12/20", "rows": [3, 4]}

### missing_cell/absent

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 3, "c": 1, "i": 1, "j": null, "ref": "Contractor: Hoffman-Pacific LLC, A Joint Venture • H-P Job No. 5290015"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 3, "c": 12, "i": 1, "j": null, "ref": "Progress Billing No."}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 10, "i": 2, "j": null, "ref": "5290015-39648"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 4, "c": 12, "i": 2, "j": null, "ref": "Billing Period  "}

### value_mismatch/clipped_edge

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 214, "c": 7, "i": 20, "j": 6, "ref": "Subcontractor shall add a pre-action fire sprinkler system to the TB IT room, a ", "got": "Subcontractor shall add a pre-action fire sprinkler system to the
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 227, "c": 7, "i": 4, "j": 6, "ref": "Make adjustment to previously issued subcontractor modification #023 to match re", "got": "Make adjustment to previously issued subcontractor modification #0
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 228, "c": 7, "i": 5, "j": 6, "ref": "Make adjustment to previously issued subcontractor modification #023 to match re", "got": "Make adjustment to previously issued subcontractor modification #0

