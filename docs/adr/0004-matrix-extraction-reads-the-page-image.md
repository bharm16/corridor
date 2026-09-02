---
status: accepted
domain: extraction
scope: current product
amended_by:
  - ADR-0006
---

# Matrix extraction reads the page image, not the table structure

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Amended by ADR-0006: the validation gate this ADR called for was run, and what the model is trusted to produce narrowed from transcribed values to page structure. The reasoning below stands.

The expiry date this ADR set on the deterministic parser has passed. #63 deleted the half that mapped column headings by synonym; the half that reads cells off word boxes survives as `corridor.geometry`, and Tier 1 depends on it for every value it stores.

`../history/v0-build-spec-2026-07.md` §"Per document type" specified deterministic table extraction with synonym-mapped headers for conflict matrices — the only extractor in the system without a model — and justified it by layout variance: *"there is no single layout even within one project's own revisions."* That premise argues for the opposite conclusion. A synonym-mapped header table is precisely what breaks on layout variance; the spec's own governing rule six lines earlier warns that *"an extractor that assumes its first sample's shape fails silently on the rest of the same series."*

It did, four times, and every one was silent:

- text spans joined without a separator, so `City of Houston` became `Cityof Houston` and split one utility owner into two across 154 rows
- cells clipped and bled at their boundaries — `Rothwell Street` read as `othwell Street`, `Canal Street` read as `Canal Street o` where the `o` began the next column
- one revision heading its columns `Start Station, Offset`, leaving 584 rows of offsets unread inside the station field since M1
- FDOT SR 789 extracting zero rows, because its owner is stated in the page header and `find_tables()` returns only the table

That last one is the decisive case. No synonym could have reached it. The information a row needs was on the page but outside the table, and a table-shaped reader is structurally blind to it.

Matrices are therefore extracted by a vision model reading the rendered page, which is the only representation where the layout survives intact. Citations get **stronger**, not weaker, because the model now transcribes values rather than reading them:

- extraction runs per page and the page number is ours, so a citation cannot point at a page the model invented
- the quote is verified against the page text, as with every other extractor
- **every token of every stored field value must also appear on the page** — the check that catches `1140+00` where the document says `1149+00`, which a row-level fuzzy quote match at 0.9 never could

The model reads the image; verification runs against the text layer. Two independent representations, so a hallucinated value cannot appear in a stream the model never saw.

## Considered options

**Keep the parser and invest in it** — per-layout profiles, a party sourced from outside the table. Preserves exact values and zero model cost, and the parser is genuinely exact where it works. Rejected because every new agency then costs engineering, and one session produced four silent defects measuring that cost.

**Hybrid: parser first, model on `NoMatrixFound`.** Attractive — free on known layouts, no wall on unknown ones. Rejected as a false economy: the cross-check only agrees where the parser succeeds, which is the lowest-risk case, and is absent exactly on the novel layouts where transcription risk is highest.

## Consequences

Matrix candidates now carry a real `model` and a real confidence, where before both were `None` and `1.0`. Eval history across that boundary is not comparable, which is what those columns exist to make visible.

Field-token verification moves from a test into the pipeline. It is the only mechanical check on field *values*; citation verification has never been one, and that is why the first two defects above survived so long looking fully verified.

The deterministic parser stays in the tree, called only to generate eval baselines, until the vision path has been measured against its 100%-recall result on Project A. It is dead code with an expiry date, tracked for deletion — not kept.
