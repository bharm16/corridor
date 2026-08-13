# SH 99 bounded coordinator rehearsal

This command proves the assembled coordinator outcome on a disposable PostgreSQL
clone of the approved shared SH 99 state. It does not mutate the source database.
It first verifies the closed shared mechanical-Admission receipt and pins the current source
revision, data-only source dump, corpus document digests, Active Runs, policy runs,
the Report ruleset/provenance mode, seeded coordinator, and the three scenario
Candidate identities. It also records the runtime/database environment, retries,
assistance, errors, and explicit deviations from an unassisted coordinator run.

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
only after the clock stops and checks both source-preserving Kinder Morgan timings,
the exact Milestone Impact plan, Equistar's January month boundary (not past due on
January 31 and past due after it), unknown scope, Candidate 7587's absence from
work/report output, and every open unknown-scope Report statement. A released run
copies the exact fixed PDF into `released-report.pdf`, alongside its sealed receipt;
the database-free verifier checks its bytes and SHA-256. A failed run that never
releases a PDF retains an empty file and cannot claim a release.

The current replay records the observed coordinator-home gap: it does not distinguish
the required statement among its work-list cards. If a later run reaches them, the
bundle will also record any Evidence-page or raw-artifact-identity assistance rather
than calling the run a pass. The current result is therefore a sealed **failed**
internal rehearsal, not customer-usability validation.

Verify the closed bundle later without opening PostgreSQL:

```bash
make sh99-coordinator-rehearsal ARGS="verify /tmp/sh99-coordinator-rehearsal \
  --expected-manifest-sha256=<printed-integrity-manifest-sha256>"
```
