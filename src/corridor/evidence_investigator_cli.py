"""Operator-only entry point for one hidden Statement Review Assistant run.

Calling the domain operation directly left transport identity and terminal
receipts to operator convention. This narrow CLI pins both and deliberately
has no coordinator-facing route. The guarded database, JSON receipt and exit
codes are the shared frame in `experimental_command`.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from corridor.config import settings
from corridor.evidence_investigator import InvestigationBudget
from corridor.evidence_investigator_runtime import (
    configured_direct_runtime,
    configured_runtime_identity,
    run_receipted_investigation,
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
            "Run the read-only Statement Review Assistant for one Statement Needing "
            "Clarification. It gathers supported options and cannot make the project decision."
        ),
    )
    parser.add_argument(
        "candidate_id", type=int,
        help="Extracted Proposal id (retained technical argument: candidate_id)",
    )

    def investigate(session, args):
        runtime = configured_direct_runtime(
            model=settings.evidence_investigator_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
        identity = configured_runtime_identity(
            settings.evidence_investigator_model
        )
        receipt = asyncio.run(
            run_receipted_investigation(
                session,
                args.candidate_id,
                runtime=runtime,
                identity=identity,
                budget=InvestigationBudget(),
            )
        )
        return {
            "run_id": receipt.run.public_id,
            "status": receipt.result.status,
            "reason": getattr(receipt.result, "reason", None),
            "packet": (
                asdict(receipt.result.packet)
                if receipt.result.packet is not None
                else None
            ),
            "non_authoritative": True,
        }

    return run_json_command(
        parser,
        argv,
        investigate,
        session_factory=session_factory,
        database_guard=database_guard,
    )


if __name__ == "__main__":
    raise SystemExit(main())
