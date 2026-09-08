# Complete timing after native integration

Both partitions now use successful measurements at
`ebe16417bbcf1a06a3fc812d8cc3406778e2bf77`, after the native integration,
qualification-mechanism and regression-proof changes. The source checkout
was clean before, between and after the two successful runs. Each gate used
four workers, and no other team test lane ran concurrently. Root and render
dependencies were installed before measurement.

| Gate | Passed | Declared skips | Failures/errors | Pytest wall time |
|---|---:|---:|---:|---:|
| `make test-timing` | 5,157 | 3 | 0 | 228.52 s |
| `make test-slow-timing` | 72 | 2 | 0 | 95.02 s |

The unchanged timing producer regenerated `tests/durations.json` and
`tests/durations-slow.json`. Each records all 257 test files, including explicit
zeroes where the gate selects no measured work. The measurements total 867.9
non-slow file-seconds across 249 observed files and 274.8 slow file-seconds
across eleven observed files. These sums are not elapsed gate time; tests ran
in parallel. An independent check verified every rounded value against its
JUnit source, the complete file set, and exactly one appearance per partition
under the existing five non-slow/four slow shard configuration.

The suite selection rules, skip conditions, shard counts, release conditions,
migration window and production configuration are unchanged. These local observations set partition
weights; they do not establish a new CI performance claim.

## Regression proof corrected before final measurement

The first non-slow attempt exposed two native-pipeline tests that assumed the
entire database contained no record-inclusion requests. A controlled fixture
proved the runtime preserved an unrelated existing request while those
zero-count assertions failed. The tests now compare every row and column in
the protected relations, preserve preexisting requests and selection receipts,
and allow only the exact returned new selection IDs. Successive checkpoints
also preserve earlier selections from the same operation sequence.

Independent review then found that Core table reads could miss queued ORM
changes. The snapshot explicitly flushes first. Three regression controls
queue an insertion, update or deletion without flushing; each exposed the
old false negative and now triggers the intended preservation assertion.
The corrected test file passed all 24 tests and both independent review axes.
Runtime code is unchanged by these test corrections.

## Environment and declared limits

The fresh checkout initially lacked the already downloaded corpus cache.
Ignored local links made the existing content-addressed bytes available, so
nine corpus regressions ran in the final measurement. Nothing was downloaded
or re-authored to change a reference.

The first slow attempt selected Homebrew's PostgreSQL 14.19 `pg_dump` against
the PostgreSQL 16.15 server and failed its real replay. The successful rerun
used task-local PATH wrappers invoking the existing PostgreSQL 16.15 container
clients, with host port 5433 mapped to container port 5432. No system package,
repository runtime or shared database schema was changed. The source capture
and disposable restore checks stayed intact.

The three non-slow skips cover macOS's unenforced `RLIMIT_AS`, the unavailable
live NHHIP project and the optional legacy-development login. The two slow
skips require the separately managed SH99 source and its expected lifecycle
state. These are disclosed limits, not successful coverage of those live
conditions.

## Retained receipts

The [structured receipt](test-timing-native-integration-2026-09-08.json)
records the exact XML/log and generated-weight digests, skip reasons and shard
coverage checks. Complete stdout/stderr and JUnit files remain outside the
checkout under
`/Users/bryceharmon/Desktop/corridor-evidence/2026-09-08-program727/timing/ebe1641-native-wave/`.

The original #752 failure, the successful #753 slow correction, the first
native-wave failure, the successful intermediate non-slow run, the unflushed
ORM mutation proofs, and the PostgreSQL-client mismatch are all preserved.
No failed or superseded run supplies these final weights. The earlier
[slow-timing receipt](test-timing-2026-09-08.md) remains historical evidence.

Reproduction uses the existing commands, after the full gates succeed:

```bash
uv run python scripts/test_timing.py out/timing/non-slow.xml --write tests/durations.json
uv run python scripts/test_timing.py out/timing/slow.xml --write tests/durations-slow.json
```
