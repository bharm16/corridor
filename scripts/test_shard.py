"""Split the test files into balanced shards for parallel CI runners.

#548 found the required gate CPU-bound: the GitHub runner has four cores, so
redistributing work between xdist workers on one runner cannot help, however
uneven the files are.  More actual CPU can, and GitHub runs matrix jobs on
separate runners.

Balance uses the recorded per-file seconds in ``tests/durations.json``, which
`make test-timing` and ``scripts/test_timing.py --write`` produce together.  A file with no recorded duration is
treated as average rather than free, so a newly added file cannot silently
land in an already-full shard.  Shards are filled longest-first onto whichever
shard is currently smallest, which keeps the slowest file from deciding the
wall clock on its own.

Every named file also costs its shard ``COLLECTION_SECONDS`` whether or not
the gate selects a test from it, because each xdist worker imports every
module named on the command line.  The measurement: a slow shard holding 166
files and one test took 84s in CI, against 12s for a shard holding five files
and no tests at all.  Ignoring that term put all ~160 zero-duration files on
one runner, which is why that shard cost 84s while it proved nothing.

Usage:
    uv run python scripts/test_shard.py --shards 4 --shard 1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DURATIONS = ROOT / "tests" / "durations.json"
SLOW_DURATIONS = ROOT / "tests" / "durations-slow.json"
# What one named test file costs a CI shard in import and collection, before
# any of its tests run (#548).
COLLECTION_SECONDS = 0.5


def test_files() -> list[str]:
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


def shard(files: list[str], durations: dict[str, float], shards: int) -> list[list[str]]:
    """Fill the smallest shard with the longest remaining file."""

    known = [value for value in durations.values() if value > 0]
    average = sum(known) / len(known) if known else 1.0
    weighted = sorted(
        (
            (durations.get(name, average) + COLLECTION_SECONDS, name)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--shard", type=int, required=True, help="1-based")
    parser.add_argument(
        "--slow",
        action="store_true",
        help="balance by the slow gate's recorded seconds",
    )
    arguments = parser.parse_args(argv)

    if arguments.shards < 1 or not 1 <= arguments.shard <= arguments.shards:
        print("shard must be within 1..shards", file=sys.stderr)
        return 2

    durations = recorded_seconds(
        SLOW_DURATIONS if arguments.slow else DURATIONS
    )
    buckets = shard(test_files(), durations, arguments.shards)
    print(" ".join(buckets[arguments.shard - 1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
