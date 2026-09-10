"""Write the read-only storage baseline without putting file I/O in its core.

The old approach was scattered SQL and informal size claims in design notes,
which left no reproducible artifact for the cutover gate.  This thin adapter
selects exact retained rows or already-sealed output files, delegates all
measurement and digest work to ``storage_baseline``, then writes canonical JSON.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from corridor.db import WorkerSession
from corridor.storage_baseline import (
    BaselineSelection,
    build_storage_baseline,
    canonical_json_bytes,
)


DEFAULT_OUTPUT = Path("artifacts/storage-baseline/dev-corpus.json")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="storage-baseline",
        description=(
            "Measure known permanent copy chains and freeze representative "
            "Report, release, and Extraction Run semantics."
        ),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report-run-id", type=_positive_int)
    parser.add_argument("--release-id", type=_positive_int)
    parser.add_argument("--extraction-run-id", type=_positive_int)
    parser.add_argument("--coordination-report-path", type=Path)
    parser.add_argument("--release-path", type=Path)
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = _parser().parse_args(argv)
    factory = session_factory or WorkerSession
    with factory() as session:
        baseline = build_storage_baseline(
            session,
            selection=BaselineSelection(
                report_run_id=args.report_run_id,
                release_id=args.release_id,
                extraction_run_id=args.extraction_run_id,
                coordination_report_path=args.coordination_report_path,
                release_path=args.release_path,
            ),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(baseline))
    missing = [
        name
        for name, frozen in baseline["representative_outputs"].items()
        if not frozen["available"]
    ]
    print(
        f"storage baseline {baseline['known_duplication_bytes']} bytes; "
        f"cutover target <= {baseline['target']['maximum_cutover_bytes']} bytes; "
        f"sha256 {baseline['sha256']}; wrote {args.output}"
    )
    if missing:
        print(
            "warning: no retained row was available for " + ", ".join(missing),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
