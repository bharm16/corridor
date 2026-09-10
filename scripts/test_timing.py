"""Summarise a pytest JUnit report into the numbers the feedback budget needs.

#548 records a budget for required PR checks, and #423's lesson was that a
suite regresses again the moment nobody is looking at its cost.  CI evidence
alone cannot name the expensive tests: a total runtime says the job got
slower, not which file did it.  This turns one JUnit report into per-file
totals, diffs two reports so a pull request can show what it added, and with
`--write` refreshes the bootstrap weights.  The report is read by the same
rule as the CI receipt (`scripts/test_gate/junit.py`), so the weights this
writes and the weights CI measures are the same number for the same run.

Usage:
    uv run python scripts/test_timing.py out/timing/non-slow.xml
    uv run python scripts/test_timing.py out/timing/non-slow.xml --against base.xml
    uv run python scripts/test_timing.py out/timing/slow.xml --write tests/durations-slow.json
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.test_gate.junit import measured_cases
from scripts.test_gate.partition import test_files, write_durations


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
        help="record per-file seconds as the partition's bootstrap weights",
    )
    arguments = parser.parse_args(argv)

    if not arguments.report.exists():
        print(f"no report at {arguments.report}", file=sys.stderr)
        return 2

    files = test_files()
    try:
        current = measured_cases(arguments.report, files)[1]
        if arguments.write is not None:
            write_durations(current, arguments.write)
        if arguments.against is None:
            print(_render(current, arguments.limit))
            return 0

        if not arguments.against.exists():
            print(f"no baseline report at {arguments.against}", file=sys.stderr)
            return 2
        print(_render_diff(current, measured_cases(arguments.against, files)[1], arguments.limit))
    except ValueError as error:
        print(f"report cannot weight the current suite: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
