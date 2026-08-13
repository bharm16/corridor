# SH 99 mechanical Admission acceptance

This is the operations proof for the mechanical Admission portion of the SH 99
rehearsal in [the rehearsal contract](sh99-date-rehearsal.md). It captures a
read-only, data-only PostgreSQL snapshot of the real SH 99 source database,
checks that source against the checked-out migration head, restores the snapshot
into a fresh PostgreSQL 16 database, and drops that database on exit. The shared
database is never mutated.

The replay runs the exact shared-operation path twice:

```bash
make admission ARGS="load sh99-grand-parkway"
```

Its only change is `DATABASE_URL`, which points at the disposable clone. This
means the rehearsal covers Active Run declaration, Dependency Admission, and
Event Admission over the real pinned Candidate population rather than a
synthetic event fixture.

Run it from a clean checkout, writing to a new directory:

```bash
make sh99-admission-acceptance ARGS="replay \
  --project-slug=sh99-grand-parkway \
  --source-database-url=postgresql+psycopg://corridor:corridor@localhost:5433/corridor \
  --expected-clean-git-revision=<git-rev-parse-HEAD> \
  --output-dir=/tmp/sh99-real-admission-acceptance \
  --postgres-admin-url=postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
```

The result names an integrity-manifest digest. A reviewer who has that digest
can verify the closed export without opening PostgreSQL:

```bash
make sh99-admission-acceptance ARGS="verify /tmp/sh99-real-admission-acceptance \
  --expected-manifest-sha256=<printed-integrity-manifest-sha256>"
```

`receipt.json` pins the source checkout, source migration head, source database
dump digest, exact pre-operation project rows, both exact commands, exact
after-operation rows, and the isolated late-refusal result. It fails unless
Candidate 7587 records an Admission Abstention, Candidates 7129 and 7296 remain
pending, those three Candidates create no statement, attribution, scope, timing,
commitment-date, or past-due fact, and the second replay adds no Dependency,
statement, scope, projection, or admission-audit fact.

The receipt proves a bounded isolated operation only. It is not approval to run
the shared operation; a designated human must still record that approval in #250.
