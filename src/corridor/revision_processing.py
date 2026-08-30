"""Converge on an exact Revision Comparison, verify it, then route automation.

This service owns the production ordering. A Revision Comparison remains an
immutable, Ledger-write-free receipt. Only after that receipt can be read back
and verified does this layer hand control to the released Carry-Forward Policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from corridor.automatic_carry_forward import (
    AutomaticCarryForwardResult,
    AutomaticCarryForwardRuntime,
    run_automatic_carry_forward,
)
from corridor.revision_comparison import (
    DEFAULT_MATCHER_CONFIG,
    DEFAULT_MATCHER_VERSION,
    RevisionComparisonReadback,
    create_revision_comparison,
    list_revision_comparisons,
    read_revision_comparison,
)


@dataclass(frozen=True)
class RevisionProcessingResult:
    """One verified comparison plus any released-policy support transfer."""

    comparison: RevisionComparisonReadback
    carry_forward: AutomaticCarryForwardResult


@dataclass(frozen=True)
class VerifiedRevisionPair:
    """One created-or-reused comparison after integrity readback."""

    comparison: RevisionComparisonReadback
    created: bool


def obtain_verified_revision_pair(
    session: Session,
    *,
    predecessor_extraction_run_id: int,
    successor_extraction_run_id: int,
    matcher_version: str = DEFAULT_MATCHER_VERSION,
    matcher_config: dict[str, Any] | None = None,
) -> VerifiedRevisionPair:
    """Create or reuse one exact comparison and verify its retained bytes."""
    existing_ids = {
        comparison.id
        for comparison in list_revision_comparisons(
            session,
            predecessor_extraction_run_id,
            successor_extraction_run_id,
        )
    }
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=predecessor_extraction_run_id,
        successor_extraction_run_id=successor_extraction_run_id,
        matcher_version=matcher_version,
        matcher_config=(
            matcher_config if matcher_config is not None else DEFAULT_MATCHER_CONFIG
        ),
        require_unambiguous_pair_history=True,
    )
    return VerifiedRevisionPair(
        comparison=read_revision_comparison(session, comparison.id),
        created=comparison.id not in existing_ids,
    )


def process_revision_pair(
    session: Session,
    *,
    predecessor_extraction_run_id: int,
    successor_extraction_run_id: int,
    matcher_version: str = DEFAULT_MATCHER_VERSION,
    matcher_config: dict[str, Any] | None = None,
    automatic_carry_forward_runtime: AutomaticCarryForwardRuntime | None = None,
) -> RevisionProcessingResult:
    """Create or reuse, read-verify, then route Carry-Forward for one pair."""

    verified = obtain_verified_revision_pair(
        session,
        predecessor_extraction_run_id=predecessor_extraction_run_id,
        successor_extraction_run_id=successor_extraction_run_id,
        matcher_version=matcher_version,
        matcher_config=matcher_config,
    )
    carry_forward = run_automatic_carry_forward(
        session,
        verified.comparison.comparison.project_id,
        _runtime=automatic_carry_forward_runtime,
    )
    return RevisionProcessingResult(
        comparison=verified.comparison,
        carry_forward=carry_forward,
    )
