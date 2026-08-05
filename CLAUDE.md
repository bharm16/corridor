# corridor

## Commands

```bash
make boot   # uv sync, Postgres 16 in Docker, alembic upgrade head
make test   # pytest — needs the stack up; tests hit the real database
make down   # stop the stack
```

Every entry point is a `make` target, and the Makefile comments say what each
one takes. `make extract`, `agreements` and `minutes` call a model and need
`OPENAI_API_KEY` in `.env`.

## Architecture

`src/corridor/`, one module per stage:

`corpus` (manifest → files) → `ingest` (pages: text + image) → `extract_*` and
`geometry` (Candidates) → `adjudicate` (**the only writer of the Ledger**) →
`ledger`, `exceptions`, `report`, `briefing`, `export` (readers).

Extractors only ever produce Candidates. Every module opens with a docstring
saying why it exists and what was tried before — read it before changing one.

## Gotchas

- Postgres is on host port **5433**. A Homebrew Postgres 14 takes 5432 and
  accepts the same credentials, so a wrong port connects and fails much later
  on version-specific SQL (`tests/test_boot.py` guards this).
- Tests run against the live database — no sqlite fallback, no `conftest.py`.
  Bring the stack up first.
- A database test defines its own `session` fixture: `engine.connect()`,
  `begin()`, `Session(bind=connection)`, rolled back on teardown. 22 test
  files do this; follow the pattern rather than adding shared state.
- `llm_model` and the prompt version in `prompts/` are recorded on every
  Candidate. Changing either without an eval run makes the numbers
  incomparable.

## Agent skills

### Issue tracker

Issues live as GitHub issues in `bharm16/corridor`, driven by the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
