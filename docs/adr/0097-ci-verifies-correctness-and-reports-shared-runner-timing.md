---
status: accepted
domain: testing
scope: current product
amends:
  - ADR-0087
  - ADR-0096
---

# CI verifies correctness and reports shared-runner timing

The maintainer requested repair of recurring timing-only CI failures on
2026-09-09. ADR-0096 coupled a successful correctness proof to noisy elapsed
time on shared hosted runners. Repeated retries became the way to merge the
same successful tests. This amendment separates those verdicts and removes
the recurring cold-cache cause; it changes no test population.

## Evidence

The latest fifteen workflow runs contained nineteen attempts: nine failed
only the timing policy, four had real required-job failures, five passed,
and one was cancelled. Successful attempts alone are a biased performance
sample. Three examples establish the failure without selecting only green runs:

| Run / attempt | Gate | Migration | Result |
| --- | ---: | ---: | --- |
| [34380039992/1](https://github.com/bharm16/corridor/actions/runs/34380039992/attempts/1) | 190.0s | 43.9s | All tests passed; timing failed |
| [34380039992/2](https://github.com/bharm16/corridor/actions/runs/34380039992/attempts/2) | 176.2s | 40.8s | Same revision passed |
| [34384002900](https://github.com/bharm16/corridor/actions/runs/34384002900) | 169.3s | 45.49s | All tests passed; migration timing alone failed |

Run 34381546750 also included approximately 37 seconds between one hosted
job's reported start and its first setup step. Its tests passed, its gate
took 193.1 seconds, and migration took 47.8 seconds. The six-sample historical
window used by the current policy had a 185-second median; removing one
sample moved that median between 179 and 191 seconds. These are elapsed-time
observations, not evidence that the code is incorrect.

Cache inspection identified a separate execution cost. Pruning retained only
about 82 KB; one cache hit still led to 67 seconds of wheel downloads. Keeping
wheels produced a roughly 167 MiB cache, but that cache existed only under PR
merge refs. GitHub [isolates PR caches from sibling PRs and the default branch](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache).
Merging the cache configuration therefore did not make the warmed cache
available to the next PR.

## Decision

`release-gate` remains the sole required PR status. Every expected job must
succeed, every deliberately unneeded job must be skipped, and all current
receipts must prove the exact run, revision, partition and complete file
population. Failed tests, timeouts, cancelled jobs, missing or malformed
receipts and inconsistent classifier results continue to fail the gate.
The existing bounded job timeouts stop stuck execution.

Shared-runner elapsed-time targets do not change that correctness verdict.
`tests/feedback-budget.json` retains the same 180-second median, 300-second
tail and 45-second migration targets. The existing assessment still records
every breach, and CI publishes a warning plus the complete timing report.
Measurements remain validated and feed the next file partition. Neither slow
results nor queue time are erased to make the report look healthy. A timing
warning is investigated from its spans and expensive files; rerunning an
otherwise successful PR until the stopwatch happens to pass is not remediation.
The explicit offline assessment command can still return a failing timing
verdict when used for a performance investigation.

The default branch populates the two existing unpruned wheel-cache profiles
when dependency/cache inputs change and on manual dispatch. The warmer uses
the same runner and lockfile identities as PR CI, installs locked dependencies,
and runs no tests, database setup or deployment. Separate root-only and
root-plus-render keys prevent a shorter check job from publishing an incomplete
shared cache. PRs retain their own isolated cache for changed dependency inputs.

## Consequences and validation

This explicitly replaces ADR-0096's rejection of advisory timing budgets.
Increasing arbitrary cutoffs would leave the same probabilistic merge failure;
discarding measurements would hide the execution problem. Correctness remains
enforced, performance remains visible, and cache reuse removes real repeated work.

The regression exercises `ci_feedback.finish`, including job metadata and
receipt validation, with the observed 169.3s/45.49s and 193.1s/47.8s cases.
They report their timing breaches and permit the successful correctness proof.
Missing, failed, stale and incomplete proof still refuses. Workflow tests
verify cache scope, matching profiles, locked installation and the absence of
test/deployment commands in the warmer. Required CI is then run repeatedly;
a fresh PR after the warmer completes must demonstrate default-branch cache
reuse. Timings from each run remain disclosed rather than cherry-picked.
