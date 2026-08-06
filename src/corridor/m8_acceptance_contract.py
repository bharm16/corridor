"""Shared result and failure contracts for the M8 acceptance harness.

The replay and controlled lanes live in separate modules, but publish one
assertion vocabulary and one deliberately narrow claim boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CLAIM_BOUNDARY = {
    "mechanical_correctness_only": True,
    "semantic_correctness": False,
    "recall": False,
    "human_review": False,
    "production_readiness": False,
    "independent_customer_validation": False,
}


class AcceptanceError(RuntimeError):
    """The requested acceptance operation cannot be completed honestly."""


@dataclass(frozen=True)
class AssertionResult:
    name: str
    passed: bool
    observed: Any = None
    expected: Any = None
    detail: str | None = None


class ControlledContradiction(AcceptanceError):
    """A controlled-lane claim failed after partial evidence existed."""

    def __init__(
        self,
        assertion: AssertionResult,
        *,
        controlled_raw: dict[str, Any],
        controlled_canonical: dict[str, Any],
    ) -> None:
        super().__init__(assertion.detail or assertion.name)
        self.assertion = assertion
        self.controlled_raw = controlled_raw
        self.controlled_canonical = controlled_canonical
