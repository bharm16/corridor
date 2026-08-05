"""Immutable exact-run Revision Comparisons and global row correspondence.

A Revision Comparison is a receipt, not a live worklist (ADR-0018).  It pins
two declared-successor documents, their exact completed Extraction Runs, the
matcher version and full configuration, then snapshots every Candidate input
and persists the resulting findings.  Nothing here writes to the Ledger.

The matcher deliberately does not call ``merge.rank_matches``.  Merge is an
asymmetric Candidate-to-Dependency suggestion contract; revision comparison
builds a sparse Candidate-to-Candidate graph, marks near-equal alternatives as
ambiguous, and solves maximum-cardinality / maximum-weight one-to-one
correspondence globally inside each connected component.

Version 1 deliberately supports only utility-matrix ``dependency`` rows.
Agreement obligations and minutes events have different identity contracts
that have not been calibrated on a real revision chain.  A non-empty run
containing either shape fails closed before any receipt is written.  For two
non-empty matrix runs, an unmatched row is classified as added or dropped only
when it has a parseable station and meaningful baseline; incomplete identity is
persisted as a side-specific ``unmatched`` finding. Exact-id-only runs remain
usable when all rows correspond, and an honestly completed zero-row run remains
a valid exact input.
"""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import heapq
import json
from math import inf, isfinite
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.extraction_runs import is_completed_run
from corridor.merge import parse_station
from corridor.models import (
    Candidate,
    Document,
    ExtractionRun,
    is_placeholder_party,
    RevisionComparisonFinding,
    RevisionComparisonRun,
)
from corridor.project_lock import lock_project
from corridor.verify import normalize


DEFAULT_MATCHER_VERSION = "revision-correspondence-v1"
DEFAULT_MATCHER_CONFIG: dict[str, Any] = {
    "minimum_score": 0.60,
    "ambiguity_margin": 0.04,
    "station_tolerance_ft": 500.0,
    "minimum_station_identity": 0.80,
    "minimum_location_identity": 0.85,
    "max_ambiguity_rows": 20,
    "max_ambiguity_alternatives": 32,
    "normalization": "nfkc-casefold-whitespace-v1",
    "baseline_normalization": "alphanumeric-road-id-v1",
    "field_comparison": "typed-baseline-station-v1",
    "weights": {
        "station": 4.0,
        "source_ref": 2.0,
        "owner": 2.0,
        "type": 1.0,
        "location": 1.0,
    },
}

_CONFIG_KEYS = frozenset(DEFAULT_MATCHER_CONFIG)
_WEIGHT_KEYS = frozenset(DEFAULT_MATCHER_CONFIG["weights"])
_COST_SCALE = 1_000_000
_SCORE_EPSILON = 1e-9


class RevisionComparisonError(ValueError):
    """The requested exact-run comparison cannot be produced honestly."""


class MissingExtractionRun(RevisionComparisonError):
    """One of the exact Extraction Run ids does not exist."""


class InvalidRevisionPair(RevisionComparisonError):
    """The documents are not one declared predecessor-successor edge."""


class IncompletePredecessorExtraction(RevisionComparisonError):
    """The predecessor input is not a successfully completed extraction."""


class IncompleteSuccessorExtraction(RevisionComparisonError):
    """The successor input cannot establish added or dropped rows."""


class CorruptRevisionComparison(RevisionComparisonError):
    """Persisted receipt content no longer agrees with its sealed digest."""


class InexactExtractionInputs(RevisionComparisonError):
    """A legacy run has no provably extractor-time Candidate snapshot."""


class UnsupportedComparisonShape(RevisionComparisonError):
    """An exact run contains rows outside this matcher's calibrated scope."""


@dataclass(frozen=True)
class RevisionComparisonReadback:
    """A receipt plus the exact snapshots and findings a reviewer reads."""

    comparison: RevisionComparisonRun
    findings: tuple[RevisionComparisonFinding, ...]
    predecessor_inputs: tuple[dict[str, Any], ...]
    successor_inputs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _RevisionRow:
    candidate_id: int
    kind: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class _ScoredEdge:
    predecessor_id: int
    successor_id: int
    score: float
    signals: tuple[dict[str, Any], ...]
    strong_identity: bool = True


@dataclass(frozen=True)
class _FindingDraft:
    state: str
    predecessor_ids: tuple[int, ...]
    successor_ids: tuple[int, ...]
    match_score: float | None
    field_changes: tuple[dict[str, Any], ...]
    matcher_detail: dict[str, Any]

    def content(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "predecessor_candidate_ids": list(self.predecessor_ids),
            "successor_candidate_ids": list(self.successor_ids),
            "match_score": self.match_score,
            "field_changes": list(self.field_changes),
            "matcher_detail": self.matcher_detail,
        }


@dataclass
class _FlowEdge:
    to: int
    reverse: int
    capacity: int
    cost: int
    pair: tuple[int, int] | None = None


def create_revision_comparison(
    session: Session,
    predecessor_extraction_run_id: int,
    successor_extraction_run_id: int,
    *,
    matcher_version: str = DEFAULT_MATCHER_VERSION,
    matcher_config: dict[str, Any] | None = None,
) -> RevisionComparisonRun:
    """Append one completed Revision Comparison from two explicit run ids.

    The successor must be the registry-declared successor and both runs must
    satisfy the extraction completion predicate.  In particular, no receipt
    is written for an absent or failed successor, because an empty failed run
    is not evidence that predecessor rows were dropped.
    """

    if not isinstance(matcher_version, str) or not matcher_version.strip():
        raise RevisionComparisonError("matcher_version must be a non-empty string")
    config = _resolved_config(matcher_config)

    with session.begin_nested():
        predecessor_run = _run(session, predecessor_extraction_run_id)
        successor_run = _run(session, successor_extraction_run_id)
        predecessor_document = session.get(Document, predecessor_run.document_id)
        successor_document = session.get(Document, successor_run.document_id)
        if predecessor_document is None or successor_document is None:
            raise InvalidRevisionPair("an Extraction Run has no registered document")
        if predecessor_document.project_id != successor_document.project_id:
            raise InvalidRevisionPair("a Revision Comparison cannot cross projects")

        lock_project(session, predecessor_document.project_id)
        predecessor_document = session.get(
            Document, predecessor_document.id, populate_existing=True
        )
        successor_document = session.get(
            Document, successor_document.id, populate_existing=True
        )
        predecessor_run = session.get(
            ExtractionRun, predecessor_run.id, populate_existing=True
        )
        successor_run = session.get(
            ExtractionRun, successor_run.id, populate_existing=True
        )
        if (
            predecessor_document is None
            or successor_document is None
            or predecessor_run is None
            or successor_run is None
        ):
            raise InvalidRevisionPair("the requested revision inputs disappeared")
        if predecessor_document.superseded_by != successor_document.id:
            raise InvalidRevisionPair(
                "documents are not an authority-declared predecessor-successor pair"
            )
        if not is_completed_run(predecessor_run):
            raise IncompletePredecessorExtraction(
                "predecessor extraction must have completed successfully"
            )
        if not is_completed_run(successor_run):
            raise IncompleteSuccessorExtraction(
                "successor extraction must have completed successfully before "
                "rows can be classified as dropped or vanished"
            )

        predecessor_inputs = _run_inputs(
            session, predecessor_document, predecessor_run
        )
        successor_inputs = _run_inputs(
            session, successor_document, successor_run
        )
        _validate_supported_inputs(
            predecessor_document, predecessor_inputs, side="predecessor"
        )
        _validate_supported_inputs(
            successor_document, successor_inputs, side="successor"
        )
        findings = _compare_inputs(
            predecessor_inputs,
            successor_inputs,
            config,
        )

        content = _receipt_content(
            project_id=predecessor_document.project_id,
            predecessor_document_id=predecessor_document.id,
            successor_document_id=successor_document.id,
            predecessor_run=predecessor_run,
            successor_run=successor_run,
            matcher_version=matcher_version.strip(),
            matcher_config=config,
            predecessor_inputs=predecessor_inputs,
            successor_inputs=successor_inputs,
            findings=[finding.content() for finding in findings],
        )
        comparison = RevisionComparisonRun(
            project_id=predecessor_document.project_id,
            predecessor_document_id=predecessor_document.id,
            successor_document_id=successor_document.id,
            predecessor_extraction_run_id=predecessor_run.id,
            successor_extraction_run_id=successor_run.id,
            predecessor_schema_version=predecessor_run.schema_version,
            successor_schema_version=successor_run.schema_version,
            predecessor_prompt_version=predecessor_run.prompt_version,
            successor_prompt_version=successor_run.prompt_version,
            predecessor_model=predecessor_run.model,
            successor_model=successor_run.model,
            matcher_version=matcher_version.strip(),
            matcher_config=config,
            predecessor_inputs_json=predecessor_inputs,
            successor_inputs_json=successor_inputs,
            finding_count=len(findings),
            content_sha256=_content_sha256(content),
        )
        session.add(comparison)
        session.flush([comparison])
        for ordinal, finding in enumerate(findings, start=1):
            session.add(
                RevisionComparisonFinding(
                    revision_comparison_run_id=comparison.id,
                    ordinal=ordinal,
                    state=finding.state,
                    predecessor_candidate_ids=list(finding.predecessor_ids),
                    successor_candidate_ids=list(finding.successor_ids),
                    match_score=finding.match_score,
                    field_changes=list(finding.field_changes),
                    matcher_detail=finding.matcher_detail,
                )
            )
        session.flush()
        comparison.sealed_at = datetime.now(timezone.utc)
        session.flush([comparison])
        return comparison


def read_revision_comparison(
    session: Session, comparison_id: int
) -> RevisionComparisonReadback:
    """Read and integrity-check one persisted receipt and its exact inputs."""

    comparison = session.get(RevisionComparisonRun, comparison_id)
    if comparison is None:
        raise RevisionComparisonError(
            f"Revision Comparison {comparison_id} does not exist"
        )
    if comparison.sealed_at is None:
        raise CorruptRevisionComparison(
            f"Revision Comparison {comparison.id} is not sealed"
        )
    findings = tuple(
        session.scalars(
            select(RevisionComparisonFinding)
            .where(
                RevisionComparisonFinding.revision_comparison_run_id == comparison.id
            )
            .order_by(RevisionComparisonFinding.ordinal)
        ).all()
    )
    if len(findings) != comparison.finding_count:
        raise CorruptRevisionComparison(
            f"Revision Comparison {comparison.id} expected "
            f"{comparison.finding_count} findings but stored {len(findings)}"
        )
    content = {
        "project_id": comparison.project_id,
        "predecessor_document_id": comparison.predecessor_document_id,
        "successor_document_id": comparison.successor_document_id,
        "predecessor_run": _persisted_run_content(comparison, "predecessor"),
        "successor_run": _persisted_run_content(comparison, "successor"),
        "matcher_version": comparison.matcher_version,
        "matcher_config": comparison.matcher_config,
        "predecessor_inputs": comparison.predecessor_inputs_json,
        "successor_inputs": comparison.successor_inputs_json,
        "findings": [
            _persisted_finding_content(finding) for finding in findings
        ],
    }
    if _content_sha256(content) != comparison.content_sha256:
        raise CorruptRevisionComparison(
            f"Revision Comparison {comparison.id} content digest does not match"
        )
    return RevisionComparisonReadback(
        comparison=comparison,
        findings=findings,
        predecessor_inputs=tuple(deepcopy(comparison.predecessor_inputs_json)),
        successor_inputs=tuple(deepcopy(comparison.successor_inputs_json)),
    )


def list_revision_comparisons(
    session: Session,
    predecessor_extraction_run_id: int,
    successor_extraction_run_id: int,
) -> tuple[RevisionComparisonRun, ...]:
    """List every immutable rerun for one exact pair, oldest first."""

    return tuple(
        session.scalars(
            select(RevisionComparisonRun)
            .where(
                RevisionComparisonRun.predecessor_extraction_run_id
                == predecessor_extraction_run_id,
                RevisionComparisonRun.successor_extraction_run_id
                == successor_extraction_run_id,
                RevisionComparisonRun.sealed_at.is_not(None),
            )
            .order_by(
                RevisionComparisonRun.generated_at,
                RevisionComparisonRun.id,
            )
        ).all()
    )


def comparison_derivation(
    session: Session,
    comparison_id: int,
    *,
    ruleset_version: str,
):
    """Provenance for a published statement computed from this receipt.

    Comparison findings are document-row observations, not Ledger records, so
    ADR-0013's named ``scope`` is the honest Derivation path: it identifies the
    exact immutable comparison rather than inventing Dependency ids.
    """

    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise RevisionComparisonError("ruleset_version must be a non-empty string")
    readback = read_revision_comparison(session, comparison_id)
    from corridor.report import Derivation

    return Derivation(
        ruleset_version.strip(),
        scope=f"Revision Comparison {readback.comparison.id}",
    )


def _run(session: Session, run_id: int) -> ExtractionRun:
    run = session.get(ExtractionRun, run_id)
    if run is None:
        raise MissingExtractionRun(f"Extraction Run {run_id} does not exist")
    return run


def _run_inputs(
    session: Session, document: Document, run: ExtractionRun
) -> list[dict[str, Any]]:
    if run.candidate_inputs_json is None:
        if run.candidate_count == 0:
            snapshots = []
        else:
            raise InexactExtractionInputs(
                f"Extraction Run {run.id} predates exact Candidate input capture; "
                "run a fresh extraction before comparing it"
            )
    else:
        snapshots = _canonical_jsonb(deepcopy(run.candidate_inputs_json))
    if len(snapshots) != run.candidate_count:
        raise RevisionComparisonError(
            f"Extraction Run {run.id} records {run.candidate_count} Candidates "
            f"but owns {len(snapshots)} immutable inputs"
        )
    snapshot_ids = [snapshot.get("candidate_id") for snapshot in snapshots]
    if any(not isinstance(candidate_id, int) for candidate_id in snapshot_ids):
        raise RevisionComparisonError(
            f"Extraction Run {run.id} has an input without a Candidate id"
        )
    if len(set(snapshot_ids)) != len(snapshot_ids):
        raise RevisionComparisonError(
            f"Extraction Run {run.id} repeats a Candidate input"
        )
    candidates = session.scalars(
        select(Candidate)
        .where(
            Candidate.source_document_id == document.id,
            Candidate.extraction_run_id == run.id,
        )
        .order_by(Candidate.id)
        .execution_options(populate_existing=True)
    ).all()
    candidate_ids = {candidate.id for candidate in candidates}
    if candidate_ids != set(snapshot_ids):
        raise RevisionComparisonError(
            f"Extraction Run {run.id} Candidate membership no longer matches "
            "its immutable inputs"
        )
    for snapshot in snapshots:
        if snapshot.get("project_id") != document.project_id:
            raise RevisionComparisonError(
                f"Candidate {snapshot.get('candidate_id')} crosses its "
                "Extraction Run project"
            )
        if snapshot.get("source_document_id") != document.id:
            raise RevisionComparisonError(
                f"Candidate {snapshot.get('candidate_id')} crosses its "
                "Extraction Run document"
            )
        if snapshot.get("prompt_version") != run.prompt_version:
            raise RevisionComparisonError(
                f"Candidate {snapshot.get('candidate_id')} prompt lineage "
                "disagrees with its run"
            )
        if snapshot.get("model") != run.model:
            raise RevisionComparisonError(
                f"Candidate {snapshot.get('candidate_id')} model lineage "
                "disagrees with its run"
            )
        snapshot["extraction_run_id"] = run.id
    return snapshots


def _validate_supported_inputs(
    document: Document,
    inputs: list[dict[str, Any]],
    *,
    side: str,
) -> None:
    """Reject non-utility source shapes before comparison writes a receipt."""

    if not inputs:
        return
    candidate_ids = [snapshot.get("candidate_id") for snapshot in inputs]
    if document.doc_type != "matrix":
        raise UnsupportedComparisonShape(
            f"{side} Extraction Run contains Candidates {candidate_ids} from "
            f"document type {document.doc_type!r}; matcher "
            f"{DEFAULT_MATCHER_VERSION} supports only utility-matrix "
            "dependency rows"
        )
    unsupported: list[int] = []
    for snapshot in inputs:
        payload = snapshot.get("payload_json")
        fields = payload.get("fields") if isinstance(payload, dict) else None
        if (
            snapshot.get("kind") != "dependency"
            or not isinstance(fields, dict)
        ):
            unsupported.append(snapshot["candidate_id"])
    if unsupported:
        raise UnsupportedComparisonShape(
            f"{side} Extraction Run contains unsupported Candidates "
            f"{unsupported}; matcher {DEFAULT_MATCHER_VERSION} supports only "
            "utility-matrix dependency rows"
        )


def _resolved_config(overrides: dict[str, Any] | None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_MATCHER_CONFIG)
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise RevisionComparisonError("matcher_config must be an object")
        unknown = set(overrides) - _CONFIG_KEYS
        if unknown:
            raise RevisionComparisonError(
                "unknown matcher configuration: " + ", ".join(sorted(unknown))
            )
        for key, value in overrides.items():
            if key == "weights":
                if not isinstance(value, dict):
                    raise RevisionComparisonError("matcher weights must be an object")
                unknown_weights = set(value) - _WEIGHT_KEYS
                if unknown_weights:
                    raise RevisionComparisonError(
                        "unknown matcher weights: "
                        + ", ".join(sorted(unknown_weights))
                    )
                config["weights"].update(value)
            else:
                config[key] = value

    for name in (
        "minimum_score",
        "ambiguity_margin",
        "station_tolerance_ft",
        "minimum_station_identity",
        "minimum_location_identity",
    ):
        value = config[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _is_finite_number(value)
        ):
            raise RevisionComparisonError(f"{name} must be numeric")
    if not 0 < config["minimum_score"] <= 1:
        raise RevisionComparisonError("minimum_score must be in (0, 1]")
    if not 0 <= config["ambiguity_margin"] < 1:
        raise RevisionComparisonError("ambiguity_margin must be in [0, 1)")
    if not 0.01 <= config["station_tolerance_ft"] <= 1_000_000:
        raise RevisionComparisonError(
            "station_tolerance_ft must be between 0.01 and 1000000 feet"
        )
    if not 0 <= config["minimum_station_identity"] <= 1:
        raise RevisionComparisonError("minimum_station_identity must be in [0, 1]")
    if not 0 <= config["minimum_location_identity"] <= 1:
        raise RevisionComparisonError("minimum_location_identity must be in [0, 1]")
    for name in ("max_ambiguity_rows", "max_ambiguity_alternatives"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RevisionComparisonError(f"{name} must be a positive integer")
        if value > 1_000_000:
            raise RevisionComparisonError(f"{name} must not exceed 1000000")
    for name in ("normalization", "baseline_normalization", "field_comparison"):
        if config[name] != DEFAULT_MATCHER_CONFIG[name]:
            raise RevisionComparisonError(
                f"{name} changes require a new matcher version"
            )
    for name, value in config["weights"].items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _is_finite_number(value)
            or value < 0
        ):
            raise RevisionComparisonError(f"matcher weight {name} must be non-negative")
    total_weight = sum(config["weights"].values())
    if not total_weight:
        raise RevisionComparisonError("at least one matcher weight must be positive")
    if not _is_finite_number(total_weight):
        raise RevisionComparisonError("matcher weight total must be finite")
    return _canonical_jsonb(config)


def _is_finite_number(value: int | float) -> bool:
    """Return False for floats and arbitrarily large ints that cannot coerce."""

    try:
        return isfinite(value)
    except OverflowError:
        return False


def _canonical_jsonb(value: Any) -> Any:
    """Mirror JSONB's numeric zero canonicalization before hashing/persisting."""

    if isinstance(value, float):
        if not isfinite(value):
            raise RevisionComparisonError("receipt JSON cannot contain non-finite values")
        return 0.0 if value == 0 else value
    if isinstance(value, dict):
        return {key: _canonical_jsonb(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_jsonb(item) for item in value]
    return value


def _compare_inputs(
    predecessor_inputs: list[dict[str, Any]],
    successor_inputs: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[_FindingDraft]:
    predecessor_rows = {
        snapshot["candidate_id"]: _row(snapshot) for snapshot in predecessor_inputs
    }
    successor_rows = {
        snapshot["candidate_id"]: _row(snapshot) for snapshot in successor_inputs
    }
    edges = _eligible_edges(predecessor_rows, successor_rows, config)
    assignable = {
        pair: edge
        for pair, edge in edges.items()
        if edge.score >= config["minimum_score"]
        and edge.strong_identity
    }
    matched = _global_matching(assignable)
    uncertainty = {
        "weak_identity": {
            pair for pair, edge in edges.items() if not edge.strong_identity
        },
        "near_tied": _near_tied_pairs(edges, config["ambiguity_margin"]),
        "displaced_stronger": _displaced_pairs(assignable, matched),
    }
    ambiguous, ambiguous_predecessors, ambiguous_successors = (
        _bounded_ambiguities(edges, matched, uncertainty, config)
    )
    if ambiguous:
        matched = [
            edge
            for edge in matched
            if edge.predecessor_id not in ambiguous_predecessors
            and edge.successor_id not in ambiguous_successors
        ]
    matched_predecessors = {edge.predecessor_id for edge in matched}
    matched_successors = {edge.successor_id for edge in matched}
    unmatched_predecessors = (
        predecessor_rows.keys()
        - ambiguous_predecessors
        - matched_predecessors
    )
    unmatched_successors = (
        successor_rows.keys() - ambiguous_successors - matched_successors
    )

    findings: list[_FindingDraft] = list(ambiguous)
    for edge in sorted(
        matched, key=lambda item: (item.predecessor_id, item.successor_id)
    ):
        changes = tuple(
            _field_changes(
                predecessor_rows[edge.predecessor_id].fields,
                successor_rows[edge.successor_id].fields,
            )
        )
        findings.append(
            _FindingDraft(
                state="changed" if changes else "unchanged",
                predecessor_ids=(edge.predecessor_id,),
                successor_ids=(edge.successor_id,),
                match_score=edge.score,
                field_changes=changes,
                matcher_detail={"signals": list(edge.signals)},
            )
        )

    for candidate_id in sorted(unmatched_predecessors):
        state, reason = _side_specific_unmatched_state(
            candidate_id,
            predecessor_rows,
            successor_rows,
            edges,
            ambiguous_successors,
            predecessor_side=True,
        )
        findings.append(
            _FindingDraft(
                state=state,
                predecessor_ids=(candidate_id,),
                successor_ids=(),
                match_score=None,
                field_changes=(),
                matcher_detail={"reason": reason},
            )
        )
    for candidate_id in sorted(unmatched_successors):
        state, reason = _side_specific_unmatched_state(
            candidate_id,
            successor_rows,
            predecessor_rows,
            edges,
            ambiguous_predecessors,
            predecessor_side=False,
        )
        findings.append(
            _FindingDraft(
                state=state,
                predecessor_ids=(),
                successor_ids=(candidate_id,),
                match_score=None,
                field_changes=(),
                matcher_detail={"reason": reason},
            )
        )

    state_order = {
        "ambiguous": 0,
        "unchanged": 1,
        "changed": 2,
        "unmatched": 3,
        "dropped": 4,
        "added": 5,
    }
    findings.sort(
        key=lambda finding: (
            state_order[finding.state],
            finding.predecessor_ids or (inf,),
            finding.successor_ids or (inf,),
        )
    )
    return findings


def _side_specific_unmatched_state(
    candidate_id: int,
    own_rows: dict[int, _RevisionRow],
    opposite_rows: dict[int, _RevisionRow],
    edges: dict[tuple[int, int], _ScoredEdge],
    ambiguous_opposite_ids: set[int],
    *,
    predecessor_side: bool,
) -> tuple[str, str]:
    terminal_state = "dropped" if predecessor_side else "added"
    if not opposite_rows:
        return (
            terminal_state,
            "the completed opposite Extraction Run contains zero rows",
        )
    if not _has_complete_physical_coordinate(own_rows[candidate_id]):
        return (
            "unmatched",
            "the row lacks a parseable station and meaningful baseline, so "
            f"the matcher cannot classify it as {terminal_state}",
        )
    touches_ambiguity = any(
        (
            predecessor_id == candidate_id
            and successor_id in ambiguous_opposite_ids
        )
        if predecessor_side
        else (
            successor_id == candidate_id
            and predecessor_id in ambiguous_opposite_ids
        )
        for predecessor_id, successor_id in edges
    )
    if touches_ambiguity:
        return (
            "unmatched",
            "a plausible counterpart belongs to a separate ambiguity group",
        )
    return (
        terminal_state,
        "complete coordinates place every plausible counterpart outside the "
        "matcher configuration",
    )


def _row(snapshot: dict[str, Any]) -> _RevisionRow:
    payload = snapshot.get("payload_json")
    payload = payload if isinstance(payload, dict) else {}
    fields = payload.get("fields")
    return _RevisionRow(
        candidate_id=snapshot["candidate_id"],
        kind=snapshot.get("kind", ""),
        fields=deepcopy(fields) if isinstance(fields, dict) else {},
    )


def _eligible_edges(
    predecessor_rows: dict[int, _RevisionRow],
    successor_rows: dict[int, _RevisionRow],
    config: dict[str, Any],
) -> dict[tuple[int, int], _ScoredEdge]:
    """Score every same-kind pair, retaining only plausible sparse edges.

    The scorer accepts fuzzy location identity and station containment.  A
    lossy pre-score block can therefore turn a valid correspondence into a
    false added/dropped pair.  NHHIP's revision scale is small enough to make
    correctness-first pair evaluation cheap; the graph passed to ambiguity
    and assignment remains sparse because only scorer-valid edges survive.
    """

    plausibility_floor = max(
        0.0,
        config["minimum_score"] - config["ambiguity_margin"],
    )
    edges: dict[tuple[int, int], _ScoredEdge] = {}
    for predecessor in predecessor_rows.values():
        for successor in successor_rows.values():
            if predecessor.kind != successor.kind:
                continue
            edge = _score_edge(predecessor, successor, config)
            if edge is not None and edge.score >= plausibility_floor:
                edges[(predecessor.candidate_id, successor.candidate_id)] = edge
    return edges


def _score_edge(
    predecessor: _RevisionRow,
    successor: _RevisionRow,
    config: dict[str, Any],
) -> _ScoredEdge | None:
    if predecessor.kind != successor.kind:
        return None
    weights = config["weights"]
    signals: list[dict[str, Any]] = []
    baseline_conflict = _has_baseline_conflict(
        predecessor.fields, successor.fields
    )

    station = _station_similarity(
        predecessor.fields,
        successor.fields,
        config["station_tolerance_ft"],
    )
    if station == 0.0:
        return None
    if station is not None and weights["station"]:
        signals.append(_signal("station", station, weights["station"]))

    predecessor_ref = _meaningful_identity(predecessor.fields.get("utility_id"))
    successor_ref = _meaningful_identity(successor.fields.get("utility_id"))
    reference_exact = bool(
        predecessor_ref and successor_ref and predecessor_ref == successor_ref
    )
    if predecessor_ref and successor_ref and weights["source_ref"]:
        signals.append(
            _signal(
                "source_ref",
                1.0 if reference_exact else 0.0,
                weights["source_ref"],
            )
        )

    predecessor_owner = _meaningful_identity(
        predecessor.fields.get("external_org")
    )
    successor_owner = _meaningful_identity(successor.fields.get("external_org"))
    owner_mismatch = bool(
        predecessor_owner
        and successor_owner
        and predecessor_owner != successor_owner
    )

    predecessor_type = _identity_text(
        predecessor.fields.get("utility_type")
        or predecessor.fields.get("dep_type")
    )
    successor_type = _identity_text(
        successor.fields.get("utility_type") or successor.fields.get("dep_type")
    )
    type_score = _ratio(predecessor_type, successor_type)
    if type_score is not None and weights["type"]:
        signals.append(_signal("type", type_score, weights["type"]))

    predecessor_location = _location_text(predecessor.fields)
    successor_location = _location_text(successor.fields)
    location_score = _ratio(predecessor_location, successor_location)
    if location_score is not None and weights["location"]:
        signals.append(_signal("location", location_score, weights["location"]))

    independent_location_score = _ratio(
        _independent_location_text(predecessor.fields),
        _independent_location_text(successor.fields),
    )
    independent_location_identity = (
        independent_location_score is not None
        and independent_location_score >= config["minimum_location_identity"]
    )

    # Owner and utility type identify a cohort, not a row.  An explicit source
    # id disagreement without strong station or location evidence is a pair of
    # unmatched rows, never an exact correspondence manufactured by a score at
    # the threshold.  Renumbering remains supported when the physical identity
    # primitives agree.
    physical_identity = (
        (
            not baseline_conflict
            and station is not None
            and station >= config["minimum_station_identity"]
        )
        or (
            independent_location_identity
            if baseline_conflict
            else (
                location_score is not None
                and location_score >= config["minimum_location_identity"]
            )
        )
    )
    owner_score = (
        0.0 if owner_mismatch else 1.0
    ) if predecessor_owner and successor_owner else None
    if owner_score is not None and weights["owner"]:
        signals.append(_signal("owner", owner_score, weights["owner"]))

    # Matrix ids repeat across corridors.  Once both rows establish different
    # baselines, the shared printed id cannot overrule those contradictory
    # coordinates; only a separate named location can link the rows.
    strong_identity = physical_identity or (
        reference_exact and not baseline_conflict
    )
    weak_physical_identity = (
        not baseline_conflict
        and (
            (
                station is not None
                and station > 0.0
                and _has_comparable_baseline(
                    predecessor.fields, successor.fields
                )
            )
            or (
                independent_location_score is not None
                and independent_location_score > 0.0
            )
        )
    )
    if not strong_identity and not weak_physical_identity:
        return None
    # A changed owner is review-significant but not necessarily a different
    # row. It remains plausible only when the source id agrees and there is
    # independent physical evidence. Weak physical evidence is retained as
    # ambiguity, never promoted to an assignable owner edit.
    if owner_mismatch:
        if baseline_conflict or not reference_exact:
            return None
        if not (physical_identity or weak_physical_identity):
            return None
        strong_identity = physical_identity

    total_weight = sum(signal["weight"] for signal in signals)
    if not total_weight:
        return None
    score = sum(
        signal["score"] * signal["weight"] for signal in signals
    ) / total_weight
    return _ScoredEdge(
        predecessor_id=predecessor.candidate_id,
        successor_id=successor.candidate_id,
        score=round(score, 8),
        signals=tuple(signals),
        strong_identity=strong_identity,
    )


def _signal(name: str, score: float, weight: float) -> dict[str, Any]:
    return {"name": name, "score": round(score, 8), "weight": weight}


def _identity_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return normalize(str(value))


def _meaningful_identity(value: Any) -> str:
    """Normalize one identity primitive and remove whole-value placeholders."""

    text = _identity_text(value)
    return "" if is_placeholder_party(text) else text


def _baseline_identity(value: Any) -> str:
    """Canonical road/baseline id independent of printed spacing/punctuation."""

    return "".join(
        character
        for character in _meaningful_identity(value)
        if character.isalnum()
    )


def _ratio(left: str, right: str) -> float | None:
    if not left or not right:
        return None
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _station_span(fields: dict[str, Any]) -> tuple[float, float] | None:
    values = [
        parse_station(fields.get(name))
        for name in ("station_from", "station_to", "location_start", "location_end")
    ]
    stations = [value for value in values if value is not None]
    if not stations:
        return None
    return min(stations), max(stations)


def _has_complete_physical_coordinate(row: _RevisionRow) -> bool:
    return bool(
        _baseline_identity(row.fields.get("baseline"))
        and _station_span(row.fields) is not None
    )


def _station_similarity(
    predecessor_fields: dict[str, Any],
    successor_fields: dict[str, Any],
    tolerance: float,
) -> float | None:
    if _has_baseline_conflict(predecessor_fields, successor_fields):
        # Equal station numbers on different baselines are different
        # coordinates. This is incomparable station evidence, not a distant
        # point: an exact source id or a location may still establish identity.
        return None
    predecessor = _station_span(predecessor_fields)
    successor = _station_span(successor_fields)
    if predecessor is None or successor is None:
        return None
    overlap = min(predecessor[1], successor[1]) - max(
        predecessor[0], successor[0]
    )
    if overlap >= 0:
        shorter = min(
            predecessor[1] - predecessor[0], successor[1] - successor[0]
        )
        return 1.0 if shorter <= 0 else min(1.0, overlap / shorter)
    gap = -overlap
    if gap >= tolerance:
        return 0.0
    return 1.0 - (gap / tolerance)


def _has_baseline_conflict(
    predecessor_fields: dict[str, Any], successor_fields: dict[str, Any]
) -> bool:
    predecessor = _baseline_identity(predecessor_fields.get("baseline"))
    successor = _baseline_identity(successor_fields.get("baseline"))
    return bool(predecessor and successor and predecessor != successor)


def _has_comparable_baseline(
    predecessor_fields: dict[str, Any], successor_fields: dict[str, Any]
) -> bool:
    predecessor = _baseline_identity(predecessor_fields.get("baseline"))
    successor = _baseline_identity(successor_fields.get("baseline"))
    return bool(predecessor and predecessor == successor)


def _location_text(fields: dict[str, Any]) -> str:
    return " ".join(
        value
        for value in (
            _meaningful_identity(fields.get(name))
            for name in ("location_start", "location_end", "alignment")
        )
        if value
    )


def _independent_location_text(fields: dict[str, Any]) -> str:
    """Location evidence that is independent of baseline stationing."""

    values: list[str] = []
    for name in ("location_start", "location_end", "alignment"):
        value = fields.get(name)
        if name != "alignment" and parse_station(value) is not None:
            continue
        normalized = _meaningful_identity(value)
        if normalized:
            values.append(normalized)
    return " ".join(values)


def _near_tied_pairs(
    edges: dict[tuple[int, int], _ScoredEdge], margin: float
) -> set[tuple[int, int]]:
    by_predecessor: dict[int, list[_ScoredEdge]] = defaultdict(list)
    by_successor: dict[int, list[_ScoredEdge]] = defaultdict(list)
    for edge in edges.values():
        by_predecessor[edge.predecessor_id].append(edge)
        by_successor[edge.successor_id].append(edge)

    uncertain_pairs: set[tuple[int, int]] = set()
    for alternatives in (*by_predecessor.values(), *by_successor.values()):
        alternatives.sort(
            key=lambda edge: (-edge.score, edge.predecessor_id, edge.successor_id)
        )
        if (
            len(alternatives) >= 2
            and alternatives[0].score - alternatives[1].score <= margin
        ):
            top = alternatives[0].score
            uncertain_pairs.update(
                (edge.predecessor_id, edge.successor_id)
                for edge in alternatives
                if top - edge.score <= margin
            )

    return uncertain_pairs


def _displaced_pairs(
    edges: dict[tuple[int, int], _ScoredEdge],
    matched: list[_ScoredEdge],
) -> set[tuple[int, int]]:
    """Find unchosen evidence stronger than a conflicting chosen edge."""

    chosen_pairs = {
        (edge.predecessor_id, edge.successor_id) for edge in matched
    }
    by_predecessor = {edge.predecessor_id: edge for edge in matched}
    by_successor = {edge.successor_id: edge for edge in matched}
    displaced_pairs: set[tuple[int, int]] = set()
    for pair, edge in edges.items():
        if pair in chosen_pairs:
            continue
        competing = []
        if edge.predecessor_id in by_predecessor:
            competing.append(by_predecessor[edge.predecessor_id].score)
        if edge.successor_id in by_successor:
            competing.append(by_successor[edge.successor_id].score)
        if competing and edge.score > min(competing) + _SCORE_EPSILON:
            displaced_pairs.add(pair)
    return displaced_pairs


def _bounded_ambiguities(
    edges: dict[tuple[int, int], _ScoredEdge],
    matched: list[_ScoredEdge],
    uncertainty: dict[str, set[tuple[int, int]]],
    config: dict[str, Any],
) -> tuple[list[_FindingDraft], set[int], set[int]]:
    """Withhold local uncertainty without traversing every plausible edge.

    The graph contains only an uncertainty edge or a chosen edge directly
    incident to an uncertainty vertex.  Merely plausible edges cannot bridge
    otherwise separate review decisions into one corridor-sized ambiguity.
    """

    seed_pairs = set().union(*uncertainty.values())
    if not seed_pairs:
        return [], set(), set()

    seeded_predecessors = {pair[0] for pair in seed_pairs}
    seeded_successors = {pair[1] for pair in seed_pairs}
    chosen_pairs = {
        (edge.predecessor_id, edge.successor_id)
        for edge in matched
        if edge.predecessor_id in seeded_predecessors
        or edge.successor_id in seeded_successors
    }
    relevant_pairs = seed_pairs | chosen_pairs
    predecessor_graph: dict[int, set[int]] = defaultdict(set)
    successor_graph: dict[int, set[int]] = defaultdict(set)
    for predecessor_id, successor_id in relevant_pairs:
        predecessor_graph[predecessor_id].add(successor_id)
        successor_graph[successor_id].add(predecessor_id)

    findings: list[_FindingDraft] = []
    seen_predecessors: set[int] = set()
    seen_successors: set[int] = set()
    for start in sorted(seeded_predecessors):
        if start in seen_predecessors:
            continue
        component_predecessors: set[int] = set()
        component_successors: set[int] = set()
        queue = deque([("predecessor", start)])
        while queue:
            side, candidate_id = queue.popleft()
            if side == "predecessor":
                if candidate_id in component_predecessors:
                    continue
                component_predecessors.add(candidate_id)
                queue.extend(
                    ("successor", successor_id)
                    for successor_id in predecessor_graph[candidate_id]
                )
            else:
                if candidate_id in component_successors:
                    continue
                component_successors.add(candidate_id)
                queue.extend(
                    ("predecessor", predecessor_id)
                    for predecessor_id in successor_graph[candidate_id]
                )
        seen_predecessors.update(component_predecessors)
        seen_successors.update(component_successors)
        alternatives = [
            edges[pair]
            for pair in relevant_pairs
            if pair[0] in component_predecessors
            and pair[1] in component_successors
        ]
        alternatives.sort(
            key=lambda edge: (-edge.score, edge.predecessor_id, edge.successor_id)
        )
        alternative_details = [
            _ambiguity_alternative_detail(
                edge, uncertainty=uncertainty, chosen_pairs=chosen_pairs
            )
            for edge in alternatives
        ]
        row_count = len(component_predecessors) + len(component_successors)
        if (
            row_count <= config["max_ambiguity_rows"]
            and len(alternatives) <= config["max_ambiguity_alternatives"]
        ):
            findings.append(
                _FindingDraft(
                    state="ambiguous",
                    predecessor_ids=tuple(sorted(component_predecessors)),
                    successor_ids=tuple(sorted(component_successors)),
                    match_score=alternatives[0].score,
                    field_changes=(),
                    matcher_detail={"alternatives": alternative_details},
                )
            )
            continue

        group_content = {
            "predecessor_candidate_ids": sorted(component_predecessors),
            "successor_candidate_ids": sorted(component_successors),
            "alternatives": [
                {
                    key: value
                    for key, value in detail.items()
                    if key != "signals"
                }
                for detail in sorted(
                    alternative_details,
                    key=lambda item: (
                        item["predecessor_candidate_id"],
                        item["successor_candidate_id"],
                    ),
                )
            ],
        }
        group_sha256 = _content_sha256(group_content)
        group_summary = {
            "sha256": group_sha256,
            "predecessor_count": len(component_predecessors),
            "successor_count": len(component_successors),
            "alternative_count": len(alternatives),
        }
        for candidate_id in sorted(component_predecessors):
            findings.append(
                _overflow_unmatched(
                    candidate_id,
                    predecessor_side=True,
                    alternative_details=alternative_details,
                    group_summary=group_summary,
                    limit=config["max_ambiguity_alternatives"],
                )
            )
        for candidate_id in sorted(component_successors):
            findings.append(
                _overflow_unmatched(
                    candidate_id,
                    predecessor_side=False,
                    alternative_details=alternative_details,
                    group_summary=group_summary,
                    limit=config["max_ambiguity_alternatives"],
                )
            )
    return findings, seen_predecessors, seen_successors


def _ambiguity_alternative_detail(
    edge: _ScoredEdge,
    *,
    uncertainty: dict[str, set[tuple[int, int]]],
    chosen_pairs: set[tuple[int, int]],
) -> dict[str, Any]:
    pair = (edge.predecessor_id, edge.successor_id)
    return {
        "predecessor_candidate_id": edge.predecessor_id,
        "successor_candidate_id": edge.successor_id,
        "score": edge.score,
        **{
            flag: pair in flagged_pairs
            for flag, flagged_pairs in uncertainty.items()
        },
        "chosen": pair in chosen_pairs,
        "signals": list(edge.signals),
    }


def _overflow_unmatched(
    candidate_id: int,
    *,
    predecessor_side: bool,
    alternative_details: list[dict[str, Any]],
    group_summary: dict[str, Any],
    limit: int,
) -> _FindingDraft:
    id_key = (
        "predecessor_candidate_id"
        if predecessor_side
        else "successor_candidate_id"
    )
    top_alternatives = [
        detail for detail in alternative_details if detail[id_key] == candidate_id
    ][:limit]
    return _FindingDraft(
        state="unmatched",
        predecessor_ids=(candidate_id,) if predecessor_side else (),
        successor_ids=() if predecessor_side else (candidate_id,),
        match_score=None,
        field_changes=(),
        matcher_detail={
            "reason": (
                "plausible counterparts form an unreviewable ambiguity component"
            ),
            "ambiguity_group": group_summary,
            "top_alternatives": top_alternatives,
        },
    )


def _global_matching(
    edges: dict[tuple[int, int], _ScoredEdge]
) -> list[_ScoredEdge]:
    """Sparse maximum-cardinality then maximum-weight one-to-one matching."""

    if not edges:
        return []
    predecessor_graph: dict[int, set[int]] = defaultdict(set)
    successor_graph: dict[int, set[int]] = defaultdict(set)
    for predecessor_id, successor_id in edges:
        predecessor_graph[predecessor_id].add(successor_id)
        successor_graph[successor_id].add(predecessor_id)

    matches: list[_ScoredEdge] = []
    seen_predecessors: set[int] = set()
    for start in sorted(predecessor_graph):
        if start in seen_predecessors:
            continue
        predecessors: set[int] = set()
        successors: set[int] = set()
        queue = deque([("predecessor", start)])
        while queue:
            side, candidate_id = queue.popleft()
            if side == "predecessor":
                if candidate_id in predecessors:
                    continue
                predecessors.add(candidate_id)
                queue.extend(
                    ("successor", successor_id)
                    for successor_id in predecessor_graph[candidate_id]
                )
            else:
                if candidate_id in successors:
                    continue
                successors.add(candidate_id)
                queue.extend(
                    ("predecessor", predecessor_id)
                    for predecessor_id in successor_graph[candidate_id]
                )
        seen_predecessors.update(predecessors)
        component_edges = {
            pair: edge
            for pair, edge in edges.items()
            if pair[0] in predecessors and pair[1] in successors
        }
        cardinality = _maximum_cardinality(predecessors, successors, component_edges)
        matches.extend(
            _maximum_weight_for_cardinality(
                predecessors,
                successors,
                component_edges,
                cardinality,
            )
        )
    return matches


def _maximum_cardinality(
    predecessors: set[int],
    successors: set[int],
    edges: dict[tuple[int, int], _ScoredEdge],
) -> int:
    adjacency = {
        predecessor_id: sorted(
            successor_id
            for left_id, successor_id in edges
            if left_id == predecessor_id
        )
        for predecessor_id in predecessors
    }
    pair_left: dict[int, int | None] = {candidate_id: None for candidate_id in predecessors}
    pair_right: dict[int, int | None] = {candidate_id: None for candidate_id in successors}
    distance: dict[int, int] = {}

    def breadth_first() -> bool:
        queue: deque[int] = deque()
        found = False
        for candidate_id in predecessors:
            if pair_left[candidate_id] is None:
                distance[candidate_id] = 0
                queue.append(candidate_id)
            else:
                distance[candidate_id] = -1
        while queue:
            candidate_id = queue.popleft()
            for successor_id in adjacency[candidate_id]:
                paired = pair_right[successor_id]
                if paired is None:
                    found = True
                elif distance[paired] < 0:
                    distance[paired] = distance[candidate_id] + 1
                    queue.append(paired)
        return found

    def depth_first(candidate_id: int) -> bool:
        for successor_id in adjacency[candidate_id]:
            paired = pair_right[successor_id]
            if paired is None or (
                distance.get(paired) == distance[candidate_id] + 1
                and depth_first(paired)
            ):
                pair_left[candidate_id] = successor_id
                pair_right[successor_id] = candidate_id
                return True
        distance[candidate_id] = -1
        return False

    cardinality = 0
    while breadth_first():
        for candidate_id in sorted(predecessors):
            if pair_left[candidate_id] is None and depth_first(candidate_id):
                cardinality += 1
    return cardinality


def _maximum_weight_for_cardinality(
    predecessors: set[int],
    successors: set[int],
    edges: dict[tuple[int, int], _ScoredEdge],
    cardinality: int,
) -> list[_ScoredEdge]:
    if cardinality == 0:
        return []
    left = sorted(predecessors)
    right = sorted(successors)
    source = 0
    left_offset = 1
    right_offset = left_offset + len(left)
    sink = right_offset + len(right)
    graph: list[list[_FlowEdge]] = [[] for _ in range(sink + 1)]
    left_node = {candidate_id: left_offset + index for index, candidate_id in enumerate(left)}
    right_node = {candidate_id: right_offset + index for index, candidate_id in enumerate(right)}

    for candidate_id in left:
        _add_flow_edge(graph, source, left_node[candidate_id], 0)
    for candidate_id in right:
        _add_flow_edge(graph, right_node[candidate_id], sink, 0)
    for pair, edge in sorted(edges.items()):
        cost = _COST_SCALE - int(round(edge.score * _COST_SCALE))
        _add_flow_edge(
            graph,
            left_node[pair[0]],
            right_node[pair[1]],
            cost,
            pair=pair,
        )

    potentials = [0] * len(graph)
    for _ in range(cardinality):
        distances = [10**30] * len(graph)
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        distances[source] = 0
        queue: list[tuple[int, int]] = [(0, source)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distance != distances[node]:
                continue
            for edge_index, edge in enumerate(graph[node]):
                if not edge.capacity:
                    continue
                candidate_distance = (
                    distance + edge.cost + potentials[node] - potentials[edge.to]
                )
                if candidate_distance < distances[edge.to]:
                    distances[edge.to] = candidate_distance
                    previous[edge.to] = (node, edge_index)
                    heapq.heappush(queue, (candidate_distance, edge.to))
        if previous[sink] is None:
            raise RuntimeError("maximum-cardinality matching could not be realized")
        for node, distance in enumerate(distances):
            if distance < 10**30:
                potentials[node] += distance
        node = sink
        while node != source:
            prior_node, edge_index = previous[node]
            edge = graph[prior_node][edge_index]
            edge.capacity -= 1
            graph[node][edge.reverse].capacity += 1
            node = prior_node

    chosen: list[_ScoredEdge] = []
    for candidate_id in left:
        for edge in graph[left_node[candidate_id]]:
            if edge.pair is not None and edge.capacity == 0:
                chosen.append(edges[edge.pair])
    return chosen


def _add_flow_edge(
    graph: list[list[_FlowEdge]],
    source: int,
    target: int,
    cost: int,
    *,
    pair: tuple[int, int] | None = None,
) -> None:
    forward = _FlowEdge(target, len(graph[target]), 1, cost, pair)
    reverse = _FlowEdge(source, len(graph[source]), 0, -cost)
    graph[source].append(forward)
    graph[target].append(reverse)


def _field_changes(
    predecessor: dict[str, Any], successor: dict[str, Any]
) -> list[dict[str, Any]]:
    changes = []
    for field in sorted(predecessor.keys() | successor.keys()):
        before = predecessor.get(field)
        after = successor.get(field)
        if _field_comparison_value(field, before) != _field_comparison_value(
            field, after
        ):
            changes.append({"field": field, "before": before, "after": after})
    return changes


def _field_comparison_value(field: str, value: Any) -> tuple[str, Any]:
    if field == "baseline":
        return "baseline", _baseline_identity(value)
    if field in {"station_from", "station_to"}:
        if not _meaningful_identity(value):
            return "station", None
        station = parse_station(value)
        if station is not None:
            return "station", station
    return _comparison_value(value)


def _comparison_value(value: Any) -> tuple[str, Any]:
    """A normalized value without collapsing null, false, or numeric zero."""

    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, (int, float)):
        return type(value).__name__, value
    if isinstance(value, (dict, list)):
        return type(value).__name__, json.dumps(
            value, sort_keys=True, separators=(",", ":")
        )
    return "string", normalize(str(value))


def _receipt_content(
    *,
    project_id: int,
    predecessor_document_id: int,
    successor_document_id: int,
    predecessor_run: Any,
    successor_run: Any,
    matcher_version: str,
    matcher_config: dict[str, Any],
    predecessor_inputs: list[dict[str, Any]],
    successor_inputs: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "predecessor_document_id": predecessor_document_id,
        "successor_document_id": successor_document_id,
        "predecessor_run": _extraction_run_content(predecessor_run),
        "successor_run": _extraction_run_content(successor_run),
        "matcher_version": matcher_version,
        "matcher_config": matcher_config,
        "predecessor_inputs": predecessor_inputs,
        "successor_inputs": successor_inputs,
        "findings": findings,
    }


def _extraction_run_content(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "schema_version": run.schema_version,
        "prompt_version": run.prompt_version,
        "model": run.model,
    }


def _persisted_run_content(
    comparison: RevisionComparisonRun, side: str
) -> dict[str, Any]:
    return {
        "id": getattr(comparison, f"{side}_extraction_run_id"),
        "schema_version": getattr(comparison, f"{side}_schema_version"),
        "prompt_version": getattr(comparison, f"{side}_prompt_version"),
        "model": getattr(comparison, f"{side}_model"),
    }


def _persisted_finding_content(
    finding: RevisionComparisonFinding,
) -> dict[str, Any]:
    return {
        "state": finding.state,
        "predecessor_candidate_ids": list(finding.predecessor_candidate_ids),
        "successor_candidate_ids": list(finding.successor_candidate_ids),
        "match_score": finding.match_score,
        "field_changes": finding.field_changes,
        "matcher_detail": finding.matcher_detail,
    }


def _content_sha256(content: dict[str, Any]) -> str:
    encoded = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
