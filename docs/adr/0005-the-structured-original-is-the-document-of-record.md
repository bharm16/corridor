---
status: accepted
---

# The structured original is the Document of Record, not its printed rendering

> **Terminology amendment, 2026-08-27 — [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Supporting Documentation replaces Evidence in domain explanations. Source column names and stored identifiers remain unchanged.

TxDOT's Utility Conflict Matrix is a spreadsheet. The I-35 NEX South RID publishes it as one: 24 named columns, a `Field_Column Descriptions` sheet defining every field, a `Drop-Down Lists` sheet enumerating the valid values, and three merged cells in the whole workbook. It carries columns no PDF matrix in this corpus has — `Utility Company Contact`, `Estimated Resolution Date`, `Resolution Status`, test hole number and depth — and it keeps start and end stationing and offset in four separate columns.

The PDFs are printouts of that form. Every defect in ADR-0004 is print damage: separators lost between text spans, cells clipped at their boundaries, four columns collapsed into `Start Station, Offset`, an owner pushed out of the table into a page header. The data was structured before someone exported it.

So where a document exists in both forms, the structured original is the **Document of Record** and Supporting Documentation cites it. Spreadsheets are read natively as rows — no model, exact values, and the data dictionary and controlled vocabularies come along as sources for the domain language. Running a vision model over a spreadsheet would be recovering from pixels what the file already states.

This does not narrow ADR-0004. Project A publishes no spreadsheet at all — its utilities archive holds 21 members and zero — and Project C publishes only SUE test-hole tables, no UCM. The PDFs really are all there is for both, so vision extraction is necessary rather than self-inflicted.

## Consequences

Ingestion stops being PDF-only. At the time of this decision, `doc_pages`, Supporting Documentation, and the queue's source pane all assumed a page; a sheet has none, and needs a rendering a reviewer can check a quote against.

Citations against a spreadsheet verify **exactly** rather than fuzzily, because the text being matched was generated from the cells rather than recovered from a layout. The 0.9 threshold exists for print damage that does not occur here.

The form's data dictionary states that `Utility Conflict ID` is *"unique within the transportation project."* Real documents disagree — one NHHIP revision reuses 47 ids, and two distinct Comcast conflicts share `FOC14-69`. The intent is worth recording precisely because the data does not honour it: nothing may assume uniqueness.

A stale spreadsheet must not outrank a newer PDF. Precedence is by format only where both render the same document; supersession by date still wins, and the two rules have to be applied in that order.
