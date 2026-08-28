"""Operate one exact Document Revision Processing pair with no implicit authority.

This CLI accepts only two explicit positive Extraction Run ids. It always
creates one exact Revision Comparison, immediately reads it back to verify the
sealed content, then applies the released Automatic Support Update Rules for that exact
project. It never infers runs from recency and never accepts human
identity or policy-mutation flags.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from typing import Any

from corridor.automatic_carry_forward import ABSTENTION_REASON_VERSION
from corridor.revision_comparison import RevisionComparisonError
from corridor.revision_processing import process_revision_pair


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revision-process",
        description=(
            "Document Revision Processing: compare and verify one exact "
            "predecessor-successor run pair, then apply the released Automatic "
            "Support Update Rules without changing policy authority."
        ),
    )
    parser.add_argument(
        "predecessor_extraction_run_id",
        type=_positive_int,
        help="exact completed predecessor extraction run id",
    )
    parser.add_argument(
        "successor_extraction_run_id",
        type=_positive_int,
        help="exact completed successor extraction run id",
    )
    return parser


def _finding_counts(findings) -> dict[str, int]:
    return dict(sorted(Counter(finding.state for finding in findings).items()))


def _abstention_counts(abstentions) -> dict[str, int]:
    return dict(sorted(Counter(item.reason for item in abstentions).items()))


def _payload(
    result,
    *,
    predecessor_extraction_run_id: int,
    successor_extraction_run_id: int,
) -> dict[str, Any]:
    comparison = result.comparison.comparison
    abstention_counts = _abstention_counts(result.carry_forward.abstentions)
    return {
        "abstentions": {
            "count": len(result.carry_forward.abstentions),
            "reason_version": ABSTENTION_REASON_VERSION,
            "reasons": abstention_counts,
        },
        "carried_count": len(result.carry_forward.carried),
        "comparison": {
            "content_sha256": comparison.content_sha256,
            "finding_count": comparison.finding_count,
            "finding_counts": _finding_counts(result.comparison.findings),
            "id": comparison.id,
        },
        "predecessor_extraction_run_id": predecessor_extraction_run_id,
        "successor_extraction_run_id": successor_extraction_run_id,
    }


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Run one exact comparison, verify it, and route released-policy support."""

    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if session_factory is None:
        from corridor.db import Session as session_factory

    try:
        with session_factory() as session:
            try:
                result = process_revision_pair(
                    session,
                    predecessor_extraction_run_id=(args.predecessor_extraction_run_id),
                    successor_extraction_run_id=args.successor_extraction_run_id,
                )
                payload = _payload(
                    result,
                    predecessor_extraction_run_id=(args.predecessor_extraction_run_id),
                    successor_extraction_run_id=(args.successor_extraction_run_id),
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
    except RevisionComparisonError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _print_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
