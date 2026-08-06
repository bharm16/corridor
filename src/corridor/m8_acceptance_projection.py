"""Shared evidence projectors for M8 acceptance capture and replay."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any


def project_extraction_observation(
    *,
    base_observation: dict[str, Any],
    document,
    stable_inputs: list[dict[str, Any]],
    outcome: str,
    candidate_count: int,
) -> dict[str, Any]:
    """Attach one stable extraction/layout summary to a document observation."""

    observation = deepcopy(base_observation)
    observation["extraction"] = {
        "outcome": outcome,
        "candidate_count": candidate_count,
        "verified_candidates": sum(
            1 for item in stable_inputs if item["citations_verified"]
        ),
        "unverified_candidates": sum(
            1 for item in stable_inputs if not item["citations_verified"]
        ),
        "field_key_shapes": dict(
            Counter(
                ",".join(sorted((item["payload_json"].get("fields") or {})))
                for item in stable_inputs
            )
        ),
    }
    observation["layout"] = {
        "extraction_tier_pages": dict(document.extraction_tiers or {}),
        "header_disagreements": document.header_disagreements or 0,
    }
    return observation
