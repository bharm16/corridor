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

Every entry point is a `make` target. Bare `make` lists them all with a
one-line summary, and `make <target> ARGS=--help` prints that command's own
arguments and worked invocations, which is where the contract lives: a
Makefile comment cannot travel with the flag it describes, and one of them
had already detached onto the target that drops databases. `make extract`,
`make agreements`, and `make minutes` call a model and need `OPENAI_API_KEY`
in `.env`.

## Testing

Run test targets directly and wait for their process/session exit status.
Every local pytest target runs through `scripts/run_local_tests.py`, which
streams pytest output and records running/completed state plus the actual exit
code in `out/test-results/<suite>.json`: `make test-focused`, `make test`,
`make test-slow`, `make test-full`, `make test-serial` (the `full` receipt) and
`make test-timing`/`make test-slow-timing` (the `test` and `slow` receipts,
because each measures that suite's selection). Use that receipt for background
monitoring.
Pytest summaries may contain skip and warning counts; never wait for a text
pattern such as `passed in`, or pipe live test output through `grep`/`head`.
Focused commands stop after 30 seconds by default; choose a smaller seam, or
set `FOCUSED_TEST_TIMEOUT_SECONDS` for a justified diagnostic. Broad diagnostics
stop after ten minutes (`TEST_TIMEOUT_SECONDS`). A stale monitor is not a reason to
launch a second suite: inspect the receipt and the existing process first.

**Local full-suite execution is blocked by default**, including raw unscoped
pytest. `LOCAL_BROAD_REASON=failure-reproduction` is for an observed failure
that cannot be reproduced with focused tests; `performance-investigation` is
for an explicitly requested suite-performance diagnosis. These are the only
local exceptions. Routine implementation, intermediate commits, reviews,
documentation, and pre-push reassurance are not reasons to run a local broad
suite. GitHub CI is the automatic broad gate.

An explicitly requested engine-retirement acceptance uses `make
test-engine-absent MODE=suite`. Its dedicated pytest flag admits only the
prepared environment after rechecking both environments for engine absence
(#766). It does not add a general-purpose broad-test diagnostic reason.

Use non-overlapping gates appropriate to the changed behavior
([ADR-0065](docs/adr/0065-test-gates-preserve-feedback-without-weakening-release-proof.md),
as amended by [ADR-0087](docs/adr/0087-the-migration-window-and-the-feedback-budget-are-enforced-numbers.md),
[ADR-0088](docs/adr/0088-the-required-gate-runs-the-whole-suite-in-parallel-not-a-path-selected-subset.md),
[ADR-0093](docs/adr/0093-the-required-gate-is-one-always-triggered-workflow-with-a-fail-closed-summary.md)
and [ADR-0096](docs/adr/0096-the-required-gate-measures-its-own-cost-and-rejects-feedback-budget-regressions.md),
as amended by [ADR-0097](docs/adr/0097-ci-verifies-correctness-and-reports-shared-runner-timing.md)):

- **Documentation-only edits: run `make check`.** It requires no database or
  CDK toolchain; CI skips infrastructure installation and synthesis too.
  Existing behavior-test results remain valid when only documentation changes;
  rerun the documentation checks, not PostgreSQL suites.
- **Batch related edits before validation.** A file edit, plan step, or local
  commit is not a testing checkpoint. Finish the coherent behavior change,
  then run its smallest meaningful focused check. Run again only after a
  relevant behavior change, a failure, or a concrete unresolved concern.
  A plan's verification step may be inspection or a static check; it does not
  automatically mean pytest. Pure focused tests require no database.
- Real-corpus bulk replays belong in the slow complement. Keep the ordinary
  developer loop on bounded synthetic examples. Retain and disclose existing
  corpus-availability skips; a skipped source-specific replay is not proof
  of that source. Run it once where its retained input is available.
- **One agent owns validation for a worktree.** Implementation workers report
  the checks their changes need; the coordinator combines overlapping checks
  into one run. Other agents review the results rather than launching copies.
  Keep intermediate commits local and push the reviewed change for its CI gate.
- **PR CI owns the complete release proof.** After focused checks pass, push
  for that proof. Use a local broad diagnostic only under the explicit
  exceptions above; it is not a prerequisite to CI or a loop to repeat after
  every edit.
- **PR CI is one workflow, `.github/workflows/release-gate.yml`, triggered on
  every pull request.** It runs `make check` unconditionally, and the same
  tests `make test` and the non-migration `make test-slow` select, partitioned
  by `make test-shard` across eight runners and `make test-slow-shard` across
  three, unless every changed file is documentation. This adds one ordinary
  runner to the previous seven/three allocation; required CI measures the
  capacity change against the unchanged budget (ADR-0096). `make check` owns
  `test_architecture.py` and `test_source_scan_support.py`; behavior shards
  omit those two files so each required proof runs once. Every other behavior
  test remains required (ADR-0088, ADR-0096).
- The independent `check` job runs CDK assertions and synthesis only when
  `scripts/classify_ci_change.py` identifies an infrastructure input. Pure
  workflow and deployment-runbook assertions stay in unconditional `make check`.
- **The `release-gate` job is the required status, and it fails closed.** It
  runs under `always()`, and matches every job result against what
  `scripts/classify_ci_change.py` asked for: `success` where the classifier
  said the gate was required, `skipped` where it said it was not. A
  `failure`, a `cancelled`, an unexpected `skipped`, or an inconsistent
  classifier/job pair fails it. **Never rewrite it as `success or skipped ->
  pass`** — that is how a behavior job skipped by a broken condition reports
  green (ADR-0093). After these result checks, the same required job validates
  every timing receipt. Missing, duplicate, stale, failed or incomplete proof
  fails the gate. Elapsed-time targets in `tests/feedback-budget.json` produce
  visible warnings and reports; they do not invalidate successful correctness
  evidence on shared runners. Job timeouts remain enforced (ADR-0097).
- **Path scoping lives on the jobs, never on a trigger.** A workflow skipped
  by a trigger-level path filter leaves its checks *pending*, and a required
  check that never reports blocks the pull request forever. A job skipped by
  an `if` reports `skipped`, which the summary can inspect. Do not add
  `paths` or `paths-ignore` to any `pull_request` trigger;
  `tests/test_ci_policy.py` fails if you do.
- **Documentation is a narrow allowlist**, not `**.md`: `docs/**`,
  `README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `CONTEXT-MAP.md`,
  `roadmap.md`. Everything else runs the behavior suites — `prompts/**` above
  all, because `src/corridor/extract_agreement.py` and the minutes extractors
  load those Markdown files as executable prompts at runtime, so a
  prompt-only change is a behavior change.
- **Raise the runner count one step at a time, and measure it.** Ten test jobs
  (six non-slow plus four slow, twelve counting `check` and `migration`)
  saturated this account's package downloads: `uv sync --locked` went from 2s
  to as much as 588s and two jobs stalled for *minutes inside pytest*, taking
  the gate to 11m55s. Nine jobs measured healthy twice, every `uv sync` at
  1-2s. The 2026-09-08 seven/three trial passed in
  [run 34291250899](https://github.com/bharm16/corridor/actions/runs/34291250899):
  164.92s gate, 112.41s ordinary command, 113.15s slow command, 31.48s migration.
  The 2026-09-09 eight/three trial follows two all-tests-passing #780 runs
  whose gates exceeded the budget (185.4s and 202.4s, ordinary commands
  125.2s and 138.3s). Keep measuring this allocation on subsequent PRs; every feedback threshold
  is unchanged. A historical ceiling is evidence to check, not a permanent
  prohibition on more capacity.
- **Nothing may download packages inside a test.** `workers/render` is a
  separate uv project whose `opencv-python-headless` is never in the root
  lock, so the first page render used to build that environment over the
  network, inside pytest, in four racing xdist workers, with its output
  captured. Every job that runs tests now runs `uv sync --project
  workers/render --frozen` first.
- **CI's per-job setup is one concurrent step**, `scripts/ci_environment.sh`.
  PostgreSQL starts from the runner image while the locked Python environments
  are prepared; it does not pull a `services:` container.
  The gate's wall clock is the slowest required job, so it samples the
  worst setup draw taken in the run rather than the average one: serial setup
  steps add their draws, concurrent ones do not (#595).
- **CI measures and reuses its own timings.** Each test command records its
  receipt in a unique job output and its log. The required summary validates
  current job outputs, writes
  the feedback report to the run summary, and retains it in its own log.
  The next run shares one validated timing output with every shard. No artifact
  upload is required after a test passes.
  `tests/durations*.json` are bootstrap weights. Routine changes do not require
  local full-suite timing reruns or duration-only follow-up PRs. Use
  `make test-timing` or `make test-slow-timing` only to diagnose a local cost.
- **Investigate timing warnings from evidence.** The feedback report retains
  current gate time, rolling median/p90, migration time and expensive files.
  Distinguish hosted queue/provisioning delay from setup and test work, then
  repair the measured cost. Do not rerun successful tests merely to draw a
  luckier time. Target changes require a new ADR; the current targets remain
  unchanged and advisory (ADR-0097).
- **Warm dependency caches on `main`.** The dependency-only cache workflow
  populates the same unpruned root and root/render profiles used by PRs.
  PR-scoped caches cannot warm sibling PRs. The warmer runs no test suite,
  PostgreSQL setup or deployment; details are in
  [the cache guide](docs/operations/ci-wheel-cache.md).
- **Match workers to the runner and fixtures.** Private Linux CI uses two
  xdist workers per runner. Ordinary tests use `worksteal`; slow tests use
  `loadfile` with `--no-loadscope-reorder` so each module fixture is built once
  and measured file order survives xdist's default case-count sort. Local `TEST_WORKERS` may be
  overridden for the machine. The migration target runs its owning file
  on two workers against the disposable databases that those tests create.

- A merge to `main` does not repeat that suite.
- Deliver changes to `main` through a PR; direct pushes have no duplicate
  post-merge test workflow.
- A change to migrations, schema models, the database test harness, or
  anything else in `MIGRATION_PATHS` in `scripts/classify_ci_change.py` also
  runs `make test-migrations`, as the gate's `migration` job. Ordinary
  application changes do not.
- `make test-full` is the complete manual and weekly scheduled gate,
  `.github/workflows/full-suite.yml`. It has no `pull_request` trigger and is
  not part of ordinary PR or post-merge CI.
- Source or test changes invalidate results for the affected behavior. Rerun
  that seam locally and let required CI prove the revised behavior. Changes
  limited to documentation do not invalidate earlier behavior-test results.

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
(#492); the application runtime role reads, appends segments, facts, and
proposals only through the source-append role's commands (`source_append.py`),
and cannot make anything effective. Support assessments join that matrix with
#530.
Implementation status is in [roadmap.md](roadmap.md). The spine lifecycle is
now built end to end: Adopt Baseline (#509), Proposed Delta identity (#518),
Resolve Delta (#519), and the atomic Review Packet transaction (#526). What
remains open on it is the released class-specific projection policies.

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
legacy reader working during migration. They also run only for a **legacy
project**: a project holding a baseline-adoption receipt is in
`adopted_baseline` operating mode (`operating_mode.py`, #520), and PostgreSQL
refuses every legacy accepted-value write for it. The Adopt Baseline importer
that establishes that mode is built (`baseline_adoption.py`, #509) and invokes
that one-way transition in the same transaction as the baseline it adopts.

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

**`release-gate` is a required status check on `main`.** The "main release
gate" ruleset blocks a merge until it reports success, so the gate is enforced
by the server rather than by remembering to look. Watch it anyway, because a
watched run tells you *which* job failed while the ruleset only tells you the
merge is blocked:

```bash
gh pr checks <pr-number> --watch --fail-fast
```

`release-gate` reports on every pull request, including documentation-only
ones, and it fails closed: a behavior job that was skipped when the classifier
said it was required fails the gate rather than passing it (ADR-0093).
Merging with a red or still-running job is a regression to file (#516).

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
- Database tests use real PostgreSQL — no sqlite fallback. Start the stack
  only for tests that connect to it. Parallel workers create their guarded
  database lazily on the first connection and reuse one migrated template;
  pure checks perform neither database setup nor database teardown. Serial
  database tests use the configured database.
- **A database test asks `tests/conftest.py` for its fixtures; it does not
  hand-write them.** `session` is one rollback-scoped Session over this
  worker's migrated database, so nothing a test writes through it survives the
  test. `project` is one synthetic Project flushed inside that transaction,
  with a fresh slug because `projects.slug` is unique and a module that commits
  a scenario must not collide with one that rolls back. `member_project` builds
  that project and seeds the one roster membership the #331 access gate wants,
  taking the acting principal the module names. `runtime_database` is the next
  bullet. Declare a local `project` only when the test asserts on a project's
  own slug or display name, or needs a value the shared row does not carry, and
  a local `session` only when the transaction itself differs: a non-default
  isolation level is an argument to `rollback_scoped_session`, not a copied
  fixture body. `tests/test_harness_fixtures.py` holds these promises, so a
  change to one fails there rather than in 170 modules.
- A public seam that requires independent committed transactions — durable
  leases, competing workers, or restart recovery — uses only the harness-owned
  `runtime_database` fixture in `tests/conftest.py`. Ordinary database tests
  remain rollback-scoped; test modules do not provision their own databases.
  A module that needs an isolated database of its own asks the harness's
  `provision_isolated_database` for one instead of calling
  `provision_disposable_postgres` itself. Either way the harness copies the
  single migrated template its run has already built, so nothing replays
  Alembic per fixture; only a test whose subject *is* the migration asks for a
  replay, with `reuse_migrated_template=False`.
  Database upgrade tests cover one fresh baseline and the single supported
  released-head-to-current transition. A migration test expires after every
  supported database has advanced past its starting revision; current behavior
  stays in ordinary tests.
- The supported migration window is the current released head and its immediate
  successor only. **At most one unreleased executable successor may exist after
  the revision recorded in `src/corridor/migrations/policy.py`.** A second
  migration-bearing change folds into the current unreleased transition, or
  consolidates the chain and lowers the recorded count first — it does not
  append another revision. "Add one linear successor" was read as "append one
  more successor in every pull request", which grew the chain from 2 revisions
  to 23 after #423 bounded it (#548).
- Test exact transformed rows, and retire the predecessor test when the window
  advances. Do not add another feature-specific blank-database history test;
  the baseline test owns that.
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
