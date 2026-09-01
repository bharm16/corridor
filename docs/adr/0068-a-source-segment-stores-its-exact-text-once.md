---
status: accepted
domain: extraction
scope: current product
amended_by:
  - ADR-0079
---

# A source segment stores its exact text once

Today a cited quote physically exists in at least four tables (candidate payload, run snapshot, `evidence_links.quote`, `accepted_facts_json`), and page text lives as one page-wide string (`doc_pages.text`) with a `text_source` winner. The storage research introduces `source_segments` as the single evidence layer: one row per addressable piece of a source — a spreadsheet cell, a PDF cell, a sentence or paragraph, an agenda item, an email body span, an agreement clause, a date-table row, the exact words of a Recorded Verbal Statement.

Decision: **a segment stores its own exact text or value, once, with a digest — and a typed locator addresses it in the original bytes.**

- The segment row carries: parent Document Rendition, segment kind, the exact text/value, a content digest, ordering, and a typed locator (sheet + cell range; page + polygon + token ids; MIME part + offsets; page + clause span).
- Facts (ADR-0067), proposals, decisions, and released citations reference segment ids and never copy segment text.
- Verification runs through the locator against the content-addressed original bytes (`corpus/files/<sha256>` — the one boundary the current design already gets right). The segment's text is the working copy; the bytes are the proof.
- Segmenters emit non-overlapping segments per kind. A fact needing several pieces references several segments (ADR-0069); nobody mints a jumbo segment to avoid a join.

## Considered options

**Store only offsets into canonical page text (the Document AI shape).** Rejected for three reasons. Half the segment kinds are not spans of linear text — a workbook cell, a drawing callout, a schedule-table row have no meaningful offset into page prose. Regenerated page text (a different OCR pass, a different extraction configuration) silently invalidates offsets. And the retention model (ADR-0072) expires full page text and renders as rebuildable intermediary data — possible only if the cited segment owns its text; offsets would make the page blob permanently reachable evidence.

**Keep copying quotes into each consumer.** Rejected: that is the present design, and it is why one quote lives in four tables.

## Consequences

`DocPage.text` becomes a rebuildable processing artifact rather than the owner of cited text; the OCR-versus-native-token layering from the PDF extraction research lands beneath this contract (tokens and renders are intermediary; cited segments are durable). `EvidenceLink.quote` is superseded for new writes by segment references. Both major document-AI vendors converge on this addressing scheme (stable block/segment id + page + geometry + text), which is independent confirmation of the shape, not the reason for it.
