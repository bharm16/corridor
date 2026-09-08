# Retained-citation inventory for the PDF engine retirement

Environment inspected: **local development database for this checkout**, recorded 2026-09-08T18:50:18.973238+00:00.

Database `corridor` at `172.18.0.2/32:5432`, schema revision `b2d5f8a1c4e7`, read in a read-only REPEATABLE READ transaction that was rolled back.

## Finding

**No affected retained citations in this environment.** Every one of the 7 tables that hold a foreign key into `source_segments` holds zero links, and every one of the 14 retained decision and release tables inspected holds zero rows. There is therefore no persisted citation in this database whose meaning either retired engine could carry.

## What was inspected

Source Segment reference tables, discovered from the live schema rather than listed by hand:

- `evidence_link_sources.source_segment_id`: 0 rows, 0 non-null links
- `fact_closure_sources.source_segment_id`: 0 rows, 0 non-null links
- `fact_sources.source_segment_id`: 0 rows, 0 non-null links
- `recorded_verbal_origin_backfill_receipts.source_segment_id`: 0 rows, 0 non-null links
- `subject_resolution_attempts.source_segment_id`: 0 rows, 0 non-null links
- `subject_resolution_decisions.source_segment_id`: 0 rows, 0 non-null links
- `support_assessment_sources.source_segment_id`: 0 rows, 0 non-null links

Retained decision and release tables:

- `fact_decisions` (an accepted Source Fact decision): 0 rows
- `delta_record_decisions` (a resolved Proposed Delta): 0 rows
- `project_record_revisions` (an atomic accepted-record revision): 0 rows
- `project_baseline_adoptions` (an adopted baseline receipt): 0 rows
- `report_runs` (a report run, which retains the reading it published): 0 rows
- `release_candidates` (a prepared release candidate): 0 rows
- `release_candidate_artifacts` (a candidate's rendered bytes): 0 rows
- `release_packages` (an authorized release package): 0 rows
- `release_package_artifacts` (a released package's rendered bytes): 0 rows
- `external_report_releases` (a legacy external release): 0 rows
- `external_report_artifacts` (a legacy external release's bytes): 0 rows
- `documents` (a registered source document): 0 rows
- `source_segments` (a persisted citation): 0 rows
- `facts` (a captured Source Fact): 0 rows

## Limits of this evidence

- This is one database: the one this checkout is configured against. It is not evidence about a deployed environment, a customer database, a backup, or any other database, and nothing here should be read as proving anything about them.
- A zero-row result is an absence of population, not a passing historical-compatibility check. The question 'can a retained citation survive retirement?' is answered by the seeded contract tests, not by this count.
- No corpus population was reconstructed and offered as customer history. Re-reading corpus files would describe what the reader does now, not which rows exist and what produced them.
- No source text, quoted passage or customer name was read or exported. Digests and identities only.
- This schema is stale for native readings: `rendition_sha256`, `reading_sha256`, `reader_identity`, `location_json`, `span_stream`, `table_index`, `cell_row`, `cell_column`, `row_span`, `column_span` are absent from `source_segments`, so it could not record a native reader identity even if rows existed. Schema staleness is a separate fact from an absence of rows.
