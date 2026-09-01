---
status: accepted
domain: extraction
scope: current product
amended_by:
  - ADR-0079
---

# Token layers are Class B artifacts with PostgreSQL manifests

Ingestion produced one page-wide `DocPage.text` string per page by concatenating native text and per-region OCR text (`ingest.py` `_extract_pages`). That single winner discards coordinates, cannot separate a trustworthy native reading from a suspect OCR one, and — per the 2026-08-30 extraction-architecture research (§D, §F) — makes cell-level fusion impossible: "more text is not better text." Ticket #442 replaces the winner with two coordinate-bearing token layers, kept separately, native and OCR.

The open implementation choice the ticket names is **where the tokens live**: columnar Parquet artifacts in the content-addressed store with PostgreSQL manifest rows, or partitioned TTL tables — "decide from the dev corpus and record the decision."

## Measurement

Measured across the entire registered dev corpus (`corpus/files`, PyMuPDF native words):

- 195 PDF documents, 1,369 pages, 318,105 native word tokens.
- Per page: mean 232, median 203, p95 770, max 1,295 tokens.
- With OCR roughly doubling on image/mixed pages, ~636,000 token rows total — about 76 MB of PostgreSQL row storage for the whole corpus, and TTL-eligible (Class B, 30/90-day expiry per ADR-0072).

The volume is small. Columnar compression pays for itself at orders of magnitude more data than this; a per-page median of ~200 tokens is a trivial JSON document.

## Decision

**One canonical token-layer artifact per (document, page, origin) in the content-addressed store, with a PostgreSQL manifest row, registered through the Class B `ProcessingArtifact` seam (ADR-0072).** Manifests live in PostgreSQL either way, as the ticket requires.

- The artifact is a canonical JSON serialization of the layer's tokens — not Parquet. At this measured volume the columnar format's only advantage (scan compression) is unwarranted, and it would add a `pyarrow` dependency for no measured benefit.
- The manifest row (`token_layers`) records engine/configuration identity, page, origin, token count, a quality summary, the artifact digest and byte size, and the retention class — mirroring `page_render_derivatives` (ADR-0072's manifest pattern).
- Persistence registers the artifact through `register_processing_artifact(kind="token_layer")` exactly as renders do, so the existing retention TTL, reachability check, holds, and dry-run manifest apply unchanged. A new `kind` is added to `ck_processing_artifact_kind`; no new retention machinery is written.

Rejected — **partitioned TTL tables in PostgreSQL**: viable at this volume but would duplicate the retention planner (a bespoke row-family delete path) that the artifact seam already provides for free, and keeps high-churn intermediary rows in the primary store rather than the CAS where every other intermediary already lives.

Rejected — **Parquet-in-CAS**: the same artifact+manifest shape this decision adopts, but with a columnar encoder and a new dependency justified only by volume the corpus does not have. If a future corpus makes token volume large, the artifact encoding can change behind the manifest without touching readers.

## Consequences

Durable citation never depends on a token row (ADR-0068): a cited reading is promoted into a `source_segment` carrying its own exact text and digest, so deleting an expired token layer leaves every promoted segment fully usable and verifiable. Token layers and any derived table hypotheses are Class B; only what is promoted to a segment is Class A. `DocPage.text` stops being a native-vs-OCR winner and becomes a rebuildable projection over the token layers (Class C) kept only for existing readers; geometry-consuming extraction reads the native token layer directly.
