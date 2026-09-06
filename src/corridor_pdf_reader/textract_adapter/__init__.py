"""Corridor's Textract adapter: the measured rung behind one outbound boundary (#732, ADR-0094).

The measured code is `corridor_pdf_reader.textract`, imported unchanged from
commit c39363e. This package is what Corridor adds around it, and it exists
because the rung as measured answers "what does Textract say about this
raster" and nothing else: its client caches by the PNG's digest alone, calls
under whatever AWS profile it is handed, and knows nothing of customers,
purposes or stages. Three things had to be true before Corridor could hold
it, and each is a module here:

- `records`: the provider posture (one record per provider and operation,
  bound to `docs/operations/textract-provider-posture.md` by digest), the
  customer authorization (#522's signed instance, as the adapter reads it),
  the experiment scope for public or synthetic data, and the request
  boundary they are matched against, field by field.
- `identity`: what a cached response is identified by. A raster digest is
  not enough: the authorization boundary, the operation and feature set, the
  region, the request and preprocessing configuration and the adapter version
  make the scope, and every entry retains the reported model version, the
  request identity, the raw-response digest and the normalized-reading
  digest. The same module owns the normalization, so the digest and the
  reading cannot drift apart.
- `boundary`: the only module that constructs the imported client, and it
  does so only after the authorization matched on every field. A missing or
  mismatched record is a Processing Failure with the reason and zero
  outbound requests. The boundary counts calls, retries and hits separately
  into a per-Extraction-Run cost receipt, and binds every rendition and page
  to the response it used, so one raster shared by two renditions is two
  bindings and one charge.

`rendering` puts the rung's rasterizer and the reader's glyphs under the
PDFium execution contract of `corridor_pdf_reader.execution`, and `replay`
replays retained responses through the normalizer and writes the receipt
that says they still read the same. Run-mate rescue and the polygon margin
stay off by default, as measured.

What was tried first and rejected: routing as the only guard (diagnostics,
retries, shadow runs or a future caller could call the adapter directly;
ADR-0094 puts the check in the adapter for that reason), and editing the
imported client to carry the wider cache key (the parity test would have
failed, and rightly: the measured code stays byte-identical, and the scope
directory is the wrapper's, not the client's).

No production module imports this package. It is built and tested on
retained responses only; no live call is made anywhere in Corridor, and
#739 owns the caller.
"""
