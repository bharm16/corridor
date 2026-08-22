"""Publish one explicit local Evidence Investigator shadow evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from corridor.db import Session
from corridor.evidence_investigator_evaluation import (
    ALL_STRATA,
    EvaluationRules,
    evaluate_shadow_runs,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="append", required=True, dest="runs")
    parser.add_argument("--human-scores", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-cases", type=int, default=20)
    parser.add_argument("--required-stratum", action="append", dest="strata")
    parser.add_argument("--no-agent-baseline-seconds", type=float)
    parser.add_argument("--max-review-time-ratio", type=float)
    args = parser.parse_args()
    scores = json.loads(args.human_scores.read_text())
    rules = EvaluationRules(
        min_cases=args.min_cases,
        required_strata=tuple(args.strata) if args.strata else ALL_STRATA,
        no_agent_baseline_seconds=args.no_agent_baseline_seconds,
        max_review_time_ratio=args.max_review_time_ratio,
    )
    with Session.begin() as session:
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
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
