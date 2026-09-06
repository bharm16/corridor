# Loop score: loop

Holdout included: False. A pair passes only when every page passes and every reference cell is exact somewhere.

| Measure | Value |
|---|---:|
| Pairs passing | 262 / 263 |
| Pages passing | 1589 / 1589 |
| Reference cells exact | 483210 / 483336 |
| Documents the reader failed on | 0 |

## Failure classes (cells)

| Class | Cells | Pages | Pairs |
|---|---:|---:|---:|
| value_mismatch | 124 | 64 | 39 |
| unverifiable | 29 | 8 | 8 |
| columns_folded | 16 | 6 | 4 |
| prose_row | 15 | 1 | 1 |
| uncovered | 6 |  | 1 |
| merged_rows | 1 | 1 | 1 |

## Subclasses

| Subclass | Cells |
|---|---:|
| value_mismatch/clipped_evidence | 63 |
| unverifiable/print_area_overflow | 29 |
| value_mismatch/overflow_hashes | 22 |
| value_mismatch/clipped_prefix | 20 |
| columns_folded/unmapped_column | 16 |
| prose_row/outside_workbook | 15 |
| value_mismatch/clipped_suffix | 9 |
| value_mismatch/clipped_middle | 8 |
| uncovered/absent | 6 |
| value_mismatch/rounded_to_width | 2 |
| merged_rows/text_intact | 1 |

## By producer family

| Family | Pairs pass | Pages pass | Cells exact |
|---|---:|---:|---:|
| AdobePDFLibrary | 6/6 | 15/15 | 1496/1496 |
| Distiller | 1/1 | 2/2 | 105/105 |
| Excel | 221/221 | 1368/1368 | 468841/468951 |
| Ghostscript | 9/10 | 29/29 | 3844/3856 |
| PrintToPDF | 25/25 | 175/175 | 8924/8928 |

## Worst pairs

| Key | PDF | Family | Pages | Errors | Uncovered |
|---|---|---|---:|---:|---:|
| fcbe0ceb23e61b0f | PE-40_Change-Order-224-Billing-SOV-As-Agreed-3-4-2 | Ghostscript | 1 | 15 | 6 |
| 1ee4a00b8eb6173b | REVISED-McClean-December-SOV.pdf | Excel | 5 | 16 | 0 |
| 68f6277236871887 | H-S-I-Billing-Inv-078-07-20-25-r0.pdf | Excel | 14 | 10 | 0 |
| ed6b83273cf12cf1 | H-S-I-Billing-Inv-067-05-20-24-r2.pdf | Excel | 14 | 9 | 0 |
| e4e7e3b5fa093bc6 | sov.pdf | Excel | 14 | 8 | 0 |
| 642f81cf6857b000 | DCW-Cost-Bid-Tab-Comparative---Plaza-Cost-Report-- | Excel | 6 | 7 | 0 |
| 6f9405fa2ea1b2af | DCW-Cost-Bid-Tab-Comparative---Plaza-Cost-Report-- | Excel | 6 | 7 | 0 |
| 1c1b3b097169c693 | 19132-92---July-2025.pdf | Excel | 14 | 6 | 0 |
| 4d5c3b52fd29dd09 | Valley-19132-95---December-2025.pdf | Excel | 14 | 6 | 0 |
| 737f85b2d5fb9382 | 19132-84-VE-September-2024---Rev1.pdf | Excel | 14 | 6 | 0 |
| d84e75cee9409385 | Copy-of-19132-85-VE-October-2024---R1.pdf | Excel | 14 | 6 | 0 |
| e3db3014b7973d93 | 3.2024-Colman-Dock-Billing.pdf | Excel | 7 | 6 | 0 |
| b62895f03208009f | 85531_1010_Index.pdf | Ghostscript | 10 | 5 | 0 |
| d16164dcb786aa95 | 19132-76-VE-December-2023.pdf | Excel | 14 | 5 | 0 |
| 0aa1faaab782ded0 | 5007_19-03-Billing.pdf | Excel | 3 | 4 | 0 |
| 17e56e5a74783c63 | Copy-of-19132-73-VE-August-2023.pdf | Excel | 13 | 4 | 0 |
| 2449edb4c2805be4 | 5007_19-09-Billing.pdf | Excel | 5 | 4 | 0 |
| 610d4097dfa0a45d | 5007_18-11-Billing.pdf | Excel | 3 | 4 | 0 |
| 9a201e0540236c5b | Dec.-2019.pdf | Excel | 7 | 4 | 0 |
| c15972df8830e9e0 | 5007_19-11-Billing.pdf | Excel | 7 | 4 | 0 |
| d4bd6642b42c4708 | 5007_19-06-Billing.pdf | Excel | 4 | 4 | 0 |
| dd5d52199e872e1d | 5007_18-10-Billing.pdf | Excel | 3 | 4 | 0 |
| 1365981f856ebeb5 | Copy-of-12-20-22-United-PSG---Billing-App-10.pdf | Excel | 4 | 3 | 0 |
| 24442fa4c3bc0d81 | Copy-of-3-18-22-United-PSG-Revised-per-HCC-Comment | Excel | 4 | 3 | 0 |
| 8da10c47cb158949 | Copy-of-United-PSG---Billing-App-13.pdf | Excel | 4 | 3 | 0 |

## Examples

### value_mismatch/clipped_suffix

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 19, "j": 7, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined verison) and as directed by 
- {"pair": "084701d0ea4a9037", "pdf": "19132-61-VE-May-2022.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 28, "j": 7, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined verison) and as directed by H
- {"pair": "0c8e468d6b76883a", "pdf": "19132-64-VE-August-2022.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 19, "j": 7, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined verison) and as directed b
- {"pair": "1c1b3b097169c693", "pdf": "19132-92---July-2025.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 19, "j": 7, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "in Valley CP 064.2 (WSF redlined version) and as directed by H
- {"pair": "22eb479caa26c8dd", "pdf": "7.2023-Billing.pdf", "page": 5, "table": 0, "sheet": "SOV", "r": 121, "c": 7, "i": 21, "j": 7, "ref": "Supply and install Metal Wall Panels systems, Curtain Wall and Glazing systems, ", "got": "Roofing systems and Metal Guardrail systems complete at the new Entry

### value_mismatch/clipped_middle

- {"pair": "02753bf7e6549f5a", "pdf": "19132-71-VE-June-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 7, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to com
- {"pair": "084701d0ea4a9037", "pdf": "19132-61-VE-May-2022.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 1, "j": 7, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to compl
- {"pair": "0c8e468d6b76883a", "pdf": "19132-64-VE-August-2022.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 7, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "electric activtiesw and compllete path work to be able to c
- {"pair": "1c1b3b097169c693", "pdf": "19132-92---July-2025.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 20, "j": 7, "ref": "Provide premium overtime only, including straight OT and double time OT, to acce", "got": "electric activtiesw and complete path work to be able to compl
- {"pair": "d16164dcb786aa95", "pdf": "19132-76-VE-December-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 1, "j": 7, "ref": "Provide premium overtime only, including straight OT and double time OT, to acce", "got": "electric activtiesw and complete path work to be able to c

### unverifiable/print_area_overflow

- {"pair": "0aa1faaab782ded0", "pdf": "5007_19-03-Billing.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": null, "c": null, "i": 0, "j": 1, "got": "ORE FINAL PRINTING enter any digit to remove the green shading in the data entry"}
- {"pair": "0aa1faaab782ded0", "pdf": "5007_19-03-Billing.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 2, "c": 3, "i": 1, "j": 1, "got": "imodal Terminal at Colman Dock Project • Seattle, WA."}
- {"pair": "0aa1faaab782ded0", "pdf": "5007_19-03-Billing.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 3, "c": 3, "i": 2, "j": 1, "got": "-Pacific LLC, A Joint Venture • H-P Job No. 5290015"}
- {"pair": "0aa1faaab782ded0", "pdf": "5007_19-03-Billing.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 4, "c": 7, "i": 3, "j": 5, "got": "erg Co. 15400 SE 30th Pl, Ste 100 Bellevue, WA 98007"}
- {"pair": "2449edb4c2805be4", "pdf": "5007_19-09-Billing.pdf", "page": 2, "table": 0, "sheet": "SOV", "r": 1, "c": 3, "i": 0, "j": 1, "got": "ORE FINAL PRINTING enter any digit to remove the green shading in the data entry"}

### value_mismatch/clipped_evidence

- {"pair": "10036f98f9b22c0d", "pdf": "Copy-of-Copy-of-4.2024-Colman-Dock-Billing-by-GE-050724.pdf", "page": 6, "table": 0, "sheet": "SOV", "r": 147, "c": 5, "i": 8, "j": 5, "ref": "WSF CO 368\nMCA 101", "got": "368\nMCA 101"}
- {"pair": "11e04a9671934fb0", "pdf": "Billing-Review-Comments-Disposition---Estimate-77.pdf", "page": 10, "table": 0, "sheet": "Billing Issues", "r": 84, "c": 10, "i": 7, "j": 10, "ref": "Updated SOV to correct the amount Work Complete Previously billed is acknowledge", "got": "Updated SOV to correct
- {"pair": "1365981f856ebeb5", "pdf": "Copy-of-12-20-22-United-PSG---Billing-App-10.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 105, "c": 10, "i": null, "j": null, "ref": " $-   ", "got": ""}
- {"pair": "1365981f856ebeb5", "pdf": "Copy-of-12-20-22-United-PSG---Billing-App-10.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 105, "c": 14, "i": null, "j": null, "ref": " $-   ", "got": ""}
- {"pair": "1365981f856ebeb5", "pdf": "Copy-of-12-20-22-United-PSG---Billing-App-10.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 105, "c": 16, "i": null, "j": null, "ref": " $-   ", "got": ""}

### value_mismatch/clipped_prefix

- {"pair": "11e04a9671934fb0", "pdf": "Billing-Review-Comments-Disposition---Estimate-77.pdf", "page": 3, "table": 0, "sheet": "Billing Issues", "r": 25, "c": 7, "i": 8, "j": 7, "ref": "Excel row 120, North Trestle – Sediment Capping – Phase 3, incorrectly indicates", "got": "Excel row 120, North Tres
- {"pair": "17e56e5a74783c63", "pdf": "Copy-of-19132-73-VE-August-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 15, "j": 7, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "Provide premium overtime labor only to install elec
- {"pair": "17e56e5a74783c63", "pdf": "Copy-of-19132-73-VE-August-2023.pdf", "page": 12, "table": 0, "sheet": "SOV", "r": 393, "c": 7, "i": 16, "j": 7, "ref": "Provide premiuim overtime only, including straight OT and double time OT, to acc", "got": "Provide premiuim overtime only, including straight 
- {"pair": "17e56e5a74783c63", "pdf": "Copy-of-19132-73-VE-August-2023.pdf", "page": 13, "table": 0, "sheet": "SOV", "r": 417, "c": 7, "i": 10, "j": 7, "ref": "Supply and Install revised skyline wireway for OFCI Turnstile Electrical \nChange", "got": "Supply and Install revised skyline wireway for OFC
- {"pair": "4d5c3b52fd29dd09", "pdf": "Valley-19132-95---December-2025.pdf", "page": 10, "table": 0, "sheet": "SOV", "r": 392, "c": 7, "i": 15, "j": 7, "ref": "Provide premium overtime labor only to install electrical work at TB trestle lev", "got": "Provide premium overtime labor only to install elec

### value_mismatch/overflow_hashes

- {"pair": "1e0e43f52016e8e2", "pdf": "Hoffman-Pacific-Billing-Master-Form-HP-June-2023.pdf", "page": 1, "table": 0, "sheet": "Summary-HCC Only", "r": 5, "c": 16, "i": 1, "j": 8, "ref": "10/20/2021", "got": "#########"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 132, "c": 8, "i": 6, "j": 8, "ref": " $9.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 133, "c": 8, "i": 7, "j": 8, "ref": " $10.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 134, "c": 8, "i": 8, "j": 8, "ref": " $11.00 ", "got": "#"}
- {"pair": "1ee4a00b8eb6173b", "pdf": "REVISED-McClean-December-SOV.pdf", "page": 4, "table": 0, "sheet": "SOV", "r": 135, "c": 8, "i": 9, "j": 8, "ref": " $11.00 ", "got": "#"}

### columns_folded/unmapped_column

- {"pair": "642f81cf6857b000", "pdf": "DCW-Cost-Bid-Tab-Comparative---Plaza-Cost-Report---Yesler-10.14.22-WSF.pdf", "page": 2, "table": 0, "sheet": "Summary", "r": 1, "c": 1, "i": 0, "j": 0, "ref": "DCW Cost Management", "got": "DCW Cost Management"}
- {"pair": "642f81cf6857b000", "pdf": "DCW-Cost-Bid-Tab-Comparative---Plaza-Cost-Report---Yesler-10.14.22-WSF.pdf", "page": 2, "table": 0, "sheet": "Summary", "r": 2, "c": 1, "i": 1, "j": 0, "ref": "Washington State Department of Transportation", "got": "Washington State Department of Transportation"}
- {"pair": "642f81cf6857b000", "pdf": "DCW-Cost-Bid-Tab-Comparative---Plaza-Cost-Report---Yesler-10.14.22-WSF.pdf", "page": 2, "table": 0, "sheet": "Summary", "r": 3, "c": 1, "i": 2, "j": 0, "ref": "Multimodal Terminal at Colman Dock - Yesler Site", "got": "Multimodal Terminal at Colman Dock - Yesler 
- {"pair": "642f81cf6857b000", "pdf": "DCW-Cost-Bid-Tab-Comparative---Plaza-Cost-Report---Yesler-10.14.22-WSF.pdf", "page": 3, "table": 0, "sheet": "Summary", "r": 1, "c": 1, "i": 0, "j": 0, "ref": "DCW Cost Management", "got": "DCW Cost Management"}
- {"pair": "642f81cf6857b000", "pdf": "DCW-Cost-Bid-Tab-Comparative---Plaza-Cost-Report---Yesler-10.14.22-WSF.pdf", "page": 3, "table": 0, "sheet": "Summary", "r": 1, "c": 7, "i": 0, "j": 8, "ref": "Cost Estimate      October 14th, 2022   ", "got": "Cost Estimate October 14th, 2022"}

### value_mismatch/rounded_to_width

- {"pair": "68f6277236871887", "pdf": "H-S-I-Billing-Inv-078-07-20-25-r0.pdf", "page": 14, "table": 0, "sheet": "Actng Only", "r": 327, "c": 12, "i": 9, "j": 8, "ref": "26945.2854", "got": "26945.29"}
- {"pair": "ed6b83273cf12cf1", "pdf": "H-S-I-Billing-Inv-067-05-20-24-r2.pdf", "page": 14, "table": 0, "sheet": "Actng Only", "r": 327, "c": 12, "i": 9, "j": 8, "ref": "26945.2854", "got": "26945.29"}

### merged_rows/text_intact

- {"pair": "cf36a6eaefa412e8", "pdf": "Billing-Review-Comments-Disposition---Estimate-73.pdf", "page": 9, "table": 0, "sheet": "Billing Issues", "r": 59, "c": 8, "i": 4, "j": 8, "ref": "Please see attached PE #73R2", "got": "Please see attached PE #73R2\nPlease see attached PE #73R2", "rows": [59, 60]

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

