"""Publish one explicit local Statement Review Assistant shadow evaluation.

Ad-hoc notebooks were rejected because they cannot reproduce an exact selected
run set or seal failed gates. This CLI delegates to the local deterministic
grader and emits both machine-readable and human-readable receipts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from corridor.evidence_investigator_evaluation import (
    REQUIRED_STRATA,
    EvaluationRules,
    evaluate_shadow_runs,
)
from corridor.experimental_database import (
    DatabaseGuard,
    ProductionDatabaseRefusal,
    experimental_session,
    require_experimental_database,
)


def main(
    argv: list[str] | None = None,
    *,
    session_factory=None,
    database_guard: DatabaseGuard = require_experimental_database,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate an exact set of hidden Statement Review Assistant runs and "
            "write local success or failure receipts. This does not enable the assistant in the UI."
        ),
    )
    parser.add_argument("--run", action="append", required=True, dest="runs")
    parser.add_argument("--human-scores", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-cases", type=int, default=20)
    parser.add_argument("--required-stratum", action="append", dest="strata")
    parser.add_argument("--no-agent-baseline-seconds", type=float)
    parser.add_argument("--max-review-time-ratio", type=float)
    parser.add_argument(
        "--database-url",
        required=True,
        help="explicit non-production PostgreSQL database",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        rules = EvaluationRules(
            min_cases=args.min_cases,
            required_strata=tuple(args.strata) if args.strata else REQUIRED_STRATA,
            no_agent_baseline_seconds=args.no_agent_baseline_seconds,
            max_review_time_ratio=args.max_review_time_ratio,
        )
        with experimental_session(
            args.database_url,
            session_factory=session_factory,
            database_guard=database_guard,
        ) as session:
            scores = json.loads(args.human_scores.read_text())
            with session.begin_nested():
                artifact = evaluate_shadow_runs(
                    session,
                    args.runs,
                    rules=rules,
                    human_scores=scores,
                    output_dir=args.output_dir,
                )
                output = {
                    "evaluation_id": artifact.receipt.public_id,
                    "status": artifact.receipt.status,
                    "machine_receipt": str(artifact.machine_path),
                    "summary": str(artifact.summary_path),
                    "ui_enabled": False,
                }
    except (ProductionDatabaseRefusal, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
