# Matrix row accounting

The current spreadsheet reader (`sheet_native_v2`) and page reader
(`matrix_tiered_v4`) seal one `matrix-row-accounting-v1` object on every
completed Extraction Run.

The receipt names every detected data-row identity and records one disposition:

- `extracted` — a Candidate was recorded;
- `blank` — the reader detected an empty source row; or
- `skipped` — a versioned rule rejected the row, with reasons such as
  `retired_row`, `insufficient_mapped_fields`, or `missing_required_fields`.

The detected count must equal the extracted, blank, and skipped total. A gap
fails the entire attempt, rolls back partial Candidates, and retains the exact
unaccounted row identities on the failed Extraction Run. It is never reported
as a successful zero-row reading.

Historical `sheet_native_v1` and `matrix_tiered_v3` runs remain unchanged with
null accounting. The version bump prevents those readings from being pooled
with the accounted readers. A populated accounting receipt is immutable and
prevents destructive migration downgrade.
