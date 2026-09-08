# Local retained-citation census

The currently configured local database contains no retained citation population. This is unavailable evidence for actual deployed/customer history, not a passing historical-compatibility check.

Snapshot: 2026-09-08T08:53:48.657267+00:00. Verified PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) through loopback port 5433, with server-enforced transaction_read_only=on and REPEATABLE READ. Schema, web and worker configurations target the same endpoint. The transaction was rolled back after SELECT-only queries. Role visibility flags and row-security metadata are recorded in receipt.json.

Actual persisted counts are zero for projects, documents, Source Segments, Facts, Fact/Delta decisions, support sources, evidence links, legacy work decisions, reports, release candidates, authorized packages and legacy external releases. The seven direct source-segment reference tables also have zero rows. Explicit decision-to-Fact-source, decision-to-closure-source, Delta-decision-to-support-source, legacy dependency-evidence-to-segment and subject-resolution-decision paths all return zero edges. No source text or customer names were exported.

The data absence is separate from schema staleness: Alembic reports b2d5f8a1c4e7, but all ten native PDF reading/locator columns are absent; the SECURITY DEFINER append_source_segments function does not recognize pdf_cell; and the native PDF whitespace Fact transformation is absent. This local executable schema does not match current native integration capabilities. It was not migrated or repaired.

No source-byte or locator-replay check could be performed because there are no cited rows and no registered documents. Existing corpus files are not a substitute. No remote deployment, customer database, backup, other local database, or actual release artifact population was enumerated. Qualification of retained customer citations therefore remains unproved and requires the specifically identified populated environment and its original evidence.
