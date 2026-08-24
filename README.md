# corridor

A cited system of record for external-party readiness on highway projects. Every factual assertion it makes traces to a quote on a page of a source document.

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

## Documents

| | |
|---|---|
| [`CONTEXT-MAP.md`](CONTEXT-MAP.md) | Domain contexts and their canonical vocabulary. |
| [`v0-build-spec.md`](v0-build-spec.md) | Implementation detail for M0-M5 |
| [`corpus-acquisition-spec.md`](corpus-acquisition-spec.md) | Document assembly; runs in parallel from week 0 |
| [`phase-1-roadmap.md`](phase-1-roadmap.md) | Milestones and sequencing rules |
| [`docs/adr/`](docs/adr) | Decisions with lasting consequences |
