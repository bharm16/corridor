"""Operator-only entry point for one hidden Statement Review Assistant run.

Calling the domain operation directly left transport identity and terminal
receipts to operator convention. This narrow CLI pins both and deliberately
has no coordinator-facing route.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
import sys

from corridor.config import settings
from corridor.evidence_investigator import InvestigationBudget
from corridor.evidence_investigator_runtime import (
    configured_direct_runtime,
    configured_runtime_identity,
    run_receipted_investigation,
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
            "Run the read-only Statement Review Assistant for one Statement Needing "
            "Clarification. It gathers supported options and cannot make the project decision."
        ),
    )
    parser.add_argument(
        "candidate_id", type=int,
        help="Extracted Proposal id (retained technical argument: candidate_id)",
    )
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
        with experimental_session(
            args.database_url,
            session_factory=session_factory,
            database_guard=database_guard,
        ) as session:
            runtime = configured_direct_runtime(
                model=settings.evidence_investigator_model,
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            )
            identity = configured_runtime_identity(
                settings.evidence_investigator_model
            )
            with session.begin_nested():
                receipt = asyncio.run(
                    run_receipted_investigation(
                        session,
                        args.candidate_id,
                        runtime=runtime,
                        identity=identity,
                        budget=InvestigationBudget(),
                    )
                )
                output = {
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
    except (ProductionDatabaseRefusal, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
