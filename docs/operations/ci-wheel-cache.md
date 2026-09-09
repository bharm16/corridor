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
