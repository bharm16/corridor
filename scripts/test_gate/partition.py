"""The test files, the bootstrap weights that balance them, and the split.

#548 found the required gate CPU-bound: the GitHub runner has four cores, so
redistributing work between xdist workers on one runner cannot help, however
uneven the files are.  More actual CPU can, and GitHub runs matrix jobs on
separate runners.

Balance uses per-file seconds: the previous run's validated CI receipts when
the classifier can supply them, otherwise the recorded bootstrap weights in
``tests/durations.json`` and ``tests/durations-slow.json`` that
``scripts/test_timing.py --write`` produces through `write_durations` below.
Producer and consumer of that file used to live in different scripts with no
shared shape; here the writer records exactly the files `test_files` names,
zero included, and the reader hands them back.  A file with no recorded
duration is treated as average rather than free, so a newly added file cannot
silently land in an already-full shard.  Shards are filled longest-first onto
whichever shard is currently smallest, which keeps the slowest file from
deciding the wall clock on its own.

Every named file also costs its shard ``COLLECTION_SECONDS`` whether or not
the gate selects a test from it, because each xdist worker imports every
module named on the command line.  Measured by collecting the whole tests
directory against three files: 4.6s for 183 extra files, so 0.025s each, and
0.05s allowed for a CI runner.  It is a small term and it is meant to be --
an earlier revision set it to 0.5s from a CI shard that held 166 files and
ran 79s while running two tests, which turned out to be the render worker
building its environment over a saturated network, not collection at all.

`tests/test_ci_policy.py` proves that each gate's partition covers the suite
exactly; the former `scripts/test_shard.py` command that printed one shard
was invoked by nothing else and is retired.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DURATIONS = ROOT / "tests" / "durations.json"
SLOW_DURATIONS = ROOT / "tests" / "durations-slow.json"
# What one named test file costs a CI shard in import and collection, before
# any of its tests run: 0.025s measured locally, doubled for a CI runner
# (#548).
COLLECTION_SECONDS = 0.05
# A slow shard still imports files whose cases its marker expression deselects.
# The 2026-09-09 189/3/92-file split showed that near-free zero weights can pile
# almost the whole suite onto one shard. This is a scheduling floor, not a new
# timing claim or test-selection rule; every file remains assigned exactly once.
SLOW_MINIMUM_FILE_SECONDS = 0.5


def test_files() -> list[str]:
    """Every top-level test module, as the repository-relative path the gate uses."""
    return sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "tests").glob("test_*.py")
    )


def recorded_seconds(path: Path = DURATIONS) -> dict[str, float]:
    """Per-file seconds for the gate being sharded.

    The two gates select different tests from the same files, so balancing
    the slow shards by the non-slow timings puts every slow test in one
    shard: exactly what happened on the first sharded run, where one slow
    shard took 6m21s and the other three about 1m15s each.
    """

    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_durations(totals: dict[str, float], destination: Path) -> None:
    """Record per-file seconds for every test file, zero included.

    `shard` treats a file with no recorded duration as *average*, so an
    omitted file is not free -- it is imaginary work that unbalances the
    partition. Ten files added after the last hand-written recording were
    each counted as 52.8 imaginary slow seconds, which is how one slow shard
    ended up running no tests at all (#548). `junit.measured_cases` already
    names every assigned file and refuses one outside the suite, so what
    arrives here is recorded as it is, rounded to the tenth of a second.
    """

    recorded = {name: round(seconds, 1) for name, seconds in totals.items()}
    destination.write_text(
        json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def shard(
    files: list[str], durations: dict[str, float], shards: int,
    *, minimum_file_seconds: float = 0.0,
) -> list[list[str]]:
    """Fill the smallest shard with the longest remaining file."""

    if not math.isfinite(minimum_file_seconds) or minimum_file_seconds < 0:
        raise ValueError("minimum file weight must be finite and nonnegative")

    known = [value for value in durations.values() if value > 0]
    average = sum(known) / len(known) if known else 1.0
    weighted = sorted(
        (
            (max(durations.get(name, average), minimum_file_seconds) + COLLECTION_SECONDS, name)
            for name in files
        ),
        key=lambda item: (-item[0], item[1]),
    )
    buckets: list[list[str]] = [[] for _ in range(shards)]
    totals = [0.0] * shards
    for seconds, name in weighted:
        target = totals.index(min(totals))
        buckets[target].append(name)
        totals[target] += seconds
    return buckets
