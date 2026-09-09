# Preserve the wheels CI actually installs

PR #782's required run 34379131971 passed its tests but measured 188.1 seconds
against the unchanged 180-second gate. Its slow shard 1 restored an 82,563-byte
cache, then downloaded 78 packages in 67 seconds; root plus render preparation
consumed about 69 seconds. A cache hit was not evidence of reusable wheels.

`astral-sh/setup-uv@v6` defaults `prune-cache` to true. Its
[documented behavior](https://github.com/astral-sh/setup-uv/blob/d0cc045d04ccac9d8b7881df0226f9e82c39688e/README.md#disable-cache-pruning)
removes downloaded wheels before saving while retaining source-built wheels.
The selected environment installs prebuilt wheels, so every runner fetched them
again. Required and scheduled Python jobs now use `prune-cache: false`.

The action includes pruning mode in its cache key, so this change cannot restore
the old `-pruned` metadata-only cache. Default dependency globs still include the
project manifests and lockfiles. `root-check-wheels-v1` separates the check job
from `root-render-wheels-v1`, shared by ordinary, slow, migration and scheduled
full-suite jobs. Every job sharing the latter installs both locked environments;
a quicker root-only job cannot publish an incomplete cache under that key.

The first run fills the new cache; a later matching run measures its reuse.
Larger cache transfer has a real cost, and required CI still measures total gate
time against the same budget. No test, runner, gate condition, package lock,
installation command, or feedback threshold changes.

## Make the cache available to the next pull request

A successful PR cache is scoped to its merge ref. GitHub allows a PR to restore
its own or its base/default branch's cache, but not a sibling PR's cache. Merging
PR #782 did not move its approximately 167 MiB wheel cache into `main`.
The 2026-09-09 inventory found complete caches under PRs 781, 782 and 787 while
`main` still held only an older 82,626-byte pruned cache with a different dependency
hash. Same-PR warm retries therefore overstated what the next new PR could reuse.
See [GitHub's cache access rules](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache).

`warm-python-cache.yml` fills both cache populations on `main` after dependency,
Python-selection or cache-policy changes merge. A manual dispatch on `main` can
also initialize or refresh a missing cache. Dispatching another branch is refused
by the job condition. The workflow has read-only repository permissions and runs
only locked dependency installation: the root environment for
`root-check-wheels-v1`, and root plus render for `root-render-wheels-v1`.
It runs no tests, PostgreSQL setup or infrastructure deployment.

The warmer uses the same runner image, setup-uv version, implicit Python selection
and default manifest/lock globs as the release gate. Cache saves occur only after
a successful installation. Existing cache entries remain immutable; dependency
hash or Python changes choose a different key. A first-ever dependency change
still has a cold PR before its new key can be populated on `main`, and a new PR
opened before warming finishes may also download. Those cases remain visible in
the required timing receipts; this workflow does not waive a test or alter a gate.
