"""Operator commands for the prospective hidden investigator cohort."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from corridor.config import settings
from corridor.db import Session
from corridor.evidence_investigator import InvestigationBudget
from corridor.evidence_investigator_runtime import (
    ADAPTER,
    PROMPT_VERSION,
    TRANSPORT_GATE,
    DirectResponsesInvestigationRuntime,
    RuntimeIdentity,
)
from corridor.evidence_investigator_shadow import (
    capture_shadow_outcome,
    run_shadow_batch,
)
from corridor.models import Project


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("project_slug")
    run.add_argument("--limit", type=int, default=25)
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
            identity = RuntimeIdentity(
                adapter=ADAPTER,
                model=settings.evidence_investigator_model,
                prompt_version=PROMPT_VERSION,
                transport_gate_sha256=TRANSPORT_GATE["sha256"],
            )

            def runtime_factory():
                return DirectResponsesInvestigationRuntime(
                    model=settings.evidence_investigator_model,
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                )

            results = asyncio.run(
                run_shadow_batch(
                    session,
                    project.id,
                    runtime_factory=runtime_factory,
                    identity=identity,
                    budget=InvestigationBudget(),
                    limit=args.limit,
                )
            )
            output = {
                "project": project.slug,
                "cases": [
                    {
                        "shadow_case_id": item.case.public_id,
                        "run_id": item.investigation.run.public_id,
                        "status": item.execution.execution_status,
                    }
                    for item in results
                ],
                "hidden": True,
            }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
