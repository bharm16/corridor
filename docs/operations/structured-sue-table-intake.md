# Structured SH 99 SUE table intake

The SH 99 archive registers two dated SUE evidence tables from its retained
archive directory:

- `SH99_PROBE_DEPTH_SUMMARY_TABLE_1-15-2025.xlsx` is read directly.
- `SH99_TEST_HOLE_INDEX_1-15-2025.xls` is retained byte-for-byte. Corridor
  derives a deterministic `.xlsx` so the cell reader can read it.

Both curated manifest entries remain `curation_status: proposed`. Fetch may
retain and verify their bytes, but normal ingest skips them. A maintainer must
change the reviewed manifest entries to `confirmed` before ordinary project
ingest can materialize them. Tests use the internal `include_proposed` seam only
inside rollback-scoped rehearsals; there is no CLI flag that bypasses curation.

The conversion receipt binds the original and derived Document IDs and hashes,
the source and derived formats, and the converter/version. It asserts only a
format derivation. It does not assert that the two files are independent
Evidence, infer equivalence, or register Supersession.

Both readable workbooks use the native spreadsheet reader with zero model
tokens. Their rows become `Candidate(kind="evidence")` technical proposals,
not proposed Constraints or External Party Statements. Dependency and event
admission continue to reject or ignore this kind. Each proposal keeps the
exact sheet page, table-row number, whole-row quote, verified citation, mapped
source fields, and every unmapped heading/value. The existing technical `tier`
key retains the table schema identity; unknown values use
`unmapped:<exact heading>` fields without changing the shared Candidate payload
shape.

Only headings in the two exact registered schemas are mapped. `PD`, `DOC`, and
the test-hole index's blank offset-side heading remain reported and their cell
values remain under exact `unmapped:` field names; no acronym or blank heading
is guessed.

Fetch skips the derived rendition only when the original hash, converter name,
converter version, and stored output still match. Ingest reuses both Documents
and the derivation receipt by exact identity. Re-running either operation does
not replace a source, create another derivation, or displace a decision.
