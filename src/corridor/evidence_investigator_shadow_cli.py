"""Operator commands for the prospective hidden investigator cohort.

Retrospective replay was rejected because it can expose later answers. These
commands allow only prospective batch freezing and exact later label capture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from corridor.config import settings
from corridor.db import Session
from corridor.evidence_investigator import InvestigationBudget
from corridor.evidence_investigator_runtime import (
    DirectResponsesInvestigationRuntime,
    configured_runtime_identity,
)
from corridor.evidence_investigator_shadow import (
    capture_shadow_outcome,
    run_v2_shadow_cohort,
    write_shadow_cohort_manifest,
)
from corridor.models import Project


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("project_slug")
    run.add_argument("--candidate-id", type=int, action="append", required=True)
    run.add_argument("--selection-rule", required=True)
    run.add_argument("--manifest-path", type=Path, required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("shadow_case_id")
    args = parser.parse_args()
    with Session.begin() as session:
        if args.command == "capture":
            outcome = capture_shadow_outcome(session, args.shadow_case_id)
            output = {
                "shadow_case_id": args.shadow_case_id,
                "outcome_sha256": outcome.outcome_sha256,
                "strata": outcome.strata_json,
                "unresolved": outcome.unresolved,
            }
        else:
            project = session.scalar(
                select(Project).where(Project.slug == args.project_slug)
            )
            if project is None:
                raise SystemExit(f"unknown project {args.project_slug!r}")
            identity = configured_runtime_identity(
                settings.evidence_investigator_model
            )

            def runtime_factory():
                return DirectResponsesInvestigationRuntime(
                    model=settings.evidence_investigator_model,
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                )

            cohort = asyncio.run(
                run_v2_shadow_cohort(
                    session,
                    project.id,
                    candidate_ids=tuple(args.candidate_id),
                    selection_rule=args.selection_rule,
                    runtime_factory=runtime_factory,
                    identity=identity,
                    budget=InvestigationBudget(),
                )
            )
            write_shadow_cohort_manifest(cohort, args.manifest_path)
            output = {
                "project": project.slug,
                "cohort_id": cohort.manifest["cohort_id"],
                "manifest_path": str(args.manifest_path),
                "manifest_sha256": cohort.manifest["manifest_sha256"],
                "cases": [
                    {
                        "shadow_case_id": item.case.public_id,
                        "run_id": item.investigation.run.public_id,
                        "status": item.execution.execution_status,
                    }
                    for item in cohort.results
                ],
                "hidden": True,
            }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
