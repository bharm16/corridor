# SH 99 mechanical Admission acceptance

This is the operations proof for the mechanical Admission portion of the SH 99
rehearsal in [the rehearsal contract](sh99-date-rehearsal.md). It is deliberately
separate from any shared-database backfill: the runner creates a new PostgreSQL 16
database, migrates it from the checked-out source, rehydrates the digest-pinned
bounded SH 99 snapshot, then drops that database on exit.

The pinned fixture is
`tests/fixtures/sh99_admission_acceptance/v1/snapshot.json`; its SHA-256 is
`f7e1452da8abf1b05a58e68b838cca9f42937744649eb18547a9b0421d5b7365`.
It records the source revision, bounded database snapshot identity, corpus input
digest, declared Active Run, event-policy identity, and abstention reason
vocabulary. It is a receipt fixture, not a claim that the shared SH 99 database
has been modified or that its full candidate backlog was copied.

Run the replay against the local PostgreSQL 16 admin connection, writing into a
new directory:

```bash
make sh99-admission-acceptance ARGS="replay \
  --snapshot=tests/fixtures/sh99_admission_acceptance/v1/snapshot.json \
  --expected-snapshot-sha256=f7e1452da8abf1b05a58e68b838cca9f42937744649eb18547a9b0421d5b7365 \
  --expected-clean-git-revision=<git-rev-parse-HEAD> \
  --output-dir=/tmp/sh99-admission-acceptance \
  --postgres-admin-url=postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
```

The result names the integrity-manifest digest. A reviewer who has that digest
can verify the closed export without opening PostgreSQL or the source snapshot:

```bash
make sh99-admission-acceptance ARGS="verify /tmp/sh99-admission-acceptance \
  --expected-manifest-sha256=<printed-integrity-manifest-sha256>"
```

`receipt.json` records exact before-and-after Ledger rows, Candidate states,
Policy Runs, per-Candidate outcomes and reasons, created identities, a repeated
run with no duplicate Ledger facts, and the forced late-refusal result. In the
pinned cohort Candidate 7587 abstains because its citation never names an Air
Products speaker; Candidates 7129 and 7296 remain pending because their scope
cannot be mechanically proved. The only admitted control is an independently
attributable, single-Dependency commitment.

The final receipt always states the remaining gate: a designated human must
separately approve any shared SH 99 Admission operation. This runner never grants
that approval and never connects to or mutates a shared SH 99 database.
