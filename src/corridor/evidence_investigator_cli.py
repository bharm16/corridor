"""Operator-only entry point for one hidden, receipted investigation.

Calling the domain operation directly left transport identity and terminal
receipts to operator convention. This narrow CLI pins both and deliberately
has no coordinator-facing route.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from corridor.config import settings
from corridor.db import Session
from corridor.evidence_investigator import InvestigationBudget
from corridor.evidence_investigator_runtime import (
    ADAPTER,
    PROMPT_VERSION,
    TRANSPORT_GATE,
    DirectResponsesInvestigationRuntime,
    RuntimeIdentity,
    run_receipted_investigation,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate_id", type=int)
    args = parser.parse_args()
    runtime = DirectResponsesInvestigationRuntime(
        model=settings.evidence_investigator_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    identity = RuntimeIdentity(
        adapter=ADAPTER,
        model=settings.evidence_investigator_model,
        prompt_version=PROMPT_VERSION,
        transport_gate_sha256=TRANSPORT_GATE["sha256"],
    )
    with Session.begin() as session:
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
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
