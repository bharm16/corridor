# Loop score: syn-003

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 261 / 281 |
| Pages passing | 1592 / 1627 |
| Reference cells exact | 535687 / 536967 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| uncovered | 1085 |  | 20 |
| missing_cell | 654 | 27 | 15 |
| missing_row | 430 | 4 | 4 |
| value_mismatch | 281 | 134 | 86 |
| extra_value | 19 | 15 | 14 |
| columns_folded | 17 | 9 | 7 |
| unaligned_row | 13 | 3 | 3 |
| prose_row | 10 | 5 | 3 |
| unverifiable | 2 | 2 | 2 |
| outside_table | 1 | 1 | 1 |

## Subclasses

| Subclass | Cells |
|---|---:|
| uncovered/absent | 1018 |
| missing_cell/absent | 553 |
| missing_row/absent | 310 |
| missing_row/in_table_elsewhere | 120 |
| missing_cell/in_table_elsewhere | 101 |
| uncovered/value_mismatch | 65 |
| value_mismatch/different | 62 |
| value_mismatch/clipped_evidence | 53 |
| value_mismatch/overflow_hashes | 41 |
| value_mismatch/clipped_edge | 40 |
| value_mismatch/clipped_prefix | 30 |
| value_mismatch/clipped_suffix | 19 |
| columns_folded/unmapped_column | 17 |
| extra_value/not_in_workbook | 14 |
| value_mismatch/reader_has_more | 14 |
| value_mismatch/epoch_zero | 12 |
| prose_row/outside_workbook | 10 |
| value_mismatch/rounded_to_width | 10 |
| unaligned_row/misplaced | 8 |
| unaligned_row/not_in_workbook | 5 |
| extra_value/wrong_column | 3 |
| extra_value/wrong_row | 2 |
| uncovered/outside_table | 2 |
| unverifiable/print_area_overflow | 2 |
| outside_table/cell_text_outside | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| LibreOffice | 261/281 | 1592/1627 | 535687/536967 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| edcd31ccaa87dae1 | Beacon-Master-Billing-Form-Pay-Application-1-Revie | LibreOffice | 2 | 142 | 145 |
| 4fc27f0d7d3b36d0 | Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf | LibreOffice | 2 | 136 | 140 |
| 9cc782760ee360df | Commencement-Dock-Pay-Application-2-August-2019-SO | LibreOffice | 2 | 136 | 140 |
| 26d1e96822e56aa6 | Sound-Beacon-Billing-Master-Form-SB-July-2022.pdf | LibreOffice | 3 | 104 | 103 |
| 7e2545284a5b26b6 | Larkin-Tidewater-Billing-Master-Form-LT-June-2023. | LibreOffice | 3 | 95 | 91 |
| ea1c3036fd16b394 | Kestrel-Heron-Billing-Master-Form-KH-September-201 | LibreOffice | 2 | 87 | 87 |
| 90688e5f5a118a1b | Northwind-Marlow-Billing-Master-Form-NM-March-2021 | LibreOffice | 2 | 80 | 79 |
| 323fb1791291c7a4 | Copy-of-NEC-June-2018.pdf | LibreOffice | 2 | 77 | 74 |
| 3fd27b9f51571044 | H-S-I-Billing-Inv-056-04-20-23-r0.pdf | LibreOffice | 12 | 140 | 10 |
| 7699bd26d4dd0f38 | TGG-OCTOBER-2018-R0-billing.pdf | LibreOffice | 2 | 74 | 71 |
| 9680ef1710d3ae16 | Jan-SOV-2022.pdf | LibreOffice | 2 | 32 | 39 |
| bd816e084eaa0f09 | Feb-SOV-2022.pdf | LibreOffice | 2 | 32 | 39 |
| 9fd5a83f3a8e6feb | Oct.-2021.pdf | LibreOffice | 2 | 25 | 35 |
| 0b8c528592f487a0 | Feb.-2019.pdf | LibreOffice | 4 | 32 | 9 |
| 822f62ef307eea4a | DCW-Cost-Bid-Tab-Comparative-Plaza-Cost-Report-Yes | LibreOffice | 12 | 20 | 3 |
| 68e89da0e50104c1 | June-2019.pdf | LibreOffice | 4 | 10 | 8 |
| 33534c642ee2b0aa | REVISED-Tidewater-December-SOV.pdf | LibreOffice | 5 | 16 | 0 |
| b4e8635aebec0ab5 | East-Slip-BMC-Billings-10-032519-SS-EDITS.pdf | LibreOffice | 3 | 8 | 6 |
| 0ec8b70413e38ad8 | H-S-I-Billing-Inv-057-05-20-23-r0.pdf | LibreOffice | 11 | 9 | 3 |
| 07a2de86dcccb249 | R06_TOC.pdf | LibreOffice | 3 | 7 | 2 |
| 6edfd60f503e9e9e | H-S-I-Billing-Inv-078-07-20-25-r0.pdf | LibreOffice | 13 | 9 | 0 |
| 2343cf6d15d3215f | Copy-of-H-S-I-Billing-Inv-071-09-20-24-r0.pdf | LibreOffice | 12 | 8 | 0 |
| 258946a35babc9eb | H-S-I-Billing-Inv-071-09-20-24-r1.pdf | LibreOffice | 12 | 8 | 0 |
| 2d46aefb06ddf3cf | Copy-of-H-S-I-Billing-Inv-073-12-20-24-r0.pdf | LibreOffice | 12 | 8 | 0 |
| 54d6211bc4461b68 | sov.pdf | LibreOffice | 14 | 8 | 0 |

## Examples

### value_mismatch/clipped_edge

- {"pair": "005ed06085c446cd", "pdf": "H-S-I-Billing-Inv-039-10-20-21.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 204, "c": 7, "i": 25, "j": 7, "ref": "Perform PS 03 work dated 08/09/2018 as tracked on OSP tickets and per T.P. cost ", "got": "Perform PS 03 work dated 08/09/2018 as tracked on OSP
- {"pair": "06b1617d5e4567e5", "pdf": "02.2019-PA-12-Northwind.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 86, "c": 7, "i": 25, "j": 7, "ref": "WSH SL 686 and as outlined in Heron CP 153R1. Work was done to maintain project ", "got": "WSH SL 686 and as outlined in Heron CP 153R1. Work was done t
- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "Actng Only", "r": 325, "c": 7, "i": 7, "j": 5, "ref": "F/R/P concrete light pole foundation per WSM SL 650 and as outlined in S.R. COR ", "got": "F/R/P concrete ligh"}
- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "Actng Only", "r": 326, "c": 7, "i": 8, "j": 5, "ref": "Add two HK pads and fence/gate/roof encolsures per SL 643 and as outlined in S.R", "got": "Add two HK pads an"}
- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "Actng Only", "r": 328, "c": 7, "i": 10, "j": 5, "ref": "Perform column casing demolition work per RFI 873.1 as tracked on WSM FA tickets", "got": "Perform column cas"}

### value_mismatch/clipped_prefix

- {"pair": "06b1617d5e4567e5", "pdf": "02.2019-PA-12-Northwind.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 84, "c": 7, "i": 23, "j": 7, "ref": "Provide valances at View Deck Elevators per RFI 1592 and as outlined in Northwin", "got": "Provide valances at View Deck Elevators per RFI 1592 and as o
- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 204, "c": 7, "i": 9, "j": 7, "ref": "Perform PS 03 work dated 01/24/2018 as tracked on WSM tickets and per S.R. cost ", "got": "Perform PS 03 work dated 01/24/2018 as tracked on W
- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "Actng Only", "r": 327, "c": 7, "i": 9, "j": 5, "ref": "Added detailing and material costs resulting from SST granite artwork supports c", "got": "Added detailing and"}
- {"pair": "16190ac969923bcf", "pdf": "05.2021-PA-29-Inv.-3888-Kencoxlsx.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 86, "c": 7, "i": 25, "j": 7, "ref": "Supply and install stainless steel junction boxes at blockout penetrations at Te", "got": "Supply and install stainless steel junction boxes a
- {"pair": "1ed83946a20c3d13", "pdf": "April-2021.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 86, "c": 7, "i": 25, "j": 7, "ref": "Change lighting fixtures at handrail to accommodate the 2-inch SST grab rail per", "got": "Change lighting fixtures at handrail to accommodate the 2-inch SST grab ra

### prose_row/outside_workbook

- {"pair": "07a2de86dcccb249", "pdf": "R06_TOC.pdf", "page": 1, "table": 0, "sheet": "Table 1", "r": null, "c": null, "i": 0, "j": 2, "got": "Illumination schedule\n3 I-405 - Larkin to"}
- {"pair": "07a2de86dcccb249", "pdf": "R06_TOC.pdf", "page": 1, "table": 0, "sheet": "Table 1", "r": null, "c": null, "i": 1, "j": 0, "got": "Or, at a minimum, whe"}
- {"pair": "07a2de86dcccb249", "pdf": "R06_TOC.pdf", "page": 1, "table": 0, "sheet": "Table 1", "r": null, "c": null, "i": 2, "j": 0, "got": "5 Table of Conte"}
- {"pair": "07a2de86dcccb249", "pdf": "R06_TOC.pdf", "page": 2, "table": 0, "sheet": "Table 1", "r": null, "c": null, "i": 0, "j": 1, "got": "needs to be programmed."}
- {"pair": "07a2de86dcccb249", "pdf": "R06_TOC.pdf", "page": 2, "table": 0, "sheet": "Table 1", "r": null, "c": null, "i": 2, "j": 1, "got": "n the east elevation is in shadow, Scuff mark on west end."}

### value_mismatch/clipped_suffix

- {"pair": "07a2de86dcccb249", "pdf": "R06_TOC.pdf", "page": 2, "table": 0, "sheet": "Table 1", "r": 1, "c": 1, "i": 1, "j": 1, "ref": "Illumination schedule needs to be programmed.\n3       I-405 - Larkin to SR527 Im", "got": "SR527 Improvement Project"}
- {"pair": "40cabd65a2d1ab4b", "pdf": "Copy-of-19132-73-VE-August-2023.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 16, "j": 7, "ref": "Provide premium overtime only, including straight OT and double time OT, to acce", "got": "third of the building per project schedule as direc
- {"pair": "45fab85a762e5833", "pdf": "19132-92-July-2025.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 7, "ref": "Remove previously issued work added in Ashby subcontract modification 039 relate", "got": "changes to the projects IT systems, including infrastructure per
- {"pair": "466f220bbd7e7f1e", "pdf": "19132-84-VE-September-2024-Rev1.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 16, "j": 7, "ref": "Supply and install presence sensors and bollards at Terminal Building only per O", "got": "delta from Steelhead CP 46R2 previously approved by
- {"pair": "54d6211bc4461b68", "pdf": "sov.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 18, "j": 7, "ref": "Provide maintenance at Salmon Bay Dock per WSH 554 direction and as directed by ", "got": "WSH. This cost is for March 30th through April 3rd only. Weekly maintenance inc

### uncovered/absent

- {"pair": "07a2de86dcccb249", "pdf": "R06_TOC.pdf", "sheet": "Table 1", "r": 2, "c": 1, "ref": "Or, at a minimum, when the east elevation is in shadow, Scuff mark on west end.", "sub": "absent"}
- {"pair": "07a2de86dcccb249", "pdf": "R06_TOC.pdf", "sheet": "Table 1", "r": 3, "c": 1, "ref": "5       Table of Contents", "sub": "absent"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "sheet": "Summary-HCC Only", "r": 6, "c": 3, "ref": "0", "sub": "absent"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "sheet": "Summary-HCC Only", "r": 10, "c": 3, "ref": "0", "sub": "absent"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "sheet": "Summary-HCC Only", "r": 11, "c": 3, "ref": "0", "sub": "absent"}

### extra_value/not_in_workbook

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 6, "c": 4, "i": 1, "j": 5, "got": "Astoria Trestle at Fairhaven Dock Project • Astoria, OR."}
- {"pair": "26d1e96822e56aa6", "pdf": "Sound-Beacon-Billing-Master-Form-SB-July-2022.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 6, "c": 5, "i": 2, "j": 4, "got": "Tacoma Multimodal Terminal at Cannery Wharf Project • Tacoma, WA."}
- {"pair": "323fb1791291c7a4", "pdf": "Copy-of-NEC-June-2018.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 6, "c": 5, "i": 1, "j": 6, "got": "Astoria Ferry Terminal at North Landing Project • Astoria, OR."}
- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 8, "c": 5, "i": 3, "j": 6, "got": "Larkin Steel Fabricators PO Box 41014, Vancouver WA 98660"}
- {"pair": "68e89da0e50104c1", "pdf": "June-2019.pdf", "page": 4, "table": 0, "sheet": "Summary-HCC Only", "r": 6, "c": 6, "i": 1, "j": 7, "got": "Port Aransas Maintenance Facility at Cannery Wharf Project • Port Aransas, TX."}

### value_mismatch/different

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 7, "c": 2, "i": 2, "j": 3, "ref": "0", "got": "Contractor: Steelhead-Thornton LLC, A Joint Venture • S-T Job No. 5537451"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 10, "c": 2, "i": 4, "j": 3, "ref": "Bid Package No.", "got": "#2"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 11, "c": 2, "i": 5, "j": 3, "ref": "Invoice No.", "got": "38IN-562107"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 12, "c": 2, "i": 6, "j": 3, "ref": "Subcontract No.", "got": "5537451-30561-OS Vendor No: 34821"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 14, "c": 2, "i": 7, "j": 3, "ref": "Progress Billing No.", "got": "4"}

### columns_folded/unmapped_column

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 8, "c": 3, "i": 3, "j": 5, "ref": "Copperline Insulation 4871 Commerce St Fife, WA 98424", "got": "Copperline Insulation 4871 Commerce St Fife, WA 98424"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 17, "c": 3, "i": 9, "j": 3, "ref": "Voucher#", "got": "Voucher#"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 12, "i": 7, "j": 0, "ref": "Progress Billing No.", "got": "Progress Billing No."}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 73, "c": 5, "i": 9, "j": 8, "ref": "NSS Temp Access", "got": "NSS Temp Access"}
- {"pair": "3fd27b9f51571044", "pdf": "H-S-I-Billing-Inv-056-04-20-23-r0.pdf", "page": 9, "table": 0, "sheet": "Actng Only", "r": 5, "c": 2, "i": 3, "j": 2, "ref": "B I L L I N G   S U M M A R Y", "got": "B I L L I N G S U M M A R Y"}

### unaligned_row/not_in_workbook

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 1, "got": "B I L L I N G S U M M A R Y"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 3, "j": 1, "got": "Sub:"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 3, "j": 4, "got": "Copperline Insulation 4871 Commerce St Fife, WA 98424"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 6, "j": 4, "got": "5537451-30561-OS Vendor No: 34821"}
- {"pair": "822f62ef307eea4a", "pdf": "DCW-Cost-Bid-Tab-Comparative-Plaza-Cost-Report-Yesler-11.5.23-WSP.pdf", "page": 6, "table": 0, "sheet": "Summary", "r": null, "c": null, "i": 1, "j": 0, "got": "ll been reviewed and reconciled. This\nletion date of the original DCW\ntemporary "}

### unaligned_row/misplaced

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 1, "j": 1, "got": "Project:"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 1, "j": 4, "got": "Astoria Trestle at Fairhaven Dock Project • Astoria, OR."}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 2, "j": 1, "got": "Contractor: Steelhead-Thornton LLC, A Joint Venture • S-T Job No. 5537451"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 6, "j": 2, "got": "Subcontract No."}
- {"pair": "3fd27b9f51571044", "pdf": "H-S-I-Billing-Inv-056-04-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 3, "j": 0, "got": "0"}

### extra_value/wrong_row

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 3, "c": null, "i": 5, "j": 3, "got": "Invoice No."}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 3, "c": 10, "i": 5, "j": 4, "got": "38IN-562107"}

### missing_cell/absent

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 3, "c": 9, "i": 5, "j": null, "ref": "Original Contract Amount:"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 5, "c": 10, "i": 8, "j": null, "ref": "5537451"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 73, "c": 9, "i": 9, "j": null, "ref": " $7,600.00 "}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 73, "c": 10, "i": 9, "j": null, "ref": " $4,412.43 "}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 73, "c": 14, "i": 9, "j": null, "ref": " $4,412.56 "}

### missing_cell/in_table_elsewhere

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 9, "i": 7, "j": null, "ref": "Invoice No."}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 4, "c": 10, "i": 7, "j": null, "ref": "38IN-562107"}
- {"pair": "323fb1791291c7a4", "pdf": "Copy-of-NEC-June-2018.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 8, "c": 3, "i": 3, "j": null, "ref": "Northwind Elevator Co. 16705 Commerce St Lynnwood, WA 98036"}
- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 9, "c": 14, "i": 6, "j": null, "ref": " $-   "}
- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 9, "c": 16, "i": 6, "j": null, "ref": " $-   "}

### uncovered/value_mismatch

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "sheet": "Summary-HCC Only", "r": 7, "c": 2, "ref": "0", "sub": "value_mismatch"}
- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "sheet": "Summary-HCC Only", "r": 19, "c": 2, "ref": "#ERR", "sub": "value_mismatch"}
- {"pair": "26d1e96822e56aa6", "pdf": "Sound-Beacon-Billing-Master-Form-SB-July-2022.pdf", "sheet": "Summary-HCC Only", "r": 7, "c": 2, "ref": "0", "sub": "value_mismatch"}
- {"pair": "26d1e96822e56aa6", "pdf": "Sound-Beacon-Billing-Master-Form-SB-July-2022.pdf", "sheet": "Summary-HCC Only", "r": 10, "c": 3, "ref": "0", "sub": "value_mismatch"}
- {"pair": "26d1e96822e56aa6", "pdf": "Sound-Beacon-Billing-Master-Form-SB-July-2022.pdf", "sheet": "Summary-HCC Only", "r": 12, "c": 3, "ref": "0", "sub": "value_mismatch"}

### uncovered/outside_table

- {"pair": "0b8c528592f487a0", "pdf": "Feb.-2019.pdf", "sheet": "Summary-HCC Only", "r": 14, "c": 3, "ref": "4", "sub": "outside_table"}
- {"pair": "822f62ef307eea4a", "pdf": "DCW-Cost-Bid-Tab-Comparative-Plaza-Cost-Report-Yesler-11.5.23-WSP.pdf", "sheet": "Cover", "r": 1, "c": 4, "ref": "November 5, 2022", "sub": "outside_table"}

### value_mismatch/overflow_hashes

- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 9, "table": 0, "sheet": "Actng Only", "r": 2, "c": 16, "i": 0, "j": 10, "ref": "3/23/2020", "got": "###"}
- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 9, "table": 0, "sheet": "Actng Only", "r": 4, "c": 14, "i": 2, "j": 9, "ref": "2/22/2020", "got": "###"}
- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 9, "table": 0, "sheet": "Actng Only", "r": 4, "c": 16, "i": 2, "j": 10, "ref": "3/23/2020", "got": "###"}
- {"pair": "107796d2706928cb", "pdf": "New-Summit-SOV-Submitted-to-TST.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 4, "c": 13, "i": 3, "j": 13, "ref": "7/27/2017", "got": "###"}
- {"pair": "2343cf6d15d3215f", "pdf": "Copy-of-H-S-I-Billing-Inv-071-09-20-24-r0.pdf", "page": 10, "table": 0, "sheet": "Actng Only", "r": 2, "c": 16, "i": 0, "j": 10, "ref": "7/11/2024", "got": "###"}

### value_mismatch/rounded_to_width

- {"pair": "0ec8b70413e38ad8", "pdf": "H-S-I-Billing-Inv-057-05-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "Actng Only", "r": 327, "c": 12, "i": 9, "j": 8, "ref": "26945.2854", "got": "26945.29"}
- {"pair": "2343cf6d15d3215f", "pdf": "Copy-of-H-S-I-Billing-Inv-071-09-20-24-r0.pdf", "page": 12, "table": 0, "sheet": "Actng Only", "r": 327, "c": 12, "i": 12, "j": 8, "ref": "26945.2854", "got": "26945.29"}
- {"pair": "258946a35babc9eb", "pdf": "H-S-I-Billing-Inv-071-09-20-24-r1.pdf", "page": 12, "table": 0, "sheet": "Actng Only", "r": 327, "c": 12, "i": 12, "j": 8, "ref": "26945.2854", "got": "26945.29"}
- {"pair": "2d46aefb06ddf3cf", "pdf": "Copy-of-H-S-I-Billing-Inv-073-12-20-24-r0.pdf", "page": 12, "table": 0, "sheet": "Actng Only", "r": 327, "c": 12, "i": 12, "j": 8, "ref": "26945.2854", "got": "26945.29"}
- {"pair": "3fd27b9f51571044", "pdf": "H-S-I-Billing-Inv-056-04-20-23-r0.pdf", "page": 12, "table": 0, "sheet": "Actng Only", "r": 327, "c": 12, "i": 2, "j": 8, "ref": "26945.2854", "got": "26945.29"}

### extra_value/wrong_column

- {"pair": "323fb1791291c7a4", "pdf": "Copy-of-NEC-June-2018.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 8, "c": 5, "i": 3, "j": 6, "got": "Northwind Elevator Co. 16705 Commerce St Lynnwood, WA 98036"}
- {"pair": "7699bd26d4dd0f38", "pdf": "TGG-OCTOBER-2018-R0-billing.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 8, "c": 5, "i": 3, "j": 4, "got": "Tidewater Glass & Glazing 11735 Commerce St Kent, WA 98032"}
- {"pair": "b4e8635aebec0ab5", "pdf": "East-Slip-BMC-Billings-10-032519-SS-EDITS.pdf", "page": 3, "table": 0, "sheet": "Summary-HCC Only", "r": 8, "c": 5, "i": 3, "j": 6, "got": "Basalt Mechanical Co. 3788 Commerce St Kent, WA 98032"}

### value_mismatch/clipped_evidence

- {"pair": "33534c642ee2b0aa", "pdf": "REVISED-Tidewater-December-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 1, "c": 2, "i": 0, "j": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "got": "ove the green shading in the data entry areas on the for
- {"pair": "33534c642ee2b0aa", "pdf": "REVISED-Tidewater-December-SOV.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 1, "c": 2, "i": 0, "j": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "got": "ove the green shading in the data entry areas on the for
- {"pair": "33534c642ee2b0aa", "pdf": "REVISED-Tidewater-December-SOV.pdf", "page": 3, "table": 0, "sheet": "SOV", "r": 1, "c": 2, "i": 0, "j": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "got": "ove the green shading in the data entry areas on the for
- {"pair": "33534c642ee2b0aa", "pdf": "REVISED-Tidewater-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 1, "c": 2, "i": 0, "j": 2, "ref": "¬ BEFORE FINAL PRINTING enter any digit to remove the green shading in the data ", "got": "ove the green shading in the data entry areas on the for
- {"pair": "392e95291946b063", "pdf": "Billing-Review-Comments-Disposition-Est-119.pdf", "page": 1, "table": 0, "sheet": "Billing Issues", "r": 1, "c": 1, "i": 0, "j": 1, "ref": "Port Aransas Vehicle Holding Facility at Fairhaven Dock", "got": "Port Aransas Vehicle Holding Facility at Fairhaven Doc"}

### value_mismatch/epoch_zero

- {"pair": "33534c642ee2b0aa", "pdf": "REVISED-Tidewater-December-SOV.pdf", "page": 5, "table": 0, "sheet": "Actng Only", "r": 16, "c": 3, "i": 8, "j": 2, "ref": "01/00/00", "got": "12/30/99"}
- {"pair": "45fab85a762e5833", "pdf": "19132-92-July-2025.pdf", "page": 13, "table": 0, "sheet": "Actng Only", "r": 11, "c": 5, "i": 5, "j": 5, "ref": "01/00/00", "got": "12/30/99"}
- {"pair": "54d6211bc4461b68", "pdf": "sov.pdf", "page": 13, "table": 0, "sheet": "Actng Only", "r": 11, "c": 5, "i": 5, "j": 5, "ref": "01/00/00", "got": "12/30/99"}
- {"pair": "6de4a58d6c602842", "pdf": "Coleman-Dock-8-6.14.19.pdf", "page": 2, "table": 0, "sheet": "Actng Only", "r": 16, "c": 3, "i": 8, "j": 2, "ref": "01/00/00", "got": "12/30/99"}
- {"pair": "8966e35527f5b248", "pdf": "Steelhead-19132-95-December-2025.pdf", "page": 13, "table": 0, "sheet": "Actng Only", "r": 11, "c": 5, "i": 5, "j": 5, "ref": "01/00/00", "got": "12/30/99"}

### missing_row/absent

- {"pair": "3fd27b9f51571044", "pdf": "H-S-I-Billing-Inv-056-04-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "SOV", "r": 157, "c": 4, "i": null, "j": null, "ref": "11"}
- {"pair": "3fd27b9f51571044", "pdf": "H-S-I-Billing-Inv-056-04-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "SOV", "r": 158, "c": 4, "i": null, "j": null, "ref": "17"}
- {"pair": "3fd27b9f51571044", "pdf": "H-S-I-Billing-Inv-056-04-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "SOV", "r": 159, "c": 4, "i": null, "j": null, "ref": "17"}
- {"pair": "3fd27b9f51571044", "pdf": "H-S-I-Billing-Inv-056-04-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "SOV", "r": 160, "c": 4, "i": null, "j": null, "ref": "31"}
- {"pair": "3fd27b9f51571044", "pdf": "H-S-I-Billing-Inv-056-04-20-23-r0.pdf", "page": 11, "table": 0, "sheet": "SOV", "r": 161, "c": 4, "i": null, "j": null, "ref": "35"}

### missing_row/in_table_elsewhere

- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 13, "c": 14, "i": null, "j": null, "ref": " $-   "}
- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 13, "c": 16, "i": null, "j": null, "ref": " $-   "}
- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 14, "c": 14, "i": null, "j": null, "ref": " $-   "}
- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 14, "c": 16, "i": null, "j": null, "ref": " $-   "}
- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 1, "table": 0, "sheet": "SOV", "r": 15, "c": 14, "i": null, "j": null, "ref": " $-   "}

### value_mismatch/reader_has_more

- {"pair": "4fc27f0d7d3b36d0", "pdf": "Steamer-Dock-Pay-Application-2-August-2019-SOV.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 12, "c": 3, "i": 6, "j": 3, "ref": "0", "got": "6743497-14406 Vendor No: 73845"}
- {"pair": "7699bd26d4dd0f38", "pdf": "TGG-OCTOBER-2018-R0-billing.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 7, "c": 2, "i": 2, "j": 1, "ref": "0", "got": "Contractor: Summit-Basalt LLC, A Joint Venture • S-B Job No. 6120510"}
- {"pair": "7699bd26d4dd0f38", "pdf": "TGG-OCTOBER-2018-R0-billing.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 12, "c": 3, "i": 6, "j": 2, "ref": "0", "got": "6120510-31252-os Vendor No:"}
- {"pair": "90688e5f5a118a1b", "pdf": "Northwind-Marlow-Billing-Master-Form-NM-March-2021.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 7, "c": 2, "i": 2, "j": 2, "ref": "0", "got": "Contractor: Northwind-Marlow LLC, A Joint Venture • N-M Job No. 6999306"}
- {"pair": "90688e5f5a118a1b", "pdf": "Northwind-Marlow-Billing-Master-Form-NM-March-2021.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 12, "c": 3, "i": 6, "j": 3, "ref": "0", "got": "6999306- Vendor No: 393"}

### outside_table/cell_text_outside

- {"pair": "822f62ef307eea4a", "pdf": "DCW-Cost-Bid-Tab-Comparative-Plaza-Cost-Report-Yesler-11.5.23-WSP.pdf", "page": 2, "got": "November 5, 2022"}

### unverifiable/print_area_overflow

- {"pair": "9680ef1710d3ae16", "pdf": "Jan-SOV-2022.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 8, "c": 4, "i": 4, "j": 5, "got": "Ashby Elevator Co. PO Box 21070, Lynnwood WA 98036"}
- {"pair": "bd816e084eaa0f09", "pdf": "Feb-SOV-2022.pdf", "page": 2, "table": 0, "sheet": "Summary-HCC Only", "r": 8, "c": 5, "i": 4, "j": 7, "got": "Thornton Structures Inc PO Box 41626, Lynnwood WA 98036"}

