# corridor

Tracks construction constraints and coordination decisions on highway projects.
Documented facts link to exact source passages. Statements recorded from
conversations and project decisions retain the named person and date.

## Boot

```
make boot
```

Syncs dependencies with `uv`, brings up Postgres 16 in Docker, and applies migrations. Requires `uv` and a running Docker daemon.

```
make test    # pytest
make psql    # psql shell into the container
make down    # stop the stack
```

Postgres listens on **5433** on the host, not 5432 — see the comment in `docker-compose.yml`.

## Terminology and retained commands

The [context map](CONTEXT-MAP.md) leads to the current construction and
Operations glossaries. Commands retain their existing names so scripts and
saved receipts remain compatible:

| Existing command | Current meaning |
|---|---|
| `make queue` | Coordination work and Human Record Decisions. |
| `make admission` | Record Inclusion: add eligible Extracted Proposals to the Project Record under the applicable rules. |
| `make active-run` | Explicitly select a Document's Current Production Run. |
| `make carry-forward` | Automatic Support Update: update supporting documentation without changing the recorded conclusion. |
| `make milestones` | Import key dates from a schedule. |
| `make exceptions` | Check for Constraint Alerts. |
| `make evidence-investigator` | Run the Statement Review Assistant, which cannot make the project decision. |
| `make product-proving` | Publish or verify a bounded Product Test Run. |

Flags, JSON fields, reason codes, and historical receipts keep their existing
technical names. A changed label does not alter their meaning or grant authority
to make a decision. Documentation Review does not authorize construction or
establish Contract Acceptance.

## Documents

| | |
|---|---|
| [`CONTEXT-MAP.md`](CONTEXT-MAP.md) | Domain contexts and their canonical vocabulary. |
| [`v0-build-spec.md`](v0-build-spec.md) | Implementation detail for M0-M5 |
| [`corpus-acquisition-spec.md`](corpus-acquisition-spec.md) | Document assembly; runs in parallel from week 0 |
| [`phase-1-roadmap.md`](phase-1-roadmap.md) | Delivery stages and sequencing rules |
| [`docs/adr/`](docs/adr) | Decisions with lasting consequences |
