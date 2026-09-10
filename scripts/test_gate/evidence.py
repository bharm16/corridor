"""Strict readers and value checks for timing evidence.

Two copies of the strict JSON reader existed, one in the summary CLI and one in
the GitHub adapter, each refusing duplicate keys and non-finite numbers in its
own words. Evidence that reaches a gate decision is read here, once, so a
receipt, a report, a policy and a logged record are all held to the same rule.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


class EvidenceError(ValueError):
    """A timing measurement cannot support a gate decision."""


def require_object(value: Any, keys: set[str] | frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise EvidenceError(f"{label} must contain exactly {sorted(keys)}")
    return value


def require_integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise EvidenceError(f"{label} must be an integer >= {minimum}")
    return value


def require_seconds(value: Any, label: str) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise EvidenceError(f"{label} must be finite nonnegative seconds")
    return float(value)


def require_identity(value: dict) -> None:
    """A GitHub run identity: decimal run id, positive attempt, full SHA."""
    if not isinstance(value["run_id"], str) or not re.fullmatch(
        r"[1-9][0-9]*", value["run_id"]
    ):
        raise EvidenceError("run_id must be a positive decimal string")
    require_integer(value["run_attempt"], "run_attempt", 1)
    if not isinstance(value["head_sha"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", value["head_sha"]
    ):
        raise EvidenceError("head_sha must be a full lowercase commit SHA")


def loads(text: str, source: str) -> Any:
    """Parse JSON that refuses duplicate fields and non-finite numbers."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvidenceError(f"duplicate JSON field {key} in {source}")
            result[key] = value
        return result

    def constant(value):
        raise EvidenceError(f"non-finite JSON number {value} in {source}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def read_json(path: Path) -> Any:
    return loads(path.read_text(encoding="utf-8"), str(path))
