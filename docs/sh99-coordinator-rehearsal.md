# SH 99 bounded coordinator rehearsal

This command proves the assembled coordinator outcome on a disposable PostgreSQL
clone of the approved shared SH 99 state. It does not mutate the source database.
It first verifies the closed shared mechanical-Admission receipt and pins the current source
revision, data-only source dump, corpus document digests, Active Runs, policy runs,
seeded coordinator, and the three scenario Candidate identities.

Run it from a clean checkout, writing to a new directory:

```bash
make sh99-coordinator-rehearsal ARGS="replay \
  --project-slug=sh99-grand-parkway \
  --source-database-url=postgresql+psycopg://corridor:corridor@localhost:5433/corridor \
  --postgres-admin-url=postgresql+psycopg://corridor:corridor@localhost:5433/corridor \
  --expected-clean-git-revision=<git-rev-parse-HEAD> \
  --shared-admission-receipt-path=<validation-passed.json> \
  --expected-shared-admission-receipt-sha256=<shared-admission-receipt-sha256> \
  --approved-shared-state-receipt=<immutable-approval-or-audit-url> \
  --shared-backfill-elapsed-seconds=291 \
  --output-dir=/tmp/sh99-coordinator-rehearsal"
```

The timed segment opens the coordinator home, follows the existing statement
screens, records the Kinder Morgan date change and Equistar month commitment, then
uses the current Report render and release controls. Verification reads the clone
only after the clock stops and checks the two statements, Candidate 7587's existing
Admission Abstention, party-level past due, unknown scope, fixed-PDF receipt, and
the existing selected/all-active scope test.

The current product has three gaps that the bundle records: the coordinator home does
not distinguish a required statement among its work-list cards; the statement screen
does not expose the supporting Evidence page text; and release requires a raw artifact
identity that has no coordinator screen. Therefore the current replay is a sealed
**failed** internal rehearsal, not customer-usability validation.

Verify the closed bundle later without opening PostgreSQL:

```bash
make sh99-coordinator-rehearsal ARGS="verify /tmp/sh99-coordinator-rehearsal \
  --expected-manifest-sha256=<printed-integrity-manifest-sha256>"
```
