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
- Normal PR CI runs `make check` on every pull request, and `make test` plus
  the non-migration `make test-slow` selection unless every changed file is
  documentation (`**.md`, `docs/**`). A merge to `main` does not repeat that
  suite.
- Deliver changes to `main` through a PR; direct pushes have no duplicate
  post-merge test workflow.
- A change to migrations, schema models, or the database test harness also runs
  `make test-migrations`. Ordinary application changes do not.
- `make test-full` is the complete manual and weekly scheduled gate. It is not
  part of ordinary PR or post-merge CI.
- Any source or test change invalidates an earlier result. Rerun the smallest
  affected seam, then the scoped gate for the revised change.

## Architecture

### Target architecture (ADR-0075, ADR-0076, ADR-0081, ADR-0082, ADR-0083)

```
SourceEnvelope (one normalized ingress record per delivery)
→ Source Segment (exact text or value, typed locator, digest)
→ Source Fact (what the source says, captured; never the record)
→ Proposed Delta (typed difference from the accepted record; record unchanged while open)
→ Resolve Delta (human, or narrow released policy; one atomic Project Record Revision)
→ current and as-of projections
```

Adopt Baseline is the one bulk human act that establishes the accepted record
from the customer's own UCM workbook or system export. Accepted authority is
written only through the record-decision role's `SECURITY DEFINER` commands
(#492); the application runtime role reads and may append segments, facts,
proposals, and support assessments, and cannot make anything effective.
Implementation status is in [roadmap.md](roadmap.md): Adopt Baseline and
Proposed Delta are **not yet implemented** (#509, #510).

### Transitional legacy path (frozen; ADR-0081)

`src/corridor/`, one module per stage, as built for the readiness ledger:

`corpus` (manifest → files) → `ingest` (pages: text + image) → `extract_*` and
`geometry` (Extracted Proposals) → `adjudicate` (the legacy writer of Constraint
Records) → `ledger`, `exceptions`, `report`, `briefing`, `export` (readers).

`admission.py` (`load_project`), `dependency_admission.py`, `event_admission.py`,
and structured-cell automatic inclusion (`include_current_structured_cell_facts`)
still perform ADR-0029-style automatic Record Inclusion. They are **legacy paths,
frozen against new capability**: no new feature may be implemented solely
against `dependencies`, `dependency_events`, `work_decisions`,
`operative_support`, or the dispute tables. New work writes the spine first and
must not introduce another legacy-only write; a compatibility write may keep a
legacy reader working during migration. Do not treat the current pipeline as
the authority model for a customer pilot until the adopted-baseline operating
mode exists (#520).

Extractors only ever produce Extracted Proposals. Every module opens with a docstring
saying why it exists and what was tried before — read it before changing one.

## Merging

**Start on a branch, before the first commit.** `main` is for merges only, so
a direct commit to it is a defect even when the change is small and even when
an instruction says to commit to the current branch. Check where you are
first, and branch if the answer is `main`:

```bash
git rev-parse --abbrev-ref HEAD
```

Recovering a commit already made on `main` is `git branch <name> && git reset
--hard origin/main && git checkout <name>`, before anything is pushed.

Server-side required status checks are unavailable on this private free-plan
repository (#506). Until that changes, every PR merges only after every job is
green, verified with:

```bash
gh pr checks <pr-number> --watch --fail-fast
```

A merge with a red or still-running job is a regression to file (#516).

Merge as soon as a reviewed PR is green; do not leave finished work open.
Every merge squashes and deletes its branch in the same act:

```bash
gh pr merge <pr-number> --squash --delete-branch
```

Then update and prune, because `--delete-branch` removes the branch on the
server but leaves the local remote-tracking ref behind:

```bash
git checkout main && git pull --ff-only && git fetch --prune
```

Closing an issue follows the same rule: no branch, worktree, or
remote-tracking ref outlives the work it carried.

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
  database upgrade tests cover one fresh baseline and the single supported
  released-head-to-current transition. A migration test expires after every
  supported database has advanced past its starting revision; current behavior
  stays in ordinary tests.
- The supported migration window is the current released head and its immediate
  successor only. Add one linear successor, test exact transformed rows, and
  retire the predecessor test when that window advances. Do not add another
  feature-specific blank-database history test; the baseline test owns that.
- Executable revisions live in `src/corridor/migrations/baseline_versions`.
  `migrations/versions` contains inert source bytes retained only for released
  policy fingerprints; Alembic does not load that directory.
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

Multi-context — start with `CONTEXT-MAP.md`, then read the mapped Project Record or Corridor Operations glossary and the relevant `docs/adr/`. [`docs/adr/INDEX.md`](docs/adr/INDEX.md) lists the accepted decisions in force by domain; [`docs/adr/README.md`](docs/adr/README.md) holds the lifecycle rules. See [the domain guide](docs/agents/domain.md).

### Research before terminology

Before **suggesting, adding, renaming, or redefining a domain term**, including a customer label, complete [Research before proposing terminology](docs/agents/domain.md#research-before-proposing-terminology). This applies in conversation and plans before any file edit: research primary industry sources, record their meaning and scope, then propose the term. Use the mapped glossaries and [ADR-0048's adoption boundaries](docs/adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md), including its legacy mapping; preserve existing code identifiers until a separately scoped implementation changes them.
