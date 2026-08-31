# corridor

## Commands

```bash
make boot   # uv sync, Postgres 16 in Docker, alembic upgrade head
make check  # fast source and architecture checks
make test-focused ARGS="tests/test_file.py::test_name"  # tight isolated loop
make test   # broad non-slow PostgreSQL gate
make down   # stop the stack
make queue  # run the coordination UI at http://localhost:8412
```

Every entry point is a `make` target, and the Makefile comments say what each
one takes. `make extract`, `make agreements`, and `make minutes` call a model
and need `OPENAI_API_KEY` in `.env`.

## Testing

Use non-overlapping gates appropriate to the exact revision
([ADR-0065](docs/adr/0065-test-gates-preserve-feedback-without-weakening-release-proof.md)):

- During implementation, run `make check` and `make test-focused ARGS="..."`
  for the changed seam. Do not run the broad suite after every edit.
- Use `make test` after a broad change or before pushing when local broad
  feedback is useful.
- Normal PR CI runs `make check`, `make test`, and the non-migration
  `make test-slow` selection. A merge to `main` does not repeat that suite.
- Deliver changes to `main` through a PR; direct pushes have no duplicate
  post-merge test workflow.
- A change to migrations, schema models, or the database test harness also runs
  `make test-migrations`. Ordinary application changes do not.
- `make test-full` is the complete manual and weekly scheduled gate. It is not
  part of ordinary PR or post-merge CI.
- Any source or test change invalidates an earlier result. Rerun the smallest
  affected seam, then the scoped gate for the revised change.

## Architecture

`src/corridor/`, one module per stage:

`corpus` (manifest → files) → `ingest` (pages: text + image) → `extract_*` and
`geometry` (Extracted Proposals) → `adjudicate` (**the only writer of Constraint Records**) →
`ledger`, `exceptions`, `report`, `briefing`, `export` (readers).

Extractors only ever produce Extracted Proposals. Every module opens with a docstring
saying why it exists and what was tried before — read it before changing one.

## Gotchas

- Postgres is on host port **5433**. A Homebrew Postgres 14 takes 5432 and
  accepts the same credentials, so a wrong port connects and fails much later
  on version-specific SQL (`tests/test_boot.py` guards this).
- Tests use real PostgreSQL — no sqlite fallback. Bring the stack up first.
  Parallel `make test`/`make test-full` runs use `tests/conftest.py` to create,
  migrate, and drop one guarded database per xdist worker; serial pytest uses
  the configured development database.
- A database test still defines its own rollback-scoped `session` fixture:
  `engine.connect()`, `begin()`, `Session(bind=connection)`. Follow that pattern;
  the session-level harness owns database isolation, not shared test data.
- A public seam that requires independent committed transactions — durable
  leases, competing workers, or restart recovery — uses only the harness-owned
  `runtime_database` fixture in `tests/conftest.py`. Ordinary database tests
  remain rollback-scoped; test modules do not provision their own databases.
  The harness copies these databases from one migrated per-process template;
  database upgrade tests build one real template per requested revision, clone
  it, seed historical rows, and then test the requested upgrade path.
- `llm_model` and the prompt version in `prompts/` are recorded on every
  Extracted Proposal. Changing either without an eval run makes the numbers
  incomparable.
- Policy-recorded `Applies To: not yet known` is current Project Record state,
  not a confirmation question. Show known facts read-only, continue with the
  assigned project person and Next Action, and keep unknown scope as an
  Attention Reason.
  Do not keep a generic scope picker on that Work Item. A later scope correction
  belongs only in an explicit Correct flow bound to verified source passages,
  with human-readable Constraint context; never render a scope-mode quiz or save
  an attributable no-op confirmation (ADR-0035, ADR-0036, ADR-0039, ADR-0042).
- Keep the branch boundary clear: the pending Extracted Proposal screen may still ask for
  explicit scope when scope is the actual unresolved human decision; the
  mechanically admitted unknown-scope screen may not.
- When changing statement UI, verify both `tests/test_statement_coordination.py`
  and `tests/test_work_list.py`; the same authority rule appears in guided Save,
  correction, and Work List rendering.

## Agent skills

### Issue tracker

Issues live as GitHub issues in `bharm16/corridor`, driven by the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Multi-context — start with `CONTEXT-MAP.md`, then read the mapped Project Record or Corridor Operations glossary and the relevant `docs/adr/`. See [the domain guide](docs/agents/domain.md).

### Research before terminology

Before **suggesting, adding, renaming, or redefining a domain term**, including a customer label, complete [Research before proposing terminology](docs/agents/domain.md#research-before-proposing-terminology). This applies in conversation and plans before any file edit: research primary industry sources, record their meaning and scope, then propose the term. Use the mapped glossaries and [ADR-0048's adoption boundaries](docs/adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md), including its legacy mapping; preserve existing code identifiers until a separately scoped implementation changes them.
