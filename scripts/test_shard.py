"""Split the test files into balanced shards for parallel CI runners.

#548 found the required gate CPU-bound: the GitHub runner has four cores, so
redistributing work between xdist workers on one runner cannot help, however
uneven the files are.  More actual CPU can, and GitHub runs matrix jobs on
separate runners.

Balance uses the recorded per-file seconds in ``tests/durations.json``, which
`make test-timing` produces.  A file with no recorded duration is treated as
average rather than free, so a newly added file cannot silently land in an
already-full shard.  Shards are filled longest-first onto whichever shard is
currently smallest, which keeps the slowest file from deciding the wall clock
on its own.

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


def test_files() -> list[str]:
    return sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "tests").glob("test_*.py")
    )


def recorded_seconds() -> dict[str, float]:
    if not DURATIONS.exists():
        return {}
    return json.loads(DURATIONS.read_text(encoding="utf-8"))


def shard(files: list[str], durations: dict[str, float], shards: int) -> list[list[str]]:
    """Fill the smallest shard with the longest remaining file."""

    known = [value for value in durations.values() if value > 0]
    average = sum(known) / len(known) if known else 1.0
    weighted = sorted(
        ((durations.get(name, average), name) for name in files),
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
    arguments = parser.parse_args(argv)

    if arguments.shards < 1 or not 1 <= arguments.shard <= arguments.shards:
        print("shard must be within 1..shards", file=sys.stderr)
        return 2

    buckets = shard(test_files(), recorded_seconds(), arguments.shards)
    print(" ".join(buckets[arguments.shard - 1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
