"""Publish one explicit local Statement Review Assistant shadow evaluation.

Ad-hoc notebooks were rejected because they cannot reproduce an exact selected
run set or seal failed gates. This CLI delegates to the local deterministic
grader and emits both machine-readable and human-readable receipts. The
guarded database, JSON receipt and exit codes are the shared frame in
`experimental_command`.
"""

from __future__ import annotations

import json
from pathlib import Path

from corridor.evidence_investigator_evaluation import (
    REQUIRED_STRATA,
    EvaluationRules,
    evaluate_shadow_runs,
)
from corridor.experimental_command import experiment_parser, run_json_command
from corridor.experimental_database import (
    DatabaseGuard,
    require_experimental_database,
)


def main(
    argv: list[str] | None = None,
    *,
    session_factory=None,
    database_guard: DatabaseGuard = require_experimental_database,
) -> int:
    parser = experiment_parser(
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

    def evaluate(session, args):
        rules = EvaluationRules(
            min_cases=args.min_cases,
            required_strata=tuple(args.strata) if args.strata else REQUIRED_STRATA,
            no_agent_baseline_seconds=args.no_agent_baseline_seconds,
            max_review_time_ratio=args.max_review_time_ratio,
        )
        scores = json.loads(args.human_scores.read_text())
        artifact = evaluate_shadow_runs(
            session,
            args.runs,
            rules=rules,
            human_scores=scores,
            output_dir=args.output_dir,
        )
        return {
            "evaluation_id": artifact.receipt.public_id,
            "status": artifact.receipt.status,
            "machine_receipt": str(artifact.machine_path),
            "summary": str(artifact.summary_path),
            "ui_enabled": False,
        }

    return run_json_command(
        parser,
        argv,
        evaluate,
        session_factory=session_factory,
        database_guard=database_guard,
    )


if __name__ == "__main__":
    raise SystemExit(main())
