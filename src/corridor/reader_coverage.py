"""Semantic equivalence with explicit records, fields and read provenance.

The former gate changed two stationing fields inside an otherwise legacy
snapshot, then called four identical renders proof of a native reader. This
module compares the complete declared contract, including missing/extra records
and field origins. Render equality remains a diagnostic until coverage passes.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SurfaceContract:
    name: str
    record_kinds: frozenset[str]
    fields: frozenset[str]
    unchanged_output: bool = False


# Seven ADR-0081 surfaces. Briefing and check evaluation are explicit members of
# the report and Constraint Log contracts, not silently absent eighth readers.
CONTRACTS = (
    SurfaceContract("constraint_log", frozenset({"constraint", "check"}), frozenset({"identity", "accepted_values", "source_support", "coordination", "check_results"})),
    SurfaceContract("work_list", frozenset({"constraint_work", "statement_work", "proposed_delta"}), frozenset({"identity", "band", "scope", "assignment", "next_action", "deferral", "disposition"})),
    SurfaceContract("current_and_as_of_record", frozenset({"current", "as_of"}), frozenset({"identity", "revision", "accepted_values", "authority", "source_support"})),
    SurfaceContract("coordination_report", frozenset({"report", "briefing"}), frozenset({"population", "accepted_values", "coordination", "checks", "statements", "citations"})),
    SurfaceContract("workbook_export", frozenset({"workbook", "customer_format"}), frozenset({"population", "accepted_values", "coordination", "statements", "source_support"}), True),
    SurfaceContract("report_release", frozenset({"release"}), frozenset({"population", "accepted_values", "validation", "approval", "citations"}), True),
    SurfaceContract("source_and_decision_history", frozenset({"source", "decision", "correction", "reversal", "support"}), frozenset({"identity", "original_actor", "original_time", "decision_type", "source_identity", "predecessor"})),
)


@dataclass(frozen=True)
class SemanticRecord:
    kind: str
    key: str
    fields: dict[str, Any]
    # A native origin names the deciding revision/source identity, not simply
    # a boolean. Compatibility-carried fields remain explicitly ineligible.
    field_origins: dict[str, str]


@dataclass(frozen=True)
class SurfaceReading:
    surface: str
    records: tuple[SemanticRecord, ...]
    # Empty classes need explicit population proof; omission is not zero rows.
    observed_record_kinds: frozenset[str]
    output_identity: str | None = None


@dataclass(frozen=True)
class CoverageResult:
    surface: str
    passed: bool
    compared_records: int
    blockers: tuple[str, ...]


def compare_surface(contract: SurfaceContract, legacy: SurfaceReading,
                    native: SurfaceReading) -> CoverageResult:
    """Compare the declared semantic contract without forgiving absent output."""
    blockers = []
    if legacy.surface != contract.name or native.surface != contract.name:
        blockers.append("reading belongs to a different surface")
    for side, reading in (("legacy", legacy), ("native", native)):
        if reading.observed_record_kinds != contract.record_kinds:
            blockers.append(f"{side} record-class population coverage is incomplete or unexpected")
    left = {(row.kind, row.key): row for row in legacy.records}
    right = {(row.kind, row.key): row for row in native.records}
    if len(left) != len(legacy.records) or len(right) != len(native.records):
        blockers.append("record identity is duplicated")
    if set(left) != set(right):
        blockers.append("record populations differ")
    for key in sorted(set(left) | set(right)):
        for side, rows in (("legacy", left), ("native", right)):
            row = rows.get(key)
            if row is None:
                continue
            if row.kind not in contract.record_kinds or set(row.fields) != contract.fields:
                blockers.append(f"{side} {key}: field or record inventory differs")
            if side == "native" and (
                set(row.field_origins) != contract.fields
                or any(not isinstance(origin, str) or not origin.startswith(("revision:", "source_segment:", "native_decision:"))
                       or not origin.split(":", 1)[1].strip() for origin in row.field_origins.values())
            ):
                blockers.append(f"native {key}: fields lack native decision/source lineage")
        if key in left and key in right and left[key].fields != right[key].fields:
            blockers.append(f"{key}: semantic values differ")
    if contract.unchanged_output and (
        not legacy.output_identity or legacy.output_identity != native.output_identity
    ):
        blockers.append("intentionally unchanged output differs or was not compared")
    return CoverageResult(contract.name, not blockers, len(set(left) & set(right)), tuple(blockers))


def compare_all_surfaces(legacy: tuple[SurfaceReading, ...],
                         native: tuple[SurfaceReading, ...]) -> tuple[CoverageResult, ...]:
    """Every one of the seven surfaces must be present exactly once."""
    results = []
    names = {contract.name for contract in CONTRACTS}
    unknown = (set(row.surface for row in legacy) | set(row.surface for row in native)) - names
    for contract in CONTRACTS:
        before = [row for row in legacy if row.surface == contract.name]
        after = [row for row in native if row.surface == contract.name]
        if len(before) != 1 or len(after) != 1 or unknown:
            results.append(CoverageResult(contract.name, False, 0,
                ("surface is missing, duplicated, or undeclared",)))
        else:
            results.append(compare_surface(contract, before[0], after[0]))
    return tuple(results)
