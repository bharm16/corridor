---
status: accepted
domain: project-record
scope: current product
amended_by:
  - ADR-0076
  - ADR-0092
---

# The record has one time axis, and the current view projects effective decisions

The Project Record must answer "what does the record say now" and "what did the record say as of revision X" without copying itself. Today it answers by copying: `report_runs.snapshot_json` and `scheduled_report_publications.snapshot_json` each hold a full per-dependency snapshot, and `FrozenProjectReading` exists only in memory. The 2026-08-30 research settled the history model.

Decision: **facts are never retracted; effectiveness lives on decisions; recorded time is the only time axis; the current record is a plain view.**

- **Facts append and stand.** A fact is an observation of what a source said; a wrong reading gets a successor fact and a disposition, and the original stays true as a statement about the extraction.
- **Decisions carry effectiveness.** Each record-affecting typed decision has decided-at, its revision, and a superseded-by reference (null while effective). A partial unique index — one effective decision per subject and fact type where superseded-by is null — enforces the single-current-value invariant at the database for single-valued types; set-valued types omit the index by contract. Supersession is carried by this constraint, not by walking predecessor pointers; the pointer survives as a provenance edge only. This is the standard resolution in the temporal-database literature (SQL:2011's `WITHOUT OVERLAPS`, Postgres exclusion constraints): a predecessor chain needs recursion to answer "current," an indexed predicate needs one scan.
- **One revision row per atomic change.** `project_record_revisions`: predecessor revision, command type, human principal XOR released policy, idempotency key. "As of revision X" = decisions made at ≤ X and not superseded at ≤ X.
- **One time axis.** Document dates, statement dates, and Promised For values are typed fact values — ordering inputs to current-value rules, not a second query dimension. Reports, checks, and Derivations bind to revisions; a Derivation takes a calendar date as an input parameter. A correction never rewrites a past revision — it creates a new one, and a released report stays bound to the revision it cited (ADR-0040). Bitemporality is adopted only if the domain ever genuinely asks "what was true in the world on date X regardless of when we learned it" — Fowler's warning taken at face value: it complicates a system significantly and the domain has no question for it.
- **The current view is a plain PostgreSQL view.** At this scale, "latest effective decision per subject" over a partial index is an index scan; no primary source publishes a row count that forces materialization. The sanctioned escalation path, only when measured slow: plain view → materialized view refreshed concurrently → a transactionally maintained projection table. Whatever exists stays disposable, rebuilt only by its projector, and never becomes authority — a release refuses a cache whose revision differs from the requested one.

## Considered options

**Keep per-report snapshots.** Rejected for new writes: a report binding a revision id cites the same reading a snapshot copies, without the copy. Existing snapshot rows remain readable; history is not rewritten.

**Bitemporal from the start.** Rejected; see above. The one scenario that would justify it — a correction that must be retroactively effective in a Derivation — does not exist in the domain's rules, and adding the axis later is possible precisely because facts are never destroyed.

## Consequences

The equivalence gate is behavioral, not a data migration: `FrozenProjectReading` is an in-memory dataclass (`project_reading.py:28`), so proving the view correct means the four reader surfaces — Coordination Report, briefing, workbook export, release — produce identical readings from the view. New Report Runs bind revision ids instead of writing `snapshot_json`.
