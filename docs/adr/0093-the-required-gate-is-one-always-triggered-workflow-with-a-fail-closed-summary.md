---
status: accepted
domain: testing
scope: current product
amends:
  - ADR-0088
migration: the coordinator still has to add `release-gate` to the "main release gate" ruleset as a required context, confirm it reports on the latest commit, and only then remove the now-redundant `check` context (#697).
---

# The required gate is one always-triggered workflow with a fail-closed summary

**Amends ADR-0088.**

ADR-0088 decided *what* the gate runs: the whole non-slow suite and the whole non-migration slow complement on every change that is not documentation-only, partitioned across runners rather than selected by path. That decision stands untouched. This ADR decides *how the gate is shaped so it can be required server-side*, and it narrows one definition ADR-0088 wrote in passing.

## The problem ADR-0088 could not have seen

ADR-0088 described three workflows — `check.yml` on every pull request, `test.yml` under `paths-ignore: ["**.md", "docs/**"]`, and `migration-test.yml` under a `paths` allowlist — and treated all three as "required checks". They were not required, and could not be. The `main` ruleset's only required context is `check`, because `check` is the only one that reports on every pull request.

GitHub leaves the checks of a workflow skipped by a **trigger-level path filter** *pending*, not skipped. A required status check that stays pending blocks the pull request forever. So on a documentation-only pull request a required `pytest` would never report and the pull request could never merge, and on a non-migration pull request a required `migration` would do the same. The gate ADR-0088 specified was therefore enforced only by the merge procedure in `AGENTS.md` — a human running `gh pr checks --watch` — which is the "requires vigilance to work is not a guard" failure ADR-0088 itself named, one layer up.

## The decision

**The required workflow runs on every pull request. Expensive jobs remain path-conditional inside that workflow.**

`.github/workflows/release-gate.yml` is triggered on `pull_request` with no path scoping at all and holds every job: `classify`, `check`, `pytest[1..5]`, `slow[1..4]`, `migration`, and `release-gate`. The final `release-gate` job is the single context the ruleset requires.

A job skipped by an `if` condition produces a `skipped` **result**, which a summary job in the same workflow reads through `needs.<job>.result`. That difference — pending versus skipped — is the whole design. The expensive jobs keep exactly the scoping they had; it moves from the trigger to the job.

`release-gate` **fails closed**. It runs under `if: ${{ always() }}` so it still reports after a dependency fails, and it matches every result against what the classifier asked for:

| Classifier answer | Required job result |
|---|---|
| `behavior_required=true` | `pytest=success` **and** `slow=success` |
| `behavior_required=false` | `pytest=skipped` **and** `slow=skipped` |
| `migration_required=true` | `migration=success` |
| `migration_required=false` | `migration=skipped` |
| always | `classify=success` **and** `check=success` |

Any `failure`, `cancelled`, unexpected `skipped`, or inconsistent classifier/job pair fails the gate. **`success or skipped -> pass` is forbidden**: it would let a behavior job skipped by a broken condition report green, which is worse than having no required check at all, because a red gate would look green.

## Documentation is a narrow allowlist, not `**.md`

ADR-0088 permitted narrowing "to skip revisions where every changed file is documentation (`**.md`, `docs/**`)". That definition was wrong for this repository, and the error was latent, not hypothetical: **Corridor loads executable model prompts from Markdown at runtime.** `src/corridor/extract_agreement.py` reads `prompts/agreement_v3.md`, `extract_minutes_v4.py` and `extract_minutes_v5.py` read theirs, and `extractor_lineage.py` names more. A prompt-only pull request changes extraction behavior and skipped every behavior suite — the silent green ADR-0088 exists to prevent, delivered by ADR-0088's own escape clause.

Documentation is now the enumerated set `docs/**`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `CONTEXT-MAP.md`, `roadmap.md`. Everything else runs the behavior suites, `prompts/**` included.

## The classifier is in the repository, and counts both names of a rename

`scripts/classify_ci_change.py` diffs the pull request's base and head after a full-history checkout and emits `behavior_required` and `migration_required`. It is the repository's own code rather than a third-party path action for three reasons: the two answers are the gate's entire fail-closed contract and must be unit-testable; the rename handling below is a repository-specific rule no general action offers; and it must run before `uv sync`, from the standard library, so the job in front of the gate adds no package download to an account whose measured ceiling is concurrent downloads (#548, #595).

`git diff --name-status -M` reports a rename as one record naming both paths, and **both are classified**. Without that, `git mv prompts/agreement_v3.md docs/` would present a single documentation path and skip every behavior suite for a change that moved an executable prompt out from under the code reading it.

**An empty or unreadable diff is a classifier failure, never a docs-only answer.** Guessing `false` on a diff that could not be read is the same silent green in another costume.

The migration-adjacent set is carried over from the retired `migration-test.yml` trigger unchanged.

## Considered options

**Put the summary in a separate workflow.** Rejected. It would have no `needs` relationship to the test jobs and would have to poll GitHub for their state, which reintroduces both timing ambiguity and the absence-versus-failure ambiguity the `needs.<job>.result` values resolve exactly.

**Require `pytest` and `slow` directly and accept that documentation-only pull requests never merge.** Rejected on its face, and the fallback — running the full suites on documentation-only changes so they always report — spends the gate's whole feedback budget on `README.md` typos.

**Keep the three workflows and keep enforcing the gate through the merge procedure.** Rejected. That is the status quo, and it is a guard that works only while every agent and human remembers to run `gh pr checks --watch --fail-fast` and reads the result correctly.

**Write `success or skipped -> pass` in the summary, which is the common recipe.** Rejected, at length, above. It converts the required check from a proof into a decoration.

**Use a third-party changed-files action instead of an in-repository classifier.** Rejected. The rename rule and the fail-closed-on-empty-diff rule are the two behaviors that matter most here and neither is a configuration option; and a marketplace action inside the one job that gates everything is a supply-chain dependency for a `git diff`.

## Consequences

- ADR-0088's clause naming `**.md` as documentation, and its clause forbidding another `paths` filter on `.github/workflows/test.yml`, no longer govern; the file no longer exists. Everything ADR-0088 decided about *what* runs — full suites, no path-selected subset, an exact shard cover proven by a test, migration tests scoped to migration-sensitive changes, the budget numbers — is unchanged and still in force.
- `.github/workflows/check.yml` and `.github/workflows/test.yml` are retired into `release-gate.yml`. The `check` **job name is preserved exactly**, so the context the ruleset requires today keeps reporting throughout the transition and no protection gap opens.
- `.github/workflows/migration-test.yml` becomes `.github/workflows/full-suite.yml`, holding only the weekly schedule and manual dispatch that ran `make test-full`. It has no `pull_request` trigger, so it can never leave a check pending.
- The concurrent job count is unchanged. `classify` and `release-gate` run alone at either end of the gate and install nothing, so the eleven jobs competing for package downloads are the same eleven as before; `tests/test_ci_policy.py` asserts the number.
- `tests/test_ci_policy.py` executes the summary job's shipped script against every classifier/result combination, and `tests/test_classify_ci_change.py` drives the classifier through real git repositories, including the rename and the empty diff.
- The ruleset transition is deliberately not performed by the change that lands this file: `release-gate` is added as a required context **while `check` is retained**, confirmed to report on the latest commit, and only then is `check` removed as a separately required context, since the aggregator already requires it.
- The merge procedure in `AGENTS.md` stops describing `gh pr checks --watch --fail-fast` as the thing that makes the gate binding, and describes it as feedback while the server-side requirement is what blocks the merge.
- #697 closes with this decision.
