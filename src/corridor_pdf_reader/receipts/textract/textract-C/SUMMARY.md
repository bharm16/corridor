# Loop score: textract-C

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 0 / 10 |
| Pages passing | 6 / 53 |
| Reference cells exact | 12427 / 14641 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| uncovered | 2156 |  | 10 |
| value_mismatch | 1907 | 41 | 10 |
| unexplained_text | 170 | 18 | 9 |
| extra_value | 165 | 10 | 5 |
| outside_table | 149 | 19 | 9 |
| missing_value | 145 | 10 | 6 |
| span_mismatch | 16 | 10 | 5 |
| merged_rows | 8 | 3 | 3 |
| prose_row | 6 | 3 | 2 |
| missing_row | 6 | 2 | 2 |
| unaligned_row | 5 | 1 | 1 |
| merged_cells | 1 | 1 | 1 |
| missing_cell | 1 | 1 | 1 |
| unaligned_table | 1 | 1 | 1 |

## Subclasses

| Subclass | Cells |
|---|---:|
| uncovered/value_mismatch | 1826 |
| value_mismatch/different | 876 |
| value_mismatch/format_only | 856 |
| uncovered/absent | 189 |
| extra_value/not_in_workbook | 162 |
| outside_table/cell_text_outside | 149 |
| missing_value/in_table_elsewhere | 142 |
| uncovered/outside_table | 141 |
| value_mismatch/reader_has_more | 88 |
| unexplained_text/edge | 67 |
| unexplained_text/text | 54 |
| unexplained_text/numeric | 49 |
| value_mismatch/clipped_suffix | 22 |
| value_mismatch/clipped_prefix | 21 |
| value_mismatch/unicode_variant | 17 |
| span_mismatch/wider | 16 |
| value_mismatch/overflow_hashes | 11 |
| value_mismatch/clipped_edge | 8 |
| merged_rows/text_intact | 8 |
| prose_row/outside_workbook | 6 |
| value_mismatch/clipped_middle | 4 |
| value_mismatch/reader_has_less | 4 |
| extra_value/wrong_row | 3 |
| missing_row/absent | 3 |
| missing_row/in_table_elsewhere | 3 |
| unaligned_row/misplaced | 3 |
| unaligned_row/not_in_workbook | 2 |
| missing_value/absent | 2 |
| missing_value/outside_table | 1 |
| merged_cells/text_intact | 1 |
| missing_cell/in_table_elsewhere | 1 |
| unaligned_table/no_key_in_workbook | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| AdobePDFLibrary | 0/1 | 0/1 | 553/571 |
| Distiller | 0/1 | 0/2 | 60/105 |
| Excel | 0/4 | 2/33 | 10844/12303 |
| Ghostscript | 0/2 | 4/11 | 710/1345 |
| PrintToPDF | 0/2 | 0/6 | 260/317 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| b62895f03208009f | 85531_1010_Index.pdf | Ghostscript | 10 | 641 | 619 |
| 02753bf7e6549f5a | 19132-71-VE-June-2023.pdf | Excel | 13 | 674 | 475 |
| 5e979146e9322953 | 5007_20-08-Billing.pdf | Excel | 9 | 501 | 489 |
| 075ad4da3647d7dc | 12.2025-Kenco-SOV.pdf | Excel | 6 | 344 | 278 |
| 1ee4a00b8eb6173b | REVISED-McClean-December-SOV.pdf | Excel | 5 | 279 | 166 |
| 9d701a8ad492a6d7 | status-mobility-fy2018.pdf | Distiller | 2 | 50 | 44 |
| 99e3871a86ca9971 | status-mobility-fy2019.pdf | PrintToPDF | 2 | 27 | 35 |
| 50b23088b99df7c2 | Billing-Review-Comments-Disposition-Est-95.pdf | PrintToPDF | 4 | 29 | 22 |
| 5d36b973fa9393a8 | GEA_608-ROWEstimate_20170502.pdf | AdobePDFLibrary | 1 | 19 | 18 |
| fcbe0ceb23e61b0f | PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2 | Ghostscript | 1 | 16 | 10 |

## Examples

### unexplained_text/edge

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "PACIFIC LLC"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "HOFFMAN"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "A JOINT VENTURE"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "got": "Project: Seattle Multimodal Terminal at Colman Dock Project Seattle, WA."}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "got": "Invoice No. 70"}

### outside_table/cell_text_outside

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "BILLING SUMMARY"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Sub: Valley Electric 1100 Merrill Creek Parkway Everett WA 98203"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "70"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Progress Billing No."}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "06/12/23"}

### unexplained_text/text

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Seattle, WA"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "H-P Job No. 5290015"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Bid Package No. 0"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Date: 06/20/23"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "Invoice No. 70"}

### unexplained_text/numeric

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "-"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "119,223.00"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 1, "got": "$"}
- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 1, "got": "$"}

### value_mismatch/different

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 6, "c": 7, "i": 1, "j": 6, "ref": "BID ITEM #01 - Valley Electric - Mini MACC ", "got": "BID ITEM #01 Valley Electric Mini MACC"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 14, "c": 14, "i": 9, "j": 13, "ref": " $-   ", "got": "$\n."}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 16, "c": 7, "i": 11, "j": 6, "ref": "BID ITEM #04 - South Trestle - Under Pier - $4,632,180", "got": "BID ITEM #04 South Trestle Under Pier $4,632,180"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 21, "c": 5, "i": 16, "j": 4, "ref": "CO 034 \n90-100", "got": "CO.034\n90-100"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 23, "c": 16, "i": 18, "j": 15, "ref": " $-   ", "got": "$\n."}

### value_mismatch/format_only

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 14, "c": 9, "i": 9, "j": 8, "ref": " $-   ", "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 20, "c": 16, "i": 15, "j": 15, "ref": " $-   ", "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 40, "c": 14, "i": 35, "j": 13, "ref": " $251,301.42 ", "got": "251,301.42"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 41, "c": 9, "i": 36, "j": 8, "ref": " $6,348.02 ", "got": "$\n6,348.02\n$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 41, "c": 10, "i": 36, "j": 9, "ref": " $6,348.02 ", "got": "6,348.02"}

### extra_value/not_in_workbook

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 40, "c": 13, "i": 35, "j": 12, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 41, "c": 13, "i": 36, "j": 12, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 42, "c": 8, "i": 37, "j": 7, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 42, "c": 13, "i": 37, "j": 12, "got": "$"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 43, "c": 8, "i": 38, "j": 7, "got": "$"}

### value_mismatch/clipped_prefix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 5, "c": 4, "i": 0, "j": 3, "ref": "CE Item #", "got": "CE\nItem"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 219, "c": 5, "i": 34, "j": 5, "ref": "WSF CO 300", "got": "WSF CO"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 220, "c": 5, "i": 35, "j": 5, "ref": "WSF CO 300", "got": "WSF CO"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 221, "c": 5, "i": 36, "j": 5, "ref": "WSF CO 300", "got": "WSF CO"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 5, "c": 4, "i": 0, "j": 4, "ref": "CE Item #", "got": "CE\nItem"}

### value_mismatch/reader_has_more

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 5, "c": 5, "i": 0, "j": 4, "ref": "Ref Doc", "got": "#\nRef Doc"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 87, "c": 6, "i": 38, "j": 5, "ref": "5", "got": "5\nPA"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 219, "c": 4, "i": 34, "j": 4, "ref": "2", "got": "2\n300"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 220, "c": 4, "i": 35, "j": 4, "ref": "2", "got": "2\n300"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 221, "c": 4, "i": 36, "j": 4, "ref": "2", "got": "2\n300"}

### value_mismatch/clipped_suffix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 87, "c": 7, "i": 38, "j": 6, "ref": "PA SYSTEM", "got": "SYSTEM"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 226, "c": 7, "i": 41, "j": 7, "ref": "100 % PRICING ADDITION", "got": "% PRICING ADDITION"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 262, "c": 5, "i": 34, "j": 5, "ref": "NSS #16", "got": "#16"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 263, "c": 5, "i": 35, "j": 5, "ref": "CO 008", "got": "008"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 264, "c": 5, "i": 36, "j": 5, "ref": "CO 012", "got": "012"}

### missing_value/in_table_elsewhere

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 5, "c": 1, "i": 0, "j": 0, "ref": "Job Area # \nCost Code"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 186, "c": 1, "i": 1, "j": 0, "ref": "00.260000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 187, "c": 1, "i": 2, "j": 0, "ref": "00.260000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 188, "c": 1, "i": 3, "j": 0, "ref": "00.260000"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 189, "c": 1, "i": 4, "j": 0, "ref": "00.260000"}

### value_mismatch/clipped_middle

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 222, "c": 5, "i": 37, "j": 5, "ref": "WSF CO 300", "got": "CO"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 227, "c": 5, "i": 42, "j": 5, "ref": "WSF CO 300", "got": "CO"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 7, "table": 0, "sheet": "SOV", "r": 228, "c": 5, "i": 43, "j": 5, "ref": "WSF CO 300", "got": "CO"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 7, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to com

### span_mismatch/wider

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 398, "c": 7, "i": 25, "j": 7, "ref": "Install Electrical Utility Racks at Entry Building per RFI 1653 and as outlined ", "expected": [7], "got_columns": [7, 8]}
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 1, "table": 0, "sheet": "Billing Issues", "r": 1, "c": 1, "i": 0, "j": 0, "ref": "Seattle Multimodal Terminal at Colman Dock", "expected": [1], "got_columns": [1, 2]}
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 2, "table": 0, "sheet": "Billing Issues", "r": 1, "c": 1, "i": 0, "j": 0, "ref": "Seattle Multimodal Terminal at Colman Dock", "expected": [1], "got_columns": [1, 2]}
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 3, "table": 0, "sheet": "Billing Issues", "r": 1, "c": 1, "i": 0, "j": 0, "ref": "Seattle Multimodal Terminal at Colman Dock", "expected": [1], "got_columns": [1, 2]}
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 4, "table": 0, "sheet": "Billing Issues", "r": 1, "c": 1, "i": 0, "j": 0, "ref": "Seattle Multimodal Terminal at Colman Dock", "expected": [1], "got_columns": [1, 2]}

### value_mismatch/unicode_variant

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": 409, "c": 7, "i": 6, "j": 6, "ref": "Supply and install additional electrical platforms per WSF SL 324 and as \noutlin", "got": "Supply and install additional electrical platforms per WSF SL
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 119, "c": 5, "i": 29, "j": 4, "ref": "WSF CO 030 \nVPAC", "got": "WSF co 030\nVPAC"}
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 2, "table": 0, "sheet": "Billing Issues", "r": 14, "c": 7, "i": 5, "j": 6, "ref": "Completed all punchlist items above pier. No change to the SOV", "got": "Completed all punchlist items above pier. No chang
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 3, "table": 0, "sheet": "Billing Issues", "r": 19, "c": 6, "i": 3, "j": 5, "ref": "Excel row 236, CO 300, Entry Building – Demobilization, is being billed from 40-", "got": "Excel row 236, CO 300, Entry Bui
- {"pair": "50b23088b99df7c2", "pdf": "Billing-Review-Comments-Disposition-Est-95.pdf", "page": 3, "table": 0, "sheet": "Billing Issues", "r": 22, "c": 6, "i": 6, "j": 5, "ref": "Excel row 374, CO 400, Yesler Plaza – Finish/Grout (HSI), item 241, is being bil", "got": "Excel row 374, CO 400, Yesler Pl

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
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 14, "c": 9, "ref": " $-   ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 14, "c": 14, "ref": " $-   ", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 16, "c": 7, "ref": "BID ITEM #04 - South Trestle - Under Pier - $4,632,180", "sub": "value_mismatch"}
- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "sheet": "SOV", "r": 20, "c": 16, "ref": " $-   ", "sub": "value_mismatch"}

### prose_row/outside_workbook

- {"pair": "075ad4da3647d7dc", "pdf": "12.2025-Kenco-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 1, "j": 6, "got": "ITEM #11 PED, VIEW,STAIRS,ELEV, METAL PANELS\n$102,240.00"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 0, "got": "Sub: McClean Iron Works"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 35, "j": 5, "got": "Net"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 35, "j": 6, "got": "Amount of Invoice"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 35, "j": 10, "got": "$"}

### missing_value/outside_table

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 1, "i": 0, "j": 0, "ref": "Sub: McClean Iron Works 2102 Ross Ave Everett WA 98201"}

### merged_cells/text_intact

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 0, "j": 8, "ref": "Subcontract No. | 5290015-40298", "got": "Subcontract No. 5290015-40298", "columns": [9, 10]}

### value_mismatch/overflow_hashes

- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 132, "c": 8, "i": 3, "j": 7, "ref": " $9.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 133, "c": 8, "i": 4, "j": 7, "ref": " $10.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 134, "c": 8, "i": 5, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 135, "c": 8, "i": 6, "j": 7, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 136, "c": 8, "i": 7, "j": 7, "ref": " $11.00 ", "got": "#"}

### missing_cell/in_table_elsewhere

- {"pair": "5d36b973fa9393a8", "pdf": "GEA_608-ROWEstimate_20170502.pdf", "page": 1, "table": 0, "sheet": "RW", "r": 6, "c": 23, "i": 1, "j": null, "ref": "TEMPORARY R/W & COST TO CURE NOT CONSIDERED"}

### extra_value/wrong_row

- {"pair": "5d36b973fa9393a8", "pdf": "GEA_608-ROWEstimate_20170502.pdf", "page": 1, "table": 0, "sheet": "RW", "r": 7, "c": 23, "i": 2, "j": 16, "got": "TEMPORARY R/W &\nCOST TO CURE NOT\nCONSIDERED"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 0, "sheet": "DGN Files", "r": 25, "c": 4, "i": 0, "j": 2, "got": "DESCRIPTION"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": 46, "c": 4, "i": 0, "j": 2, "got": "DESCRIPTION"}

### value_mismatch/clipped_edge

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 24, "c": 7, "i": 20, "j": 6, "ref": "Make changes to correct valve box conflict per RFI 434 and Hoffman-Pacific Direc", "got": "Make changes to correct valve box conflict per RFI 434 and Hoffman
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 107, "c": 1, "i": 1, "j": 0, "ref": "00.220000", "got": "220000"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 111, "c": 1, "i": 5, "j": 0, "ref": "00.220000", "got": "220000"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 227, "c": 7, "i": 4, "j": 6, "ref": "Make adjustment to previously issued subcontractor modification #023 to match re", "got": "Make adjustment to previously issued subcontractor modification #0
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 228, "c": 7, "i": 5, "j": 6, "ref": "Make adjustment to previously issued subcontractor modification #023 to match re", "got": "Make adjustment to previously issued subcontractor modification #0

### value_mismatch/reader_has_less

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 120, "c": 1, "i": 14, "j": 0, "ref": "00.220000", "got": "220000"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 126, "c": 1, "i": 20, "j": 0, "ref": "00.220000", "got": "220000"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 259, "c": 1, "i": 8, "j": 0, "ref": "00.220000", "got": "220000"}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 9, "table": 0, "sheet": "SOV", "r": 260, "c": 1, "i": 9, "j": 0, "ref": "00.220000", "got": "220000"}

### merged_rows/text_intact

- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 227, "c": 2, "i": 4, "j": 1, "ref": "30", "got": "29\n30", "rows": [226, 227]}
- {"pair": "5e979146e9322953", "pdf": "5007_20-08-Billing.pdf", "page": 8, "table": 0, "sheet": "SOV", "r": 228, "c": 1, "i": 5, "j": 0, "ref": "00.220000", "got": "00.220000\n00.220000", "rows": [228, 229]}
- {"pair": "9d701a8ad492a6d7", "pdf": "status-mobility-fy2018.pdf", "page": 1, "table": 0, "sheet": "FY2018 Revenue & Cash Balance", "r": 15, "c": 2, "i": 8, "j": 1, "ref": " 21,765,874.54 ", "got": "21,765,874.54\n70,379.20", "rows": [15, 16]}
- {"pair": "9d701a8ad492a6d7", "pdf": "status-mobility-fy2018.pdf", "page": 1, "table": 0, "sheet": "FY2018 Revenue & Cash Balance", "r": 33, "c": 2, "i": 21, "j": 1, "ref": " 869,741.88 ", "got": "869,741.88\n749,852.56", "rows": [33, 34]}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": 50, "c": 2, "i": 5, "j": 0, "ref": "3", "got": "3\n4", "rows": [50, 51]}

### missing_row/absent

- {"pair": "9d701a8ad492a6d7", "pdf": "status-mobility-fy2018.pdf", "page": 1, "table": 0, "sheet": "FY2018 Revenue & Cash Balance", "r": 18, "c": 1, "i": null, "j": null, "ref": "Motor Carrier Act Penalties"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": 53, "c": 3, "i": null, "j": null, "ref": "1010_Roadway_Gateway\\roadway\\sheets\\85531_1010_GT006.dgn"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": 60, "c": 3, "i": null, "j": null, "ref": "1010_Roadway_Gateway\\roadway\\sheets\\85531_1010_GY007.dgn"}

### missing_row/in_table_elsewhere

- {"pair": "9d701a8ad492a6d7", "pdf": "status-mobility-fy2018.pdf", "page": 1, "table": 0, "sheet": "FY2018 Revenue & Cash Balance", "r": 18, "c": 2, "i": null, "j": null, "ref": " 3,536,345.18 "}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": 53, "c": 4, "i": null, "j": null, "ref": "RECORD CHANGE TABLE"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": 60, "c": 4, "i": null, "j": null, "ref": "TYPICAL SECTIONS; MAINLINE"}

### unaligned_table/no_key_in_workbook

- {"pair": "9d701a8ad492a6d7", "pdf": "status-mobility-fy2018.pdf", "page": 2, "table": 1, "got": "Appropriation²"}

### unaligned_row/misplaced

- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": null, "c": null, "i": 3, "j": 0, "got": "1"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": null, "c": null, "i": 3, "j": 2, "got": "INDEX OF SHEETS"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": null, "c": null, "i": 12, "j": 2, "got": "TYPICAL SECTIONS; MAINLINE"}

### unaligned_row/not_in_workbook

- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": null, "c": null, "i": 3, "j": 1, "got": "1010 Roadway Gateway\\roadway\\sheets\\85531 1010"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": null, "c": null, "i": 12, "j": 1, "got": "1010 Roadway Gateway\\roadway\\sheets\\85531 1010 GY005.dgn"}

### missing_value/absent

- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": 51, "c": 2, "i": 6, "j": 0, "ref": "4"}
- {"pair": "b62895f03208009f", "pdf": "85531_1010_Index.pdf", "page": 1, "table": 1, "sheet": "DGN Files", "r": 70, "c": 2, "i": 24, "j": 0, "ref": "19"}

