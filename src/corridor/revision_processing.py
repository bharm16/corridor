"""Create an exact Revision Comparison, verify it, then route automation.

This service owns the production ordering. A Revision Comparison remains an
immutable, Ledger-write-free receipt. Only after that receipt can be read back
and verified does this layer hand control to already-authorized Automatic
Carry-Forward for the affected project.
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
    read_revision_comparison,
)


@dataclass(frozen=True)
class RevisionProcessingResult:
    """One verified comparison plus any authorized machine support transfer."""

    comparison: RevisionComparisonReadback
    carry_forward: AutomaticCarryForwardResult


def process_revision_pair(
    session: Session,
    *,
    predecessor_extraction_run_id: int,
    successor_extraction_run_id: int,
    matcher_version: str = DEFAULT_MATCHER_VERSION,
    matcher_config: dict[str, Any] | None = None,
    automatic_carry_forward_runtime: AutomaticCarryForwardRuntime | None = None,
) -> RevisionProcessingResult:
    """Create, read-verify, then route authorized Carry-Forward for one pair."""

    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=predecessor_extraction_run_id,
        successor_extraction_run_id=successor_extraction_run_id,
        matcher_version=matcher_version,
        matcher_config=(
            matcher_config
            if matcher_config is not None
            else DEFAULT_MATCHER_CONFIG
        ),
    )
    readback = read_revision_comparison(session, comparison.id)
    carry_forward = run_automatic_carry_forward(
        session,
        readback.comparison.project_id,
        _runtime=automatic_carry_forward_runtime,
    )
    return RevisionProcessingResult(
        comparison=readback,
        carry_forward=carry_forward,
    )
