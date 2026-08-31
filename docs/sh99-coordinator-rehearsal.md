# SH 99 bounded coordinator rehearsal

This command proves the assembled coordinator outcome on a disposable PostgreSQL
clone of the approved shared SH 99 state. It never migrates or mutates the source
database. It verifies the closed shared mechanical-Admission receipt and pins the
clean source revision, shared predecessor migration, data-only dump, corpus
documents, Active Runs, policies, Report ruleset/provenance mode, coordinator, and
all three scenario Candidates. Each scenario receipt includes the exact document
name/id/SHA, source date, registered page, Candidate quote, text source, page-text
SHA, and rendered PNG SHA/byte count.

Run from a clean checkout at the repository root so relative
`out/page-images/...` assets resolve to the pinned images named by the database.
Write to a new output directory:

```bash
make sh99-coordinator-rehearsal ARGS="replay \
  --project-slug=sh99-grand-parkway \
  --source-database-url=postgresql+psycopg://corridor:corridor@localhost:5433/corridor \
  --postgres-admin-url=postgresql+psycopg://corridor:corridor@localhost:5433/corridor \
  --expected-clean-git-revision=<git-rev-parse-HEAD> \
  --expected-source-migration-head=<released-head> \
  --expected-target-migration-head=<direct-successor-head> \
  --shared-admission-receipt-path=<validation-passed.json> \
  --expected-shared-admission-receipt-sha256=<shared-admission-receipt-sha256> \
  --approved-shared-state-receipt=<immutable-approval-or-audit-url> \
  --shared-backfill-elapsed-seconds=291 \
  --output-dir=/tmp/sh99-coordinator-rehearsal-265-<revision>"
```

The command provisions the clone at `--expected-source-migration-head`, restores
and compares the data there, upgrades only that clone to
`--expected-target-migration-head`, and compares domain and scenario state again.
Scope coverage runs against the still-live clone with its `DATABASE_URL` explicitly
set. After clone disposal, the command re-reads the shared migration head, domain
state, and scenario receipts.

The timed segment matches immediate cards by visible party, exact wording,
document, and source date. External Party selects prefer an exact normalized visible
label and otherwise accept only one whole-prefix canonical expansion; all other
selects remain exact-only. The flow selects an enabled registered Evidence block
through its browser-carried ordinal and fetches the ordinary PNG URL. It saves the
Kinder Morgan date change and Equistar month commitment, renders and reviews one
fixed PDF, follows the bound release form, retrieves the released bytes, and reloads
immutable history. The second timer stops only after retrieval and history readback.

Post-run verification checks both source-preserving Kinder Morgan timings, exact
Milestone Impact, Equistar's January month boundary, unknown scope, the frozen
coordinator actor/display, every open unknown-scope Report statement, and Candidate
7587's absence from any Commitment, timing, Ledger-derived Work Item, past-due
Derivation, Report context, or released PDF. Candidate 7587 remains a lawful pending
source Candidate and may still appear in the searchable Candidate backlog.

A released run copies the exact fixed PDF into `released-report.pdf` with its frozen
release receipt. If later semantic verification fails, already-reviewed and released
bytes remain in an honest failed bundle. A run is `passed` only when both timings
meet the unchanged 5/3-minute bounds; assistance, errors, deviations, and retries
are empty; the source is unchanged; exact release evidence exists; and all post-run
verification succeeds. This is an internal workflow rehearsal with provisional
targets, not customer-usability validation.

New publications use bundle schema v3. Database-free verification dispatches by
manifest schema and retains the frozen v2 reader so the immutable #256 failed bundle
continues to verify without redefining its four-file contract.

Verify a closed bundle without opening PostgreSQL:

```bash
make sh99-coordinator-rehearsal ARGS="verify /tmp/sh99-coordinator-rehearsal-265-<revision> \
  --expected-manifest-sha256=<printed-integrity-manifest-sha256>"
```
