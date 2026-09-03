"""Summarise a pytest JUnit report into the numbers the feedback budget needs.

#548 records a budget for required PR checks, and #423's lesson was that a
suite regresses again the moment nobody is looking at its cost.  CI evidence
alone cannot name the expensive tests: a total runtime says the job got
slower, not which file did it.  This turns one JUnit report into per-file and
per-shard totals, and diffs two reports so a pull request can show what it
added.

Usage:
    uv run python scripts/test_timing.py out/timing/non-slow.xml
    uv run python scripts/test_timing.py out/timing/non-slow.xml --against base.xml
    uv run python scripts/test_timing.py out/timing/slow.xml --write tests/durations-slow.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]


def per_file_seconds(report: Path) -> dict[str, float]:
    """Total wall-clock seconds per test file, from one JUnit report."""

    totals: dict[str, float] = defaultdict(float)
    for case in ElementTree.parse(report).iter("testcase"):
        # `file` is the path pytest recorded; `classname` is the fallback for
        # reports that omit it.
        name = case.get("file") or case.get("classname", "").replace(".", "/")
        if not name.endswith(".py"):
            name = f"{name}.py"
        totals[name] += float(case.get("time", 0.0))
    return dict(totals)


def write_durations(totals: dict[str, float], destination: Path) -> None:
    """Record per-file seconds for every test file, zero included.

    `scripts/test_shard.py` treats a file with no recorded duration as
    *average*, so an omitted file is not free — it is imaginary work that
    unbalances the partition. Ten files added after the last hand-written
    recording were each counted as 52.8 imaginary slow seconds, which is how
    one slow shard ended up running no tests at all (#548). Every file gets an
    entry, and a file the gate does not select gets an explicit 0.0.
    """

    recorded = {
        str(path.relative_to(ROOT)): round(
            totals.get(str(path.relative_to(ROOT)), 0.0), 1
        )
        for path in sorted((ROOT / "tests").glob("test_*.py"))
    }
    unknown = sorted(set(totals) - set(recorded))
    if unknown:
        raise SystemExit(f"report names files that are not test files: {unknown}")
    destination.write_text(
        json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _render(totals: dict[str, float], limit: int) -> str:
    ranked = sorted(totals.items(), key=lambda item: -item[1])
    total = sum(totals.values())
    lines = [f"total {total:8.1f}s across {len(totals)} files", ""]
    for name, seconds in ranked[:limit]:
        share = 100 * seconds / total if total else 0
        lines.append(f"{seconds:8.1f}s  {share:5.1f}%  {name}")
    return "\n".join(lines)


def _render_diff(current: dict[str, float], base: dict[str, float], limit: int) -> str:
    names = set(current) | set(base)
    deltas = {
        name: current.get(name, 0.0) - base.get(name, 0.0) for name in names
    }
    ranked = sorted(deltas.items(), key=lambda item: -abs(item[1]))
    lines = [
        f"total {sum(current.values()):.1f}s vs {sum(base.values()):.1f}s "
        f"({sum(current.values()) - sum(base.values()):+.1f}s)",
        "",
    ]
    for name, delta in ranked[:limit]:
        if abs(delta) < 0.05:
            continue
        lines.append(f"{delta:+8.1f}s  {name}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--against", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument(
        "--write",
        type=Path,
        default=None,
        help="record per-file seconds for scripts/test_shard.py",
    )
    arguments = parser.parse_args(argv)

    if not arguments.report.exists():
        print(f"no report at {arguments.report}", file=sys.stderr)
        return 2

    current = per_file_seconds(arguments.report)
    if arguments.write is not None:
        write_durations(current, arguments.write)
    if arguments.against is None:
        print(_render(current, arguments.limit))
        return 0

    if not arguments.against.exists():
        print(f"no baseline report at {arguments.against}", file=sys.stderr)
        return 2
    print(_render_diff(current, per_file_seconds(arguments.against), arguments.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
