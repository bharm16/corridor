---
status: accepted
domain: testing
scope: current product
amends:
  - ADR-0087
amended_by:
  - ADR-0093
---

# The required gate runs the whole suite in parallel, not a path-selected subset

**Amends ADR-0087.**

ADR-0087 decided two things about the pull-request gate. It recorded a feedback budget as numbers a test can read, and it decided that the required gate would be *selected*: `make check`, a small critical suite, integration tests for the affected component, migration tests only on migration-sensitive paths, and path selection that fails wide for `models.py`, `db.py`, dependency configuration, shared persistence, and migrations, with the full suites moving to the weekly, manual, and pre-release runs.

The budget half was pursued and largely met. The selection half was never built, and by the time the budget was met, selection had stopped being the lever that would meet it. #548 brought the gate from 7m59s to roughly 4m25s by giving the same tests more machines and by splitting the one file that was setting the floor, without deselecting a single test. This ADR amends ADR-0087's selected-gate section to say what the repository actually does and intends to keep doing: **the full non-slow suite and the full non-migration slow complement remain required on every change that is not documentation-only, and their wall clock is managed by parallelism and by making individual tests cheaper, never by inferring from a changed path that a test need not run.**

Everything else in ADR-0087 is unchanged and still governs: the machine-readable migration window in `src/corridor/migrations/policy.py`, the ratchet that may fall and may never rise, the budget numbers themselves, the rule that measurement comes before removal, and migration tests running only on migration-sensitive paths.

## What the required gate runs today

Three workflows make up the gate, and they run concurrently.

- `.github/workflows/check.yml` runs `make check` on every pull request with no path scoping at all, because `make check` owns the ADR lifecycle, amendment-graph, and `INDEX.md` freshness tests that a documentation-only change can break.
- `.github/workflows/test.yml` runs two jobs, `pytest` and `slow`, each as a four-way matrix on separate runners. `make test-shard` and `make test-slow-shard` pass the same `-m "not slow"` and `-m "slow and not migration"` selections that `make test` and `make test-slow` pass, over a partition of the test files that `scripts/test_shard.py` computes from the per-file seconds in `tests/durations.json` and `tests/durations-slow.json`. The only path scoping on this workflow is `paths-ignore` for `**.md` and `docs/**`.
- `.github/workflows/migration-test.yml` runs `make test-migrations` on the migration-sensitive path list, and `make test-full` on the weekly schedule and on manual dispatch.

So for every change that touches anything other than documentation, the whole non-slow suite and the whole non-migration slow complement are required, exactly as they were before #548. What changed is where those tests run, not which of them run. No critical suite exists, no component-to-test mapping exists, and no fails-wide path list exists, because nothing selects.

## Where the gate stands against the budget

The numbers below come from the repository and from the GitHub Actions run history, not from estimates.

The `test` workflow's wall clock on pull requests, over the eleven successful runs since the acceptance-suite split merged on 2026-09-02, is 248, 252, 253, 262, 262, 265, 266, 268, 272, 282 and 293 seconds. That is a **median of 4m25s and a slowest run of 4m53s**. ADR-0087's ceiling is a 90th percentile under five minutes and a median under three: the p90 half is met with about seven seconds to spare on the worst run, and **the median half is not met**. The other two required workflows finish well inside it — `check` ran 19s to 30s across its last 28 successful pull-request runs, and the `make test-migrations` step took **31s** against ADR-0087's 45-second number.

The gate's wall clock is its slowest single job, and that job is dominated by one file. In the most recent run at the time of writing (Actions run 33717572688), the slowest job was `slow (1)` at 248s, of which `make test-slow-shard` was 192s and the surrounding container start, `apt-get`, `uv sync` and `alembic upgrade head` were the other ~56s. Its pytest summary was `12 passed in 187.82s`. The four slow shards held 12, 46, 0 and 1 test respectively — shard 3 ran nothing at all — because only eight files carry slow tests, and a file cannot span runners. `tests/durations-slow.json` records `tests/test_m8_acceptance.py` at 264.9s of a 422.8s slow total, which is the same floor stated a different way.

This is worth being plain about, because it says exactly where the remaining time is and what could move it. Adding runners cannot help a gate whose wall clock is one file. Splitting or cheapening that file can, and #553 already demonstrated that on the same file, which is what took the gate under five minutes. Removing roughly fifty seconds of repeated per-job setup could help too. Selecting tests by path would also help, and the next section says why it is not the way we will take.

## Why parallelism, and not path selection

Path selection was ADR-0087's answer to a slow gate, and ADR-0087 was already uneasy about it: it wrote the fails-wide rule precisely because "the failure this decision guards against is a slow gate; the failure it must not create is a gate that skips affected tests." That is the whole argument, and the evidence gathered since has only strengthened it.

The failure modes are not symmetric. A slow gate is visible on every pull request, annoying, measurable, and self-correcting because somebody eventually fixes it — which is what #548 was. A gate that skips an affected test is silent. It reports green, the change merges, and the cost arrives later attached to a different change, with the evidence that would have identified it never collected. Requiring the full suites removes that failure mode outright rather than mitigating it, and it removes it without needing anyone to keep a path-to-test map accurate as the codebase moves.

The fails-wide rule was the mitigation, and mitigations of this shape are not enforceable in practice. It requires an accurate, maintained answer to "which tests could this file affect", for a codebase where the answer is mostly "the ones that touch PostgreSQL", which is most of them. `models.py`, `db.py`, dependency configuration and shared persistence are named in ADR-0087 as the wide triggers; a genuine list would be much longer, would need extending on every new coupling, and would fail silently when someone forgot. A guard that requires vigilance to work is not a guard — ADR-0087 rejected the filename allowlist for exactly this reason, and the fails-wide path list is the same object one level up.

This suite in particular has already been caught hiding order-dependence behind a scheduling decision. #551 found `_stored_candidates` ordering by the string `subject_key`, so `"constraint:100"` sorted before `"constraint:99"`; `--dist loadfile` had been masking it by keeping each file on one worker, and only rebalancing exposed it. A suite that concealed a real defect inside its distribution mode is not one whose dependency graph should be inferred from a path list.

And the win that selection was reached for has largely been collected another way. The gate fell from 7m59s to about 4m25s with nothing deselected, nothing reclassified, and no test deleted, which is the measurement ADR-0087 insisted on having before anything was cut.

The honest counterpoint, recorded because it is real: selection probably *would* reach the three-minute median, because for most changes it would skip `tests/test_m8_acceptance.py`, which is essentially the entire remaining wall clock. That is precisely the trade being refused. The saving would come from not running the most expensive integration test on changes whose relationship to it was inferred from a path list rather than measured. The median stays unmet and stays recorded until the file is made cheaper or split further.

## What is required, and what may narrow it

- Every change that modifies anything other than documentation runs the whole non-slow suite and the whole non-migration slow complement. Both are required checks.
- The behavior gates may be narrowed by path only to skip revisions where every changed file is documentation (`**.md`, `docs/**`), because `make check` still runs unscoped on those and owns the only tests that read documentation. No other `paths` filter may be added to `.github/workflows/test.yml`; a filter that named the paths a job runs *for* would be the selected gate this ADR declines.
- The shard partition must be an exact cover of the test files, for each gate separately, and a test must prove it. A file that lands in no shard is a green gate that ran nothing, which is the one way sharding can reproduce selection's failure by accident.
- Migration tests remain path-scoped to migration-sensitive changes, unchanged from ADR-0065 and ADR-0087. The migration chain is a bounded, enumerable dependency; application code is not.
- ADR-0087's budget numbers stand as written and are not relaxed to fit the measurement. Bringing the median inside three minutes is done by making tests cheaper, splitting expensive files, removing repeated per-job setup, or adding runners.

## Considered options

**Implement ADR-0087's selected gate as written.** Rejected. It buys a median the repository can also reach by splitting one file, and it pays for it with the failure mode ADR-0087 itself named as the one the decision must not create. The fails-wide list that was meant to prevent that failure would have to be maintained by hand against a codebase where nearly everything touches shared persistence.

**Leave ADR-0087 as written and let CI stay non-compliant with it.** Rejected, and it is the option this ADR exists to close. ADR-0087's own finding was that a decision written as prose, with no mechanism that can fail, decays into a record of whatever happened. A governing decision that the required gate is selected, sitting above a gate that selects nothing, is that same defect in the document layer.

**Edit ADR-0087's gate section in place.** Rejected under `docs/adr/README.md`. Code complying with ADR-0087's selected-gate clause — a workflow with a `paths` allowlist and a critical suite — would fail review under this wording, and the normative "must" changes, so this is a new sequential decision carrying reciprocal `amends` metadata rather than an edit to an accepted body.

**Relax the three-minute median to match what is measured.** Rejected. A budget moved to wherever the measurement landed is not a budget, and the whole reason #548 exists is that the previous numbers lived only in prose. The median stays where it is, unmet, visible, and attached to a named cause.

**Drop the slow complement from the required gate and run it only on the schedule.** Rejected. It is path selection with a coarser knife: the slow complement is where the acceptance and rehearsal proofs live, and it is exactly the part a change is least likely to be thought to affect and most likely to break.

## Consequences

- ADR-0087's section "The required gate is selected, not exhaustive" no longer governs. Its critical-suite composition and its fails-wide path rule are withdrawn. Its window, ratchet, budget numbers, measurement-before-removal rule, and migration-path scoping are untouched and still in force.
- ADR-0065 needs no amendment and gains no new relationship. Its gate clause — "Normal PR CI runs `make check`, `make test`, and `make test-slow` once" — describes what happens today; the shard targets select the same two sets of tests, partitioned across runners, and `tests/test_ci_policy.py` proves each set runs exactly once and that the partition covers the suite.
- `tests/test_ci_policy.py` gains an assertion that `.github/workflows/test.yml` carries no `paths` allowlist, so narrowing the behavior gates to a subset of code paths cannot happen without changing this decision first.
- ADR-0087's `migration` note is corrected: the executable chain reached one revision in #554, so the sentence naming 21 unreleased transitions is stale, and what is actually outstanding is the three-minute median.
- The remaining distance to that median is `tests/test_m8_acceptance.py` and about fifty seconds of repeated per-job setup. Neither is scheduled here.
- #548 closes with this decision. The gate it delivered and the decision that governs it now say the same thing.
