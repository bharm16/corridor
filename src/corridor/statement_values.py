"""Dependency-free values shared by statement readers and writers.

Candidate preparation and durable statement recording both need the exact same
timing, scope, and cited-Evidence shapes.  Keeping those values below both
modules lets each depend on stable domain vocabulary without either importing
the other's implementation.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
import re


class StatementRefusal(ValueError):
    """The proposed statement would manufacture a fact the record lacks."""


def reports_completion(text: str) -> bool:
    """An explicit report of completion, with negation/future language unresolved."""
    return bool(re.search(r"\b(?:completed|complete|finished)\b", text, re.I)) and not bool(
        re.search(r"\b(?:not|incomplete|will|would|could|should|may|might|if|expect\w*)\b", text, re.I))


def states_unknown_scope(text: str) -> bool:
    return bool(re.search(r"\b(?:scope|applies to)\s*(?::|is)?\s*(?:not yet known|unknown|TBD)\b", text, re.I))


@dataclass(frozen=True)
class StatementTiming:
    """One timing exactly as the External Party stated it."""

    text: str
    precision: str
    start_date: date | None
    end_date: date | None

    @classmethod
    def day(cls, text: str, value: date) -> "StatementTiming":
        return cls(text=text, precision="day", start_date=value, end_date=value)

    @classmethod
    def month(cls, text: str, year: int, month: int) -> "StatementTiming":
        return cls(
            text=text,
            precision="month",
            start_date=date(year, month, 1),
            end_date=date(year, month, monthrange(year, month)[1]),
        )

    @classmethod
    def approximate(cls, text: str) -> "StatementTiming":
        return cls(text=text, precision="approximate", start_date=None, end_date=None)


@dataclass(frozen=True)
class StatementScope:
    """The attributable scope decision, never inferred from party context."""

    mode: str
    dependency_ids: tuple[int, ...] = ()

    @classmethod
    def unknown(cls) -> "StatementScope":
        return cls("unknown")

    @classmethod
    def selected(cls, dependency_ids: tuple[int, ...] | list[int]) -> "StatementScope":
        return cls("selected", tuple(dependency_ids))

    @classmethod
    def all_active(cls) -> "StatementScope":
        return cls("all_active")


@dataclass(frozen=True)
class CitedStatementEvidence:
    """The one page citation owned by a cited event rather than a Dependency."""

    document_id: int
    page_no: int
    quote: str
