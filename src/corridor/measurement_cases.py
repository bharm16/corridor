"""Turn human extraction rulings into replayable measurement cases.

Previously, corrections, Source Discrepancy settlements, and Do Not Add acts
survived only in their Project Record histories. That retained accountability
but gave Extraction Measurement no exact source/ruling pair to replay. This
module records a separate append-only Operations lineage and never mutates the
Project Record it observes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from corridor.models import (
    AuditLog,
    Candidate,
    CommitmentScopeMembership,
    Dependency,
    Document,
    DisputeSettlement,
    ExternalOrg,
    ExternalPartyStatement,
    ExtractionMeasurementCaseState,
    ExtractionRun,
    CommitmentScopeDecision,
    StatementCoordinationReversal,
)

CASE_PREDICTIONS_SCHEMA = "corridor.extraction-measurement-case-predictions.v1"


class CasePredictionError(ValueError):
    """Candidate-model outputs cannot be bound to the exact active case set."""


@dataclass(frozen=True)
class CasePredictionSet:
    """Exact candidate-model outputs keyed by immutable case-state public id."""

    outputs: Mapping[str, dict]
    source: str
    sha256: str
    schema_version: str = CASE_PREDICTIONS_SCHEMA

    def receipt(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "sha256": self.sha256,
            "case_state_public_ids": sorted(self.outputs),
        }


def load_case_predictions(path: str | Path) -> CasePredictionSet:
    """Load exact case-bound model outputs without treating them as rulings."""

    source = Path(path)
    try:
        raw = source.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CasePredictionError(f"case predictions are unreadable: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "predictions",
    }:
        raise CasePredictionError("case predictions must contain schema and predictions")
    if payload.get("schema_version") != CASE_PREDICTIONS_SCHEMA:
        raise CasePredictionError("case predictions have an unsupported schema")
    rows = payload.get("predictions")
    if not isinstance(rows, list):
        raise CasePredictionError("case predictions must be a list")
    outputs: dict[str, dict] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"case_state_public_id", "output"}
            or not isinstance(row.get("case_state_public_id"), str)
            or not row["case_state_public_id"].strip()
            or not isinstance(row.get("output"), dict)
            or row["case_state_public_id"] in outputs
        ):
            raise CasePredictionError("case prediction identity or output is invalid")
        outputs[row["case_state_public_id"]] = row["output"]
    return CasePredictionSet(
        outputs=outputs,
        source=str(source),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


@dataclass(frozen=True)
class CaseScore:
    """One current human case scored against an exact run population."""

    case_state_id: int
    case_key: str
    kind: str
    status: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "case_state_id": self.case_state_id,
            "case_key": self.case_key,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HumanCaseMeasurement:
    """Score receipt for human rulings, distinct from machine references."""

    scores: tuple[CaseScore, ...]
    historical_state_ids: tuple[int, ...]
    outside_scope: int = 0
    reference_kind: str = "human_ruling"
    prediction_receipt: dict | None = None

    @property
    def matched(self) -> int:
        return sum(score.status == "matched" for score in self.scores)

    @property
    def mismatched(self) -> int:
        return sum(score.status == "mismatched" for score in self.scores)

    @property
    def not_applicable(self) -> int:
        return sum(score.status == "not_applicable" for score in self.scores)

    def as_dict(self) -> dict:
        return {
            "reference_kind": self.reference_kind,
            "source": "extraction_measurement_case_states",
            "case_state_ids": [score.case_state_id for score in self.scores],
            "historical_state_ids": list(self.historical_state_ids),
            "matched": self.matched,
            "mismatched": self.mismatched,
            "not_applicable": self.not_applicable,
            "outside_scope": self.outside_scope,
            "prediction_receipt": self.prediction_receipt,
            "scores": [score.as_dict() for score in self.scores],
            "limitations": [
                "Human rulings apply only to their exact recorded source and "
                "task. Machine references remain semi-independent ceilings, "
                "not human gold."
            ],
        }


def score_measurement_cases(
    session: Session,
    *,
    project_id: int,
    extraction_run_ids: set[int],
    predictions: CasePredictionSet | None = None,
) -> HumanCaseMeasurement:
    """Score current human cases against Candidates from exact completed runs."""

    selected_documents = {
        document_id: document_sha256
        for document_id, document_sha256 in session.execute(
            select(Document.id, Document.sha256)
            .join(ExtractionRun, ExtractionRun.document_id == Document.id)
            .where(ExtractionRun.id.in_(extraction_run_ids or {0}))
        ).all()
    }
    candidates = tuple(
        session.scalars(
            select(Candidate).where(
                Candidate.extraction_run_id.in_(extraction_run_ids or {0})
            )
        ).all()
    )
    successor = aliased(ExtractionMeasurementCaseState)
    current_states = tuple(
        session.scalars(
            select(ExtractionMeasurementCaseState)
            .where(
                ExtractionMeasurementCaseState.project_id == project_id,
                ~exists(
                    select(successor.id).where(
                        successor.predecessor_state_id
                        == ExtractionMeasurementCaseState.id
                    )
                ),
            )
            .order_by(ExtractionMeasurementCaseState.id)
        ).all()
    )
    scoped: list[ExtractionMeasurementCaseState] = []
    outside_scope = 0
    for state in current_states:
        if state.state != "active":
            continue
        case_documents = _source_documents(state.source_identity_json)
        if case_documents and all(
            selected_documents.get(document_id) == document_sha256
            for document_id, document_sha256 in case_documents.items()
        ):
            scoped.append(state)
        else:
            outside_scope += 1

    case_keys = {state.case_key for state in scoped}
    scoped_public_ids = {state.public_id for state in scoped}
    if predictions is not None:
        unexpected = sorted(set(predictions.outputs) - scoped_public_ids)
        if unexpected:
            raise CasePredictionError(
                "case predictions name states outside the exact active case scope: "
                + ", ".join(unexpected)
            )
    historical_state_ids = tuple(
        session.scalars(
            select(ExtractionMeasurementCaseState.id)
            .where(ExtractionMeasurementCaseState.case_key.in_(case_keys or {""}))
            .order_by(ExtractionMeasurementCaseState.id)
        ).all()
    )
    return HumanCaseMeasurement(
        scores=tuple(
            _score_case(
                session,
                state,
                candidates,
                prediction=(predictions.outputs.get(state.public_id) if predictions else None),
            )
            for state in scoped
        ),
        historical_state_ids=historical_state_ids,
        outside_scope=outside_scope,
        prediction_receipt=predictions.receipt() if predictions else None,
    )


def _score_case(
    session: Session,
    state: ExtractionMeasurementCaseState,
    candidates: tuple[Candidate, ...],
    *,
    prediction: dict | None,
) -> CaseScore:
    expected = state.expected_json
    rule = expected.get("scoring_rule")
    located = tuple(
        candidate
        for candidate in candidates
        if candidate.kind == expected.get("candidate_kind", candidate.kind)
        and _candidate_at_source(session, candidate, state.source_identity_json)
    )
    if rule == "candidate_fields_include":
        wanted = expected.get("fields") or {}
        matched = any(
            all(
                (candidate.payload_json.get("fields") or {}).get(key) == value
                for key, value in wanted.items()
            )
            for candidate in located
        )
        return _case_score(
            state,
            "matched" if matched else "mismatched",
            "corrected fields reproduced" if matched else "corrected fields missing",
        )
    if rule == "candidate_disposition":
        if prediction is not None:
            wanted = {
                "disposition": expected.get("expected_disposition"),
                "reason": expected.get("reason"),
            }
            matched = prediction == wanted
            return _case_score(
                state,
                "matched" if matched else "mismatched",
                "human disposition reproduced" if matched else "human disposition differs",
            )
        return _case_score(
            state,
            "not_applicable",
            "Do not add requires the Record Inclusion evaluator",
        )
    if rule == "disputed_claims_preserved":
        if prediction is None:
            return _case_score(
                state,
                "not_applicable",
                "settlement requires the Source Discrepancy evaluator",
            )
        wanted = {"settled_value": expected.get("settled_value")}
        matched = prediction == wanted
        return _case_score(
            state,
            "matched" if matched else "mismatched",
            "human settlement reproduced" if matched else "human settlement differs",
        )
    if rule == "statement_scope":
        if prediction is not None:
            wanted = {
                "mode": expected.get("mode"),
                "dependency_ids": expected.get("dependency_ids") or [],
            }
            matched = prediction == wanted
            return _case_score(
                state,
                "matched" if matched else "mismatched",
                "human scope reproduced" if matched else "human scope differs",
            )
        mode = expected.get("mode")
        refs = expected.get("source_conflict_refs") or []
        if mode == "unknown":
            matched = any(
                not (candidate.payload_json.get("fields") or {}).get("conflict_ref")
                for candidate in located
            )
        elif mode == "selected" and len(refs) == 1:
            matched = any(
                (candidate.payload_json.get("fields") or {}).get("conflict_ref")
                == refs[0]
                for candidate in located
            )
        else:
            return _case_score(
                state,
                "not_applicable",
                "scope requires the statement-matcher evaluator",
            )
        return _case_score(
            state,
            "matched" if matched else "mismatched",
            "statement scope reproduced" if matched else "statement scope missing",
        )
    return _case_score(
        state,
        "not_applicable",
        f"no evaluator for scoring rule {rule!r}",
    )


def _case_score(
    state: ExtractionMeasurementCaseState, status: str, detail: str
) -> CaseScore:
    return CaseScore(
        case_state_id=state.id,
        case_key=state.case_key,
        kind=state.kind,
        status=status,
        detail=detail,
    )


def _source_documents(source_identity: dict) -> dict[int, str]:
    return {
        document.get("document_id"): document.get("sha256")
        for document in source_identity.get("documents") or []
        if isinstance(document.get("document_id"), int)
        and isinstance(document.get("sha256"), str)
    }


def _candidate_at_source(
    session: Session, candidate: Candidate, source_identity: dict
) -> bool:
    wanted = {
        (
            document.get("document_id"),
            location.get("page"),
            location.get("quote"),
            location.get("table_row"),
        )
        for document in source_identity.get("documents") or []
        for location in document.get("locations") or []
    }
    return any(
        _candidate_citation_identity(session, candidate, citation) in wanted
        for citation in (candidate.payload_json or {}).get("citations") or []
    )


def record_candidate_correction_case(
    session: Session,
    candidate: Candidate,
    *,
    fields: dict[str, str],
    audit_entry: AuditLog,
) -> ExtractionMeasurementCaseState:
    """Append the exact corrected Candidate reading as human reference data."""

    return _append_case_state(
        session,
        project_id=candidate.project_id,
        case_key=f"candidate:{candidate.id}:correction",
        kind="candidate_correction",
        state="active",
        ruling_type="audit_log",
        ruling_id=audit_entry.id,
        source_identity=_candidate_source_identity(session, candidate),
        expected={
            "scoring_rule": "candidate_fields_include",
            "candidate_kind": candidate.kind,
            "fields": deepcopy(fields),
        },
        recorded_by=audit_entry.human_principal or audit_entry.actor,
    )


def record_do_not_add_case(
    session: Session,
    candidate: Candidate,
    *,
    reason: str,
    ruling_type: str,
    ruling_id: int,
    recorded_by: str,
) -> ExtractionMeasurementCaseState:
    """Append a human ruling that this exact source proposal should be absent."""

    return _append_case_state(
        session,
        project_id=candidate.project_id,
        case_key=f"candidate:{candidate.id}:do-not-add",
        kind="do_not_add",
        state="active",
        ruling_type=ruling_type,
        ruling_id=ruling_id,
        source_identity=_candidate_source_identity(session, candidate),
        expected={
            "scoring_rule": "candidate_disposition",
            "candidate_kind": candidate.kind,
            "expected_disposition": "do_not_add",
            "reason": reason,
        },
        recorded_by=recorded_by,
    )


def record_do_not_add_reversal_case(
    session: Session,
    candidate: Candidate,
    reversal: StatementCoordinationReversal,
    *,
    original_ruling_type: str,
    original_ruling_id: int,
    reason: str,
) -> ExtractionMeasurementCaseState:
    """Append a reversal; reconstruct an absent historical root if necessary."""

    case_key = f"candidate:{candidate.id}:do-not-add"
    predecessor = session.scalars(
        select(ExtractionMeasurementCaseState)
        .where(ExtractionMeasurementCaseState.case_key == case_key)
        .order_by(ExtractionMeasurementCaseState.id.desc())
        .limit(1)
    ).first()
    if predecessor is None:
        predecessor = record_do_not_add_case(
            session,
            candidate,
            reason=reason,
            ruling_type=original_ruling_type,
            ruling_id=original_ruling_id,
            recorded_by=reversal.recorded_by,
        )
    return _append_case_state(
        session,
        project_id=candidate.project_id,
        case_key=case_key,
        kind="do_not_add",
        state="reversed",
        ruling_type="statement_coordination_reversal",
        ruling_id=reversal.id,
        source_identity=predecessor.source_identity_json,
        expected=predecessor.expected_json,
        recorded_by=reversal.recorded_by,
    )


def record_dispute_settlement_case(
    session: Session,
    dependency: Dependency,
    settlement: DisputeSettlement,
    *,
    claims,
) -> ExtractionMeasurementCaseState:
    """Append one human conclusion while retaining every source claim it saw."""

    documents_by_id = _documents_for_project(
        session,
        dependency.project_id,
        {claim.document_id for claim in claims},
    )
    grouped: dict[int, list[dict]] = defaultdict(list)
    expected_claims = []
    for claim in claims:
        location = {
            "kind": "page_passage",
            "page": claim.page_no,
            "quote": claim.quote,
            "assertion_id": claim.assertion_id,
        }
        grouped[claim.document_id].append(location)
        expected_claims.append(
            {
                "assertion_id": claim.assertion_id,
                "document_id": claim.document_id,
                "page": claim.page_no,
                "quote": claim.quote,
                "asserted_value": claim.value,
            }
        )
    return _append_case_state(
        session,
        project_id=dependency.project_id,
        case_key=(
            f"dependency:{dependency.id}:dispute:{settlement.field_name}"
        ),
        kind="source_discrepancy_settlement",
        state="active",
        ruling_type="dispute_settlement",
        ruling_id=settlement.id,
        source_identity={
            "candidate_id": None,
            "extraction_run_id": None,
            "documents": [
                {
                    "document_id": document_id,
                    "sha256": documents_by_id[document_id].sha256,
                    "locations": grouped[document_id],
                }
                for document_id in sorted(grouped)
            ],
        },
        expected={
            "scoring_rule": "disputed_claims_preserved",
            "field_name": settlement.field_name,
            "settled_value": settlement.settled_value,
            "claims": expected_claims,
        },
        recorded_by=settlement.settled_by,
    )


def record_statement_fact_correction_case(
    session: Session,
    candidate: Candidate,
    statement: ExternalPartyStatement,
    *,
    evidence,
    recorded_by: str,
) -> ExtractionMeasurementCaseState:
    """Append the supported factual successor as a model-replay case."""

    affected_party = session.get(ExternalOrg, statement.affected_external_org_id)
    fields: dict = {
        "event_type": statement.event_type,
        "description": statement.description,
    }
    if statement.event_date is not None:
        fields["event_date"] = statement.event_date.isoformat()
    if affected_party is not None:
        fields["external_org"] = affected_party.name
    if statement.stated_party:
        fields["stated_party"] = statement.stated_party
    if statement.new_timing is not None:
        fields["committed_date"] = _timing_json(statement.new_timing)
    return _append_case_state(
        session,
        project_id=candidate.project_id,
        case_key=f"statement-lineage:{statement.commitment_lineage_id}:facts",
        kind="statement_fact_correction",
        state="active",
        ruling_type="statement_event",
        ruling_id=statement.id,
        source_identity=_evidence_source_identity(
            session,
            candidate,
            evidence,
        ),
        expected={
            "scoring_rule": "candidate_fields_include",
            "candidate_kind": "event",
            "fields": fields,
        },
        recorded_by=recorded_by,
    )


def record_statement_scope_correction_case(
    session: Session,
    candidate: Candidate,
    statement: ExternalPartyStatement,
    decision: CommitmentScopeDecision,
    *,
    recorded_by: str,
) -> ExtractionMeasurementCaseState:
    """Append one supported statement-scope correction for future matchers."""

    dependency_ids = list(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id)
            .where(CommitmentScopeMembership.scope_decision_id == decision.id)
            .order_by(CommitmentScopeMembership.dependency_id)
        ).all()
    )
    dependencies = session.scalars(
        select(Dependency)
        .where(Dependency.id.in_(dependency_ids or {0}))
        .order_by(Dependency.id)
    ).all()
    return _append_case_state(
        session,
        project_id=candidate.project_id,
        case_key=f"statement-lineage:{statement.commitment_lineage_id}:scope",
        kind="statement_scope_correction",
        state="active",
        ruling_type="statement_scope_decision",
        ruling_id=decision.id,
        source_identity=_candidate_source_identity(session, candidate),
        expected={
            "scoring_rule": "statement_scope",
            "candidate_kind": "event",
            "mode": decision.scope_mode,
            "dependency_ids": dependency_ids,
            "dependency_refs": [dependency.ref_code for dependency in dependencies],
            "source_conflict_refs": [
                dependency.source_ref
                for dependency in dependencies
                if dependency.source_ref
            ],
        },
        recorded_by=recorded_by,
    )


def _append_case_state(
    session: Session,
    *,
    project_id: int,
    case_key: str,
    kind: str,
    state: str,
    ruling_type: str,
    ruling_id: int,
    source_identity: dict,
    expected: dict,
    recorded_by: str,
) -> ExtractionMeasurementCaseState:
    predecessor = session.scalars(
        select(ExtractionMeasurementCaseState)
        .where(ExtractionMeasurementCaseState.case_key == case_key)
        .order_by(ExtractionMeasurementCaseState.id.desc())
        .limit(1)
    ).first()
    value = ExtractionMeasurementCaseState(
        public_id=str(uuid4()),
        project_id=project_id,
        case_key=case_key,
        predecessor_state_id=predecessor.id if predecessor else None,
        kind=kind,
        state=state,
        ruling_type=ruling_type,
        ruling_id=ruling_id,
        source_identity_json=deepcopy(source_identity),
        expected_json=deepcopy(expected),
        recorded_by=recorded_by,
    )
    session.add(value)
    session.flush([value])
    return value


def _candidate_source_identity(session: Session, candidate: Candidate) -> dict:
    grouped: dict[int, list[dict]] = defaultdict(list)
    payload = candidate.payload_json or {}
    location_kind = (
        "worksheet_row"
        if payload.get("text_source") == "cells"
        else "page_passage"
    )
    for citation in payload.get("citations") or []:
        document_id = citation.get("document_id")
        page = citation.get("page")
        quote = citation.get("quote")
        if (
            isinstance(document_id, bool)
            or not isinstance(document_id, int)
            or isinstance(page, bool)
            or not isinstance(page, int)
            or page <= 0
            or not isinstance(quote, str)
            or not quote.strip()
        ):
            raise ValueError("measurement case requires an exact cited source location")
        location = {"kind": location_kind, "page": page, "quote": quote}
        if location_kind == "worksheet_row":
            table_row = _candidate_table_row(session, candidate, citation)
            if table_row is None:
                raise ValueError(
                    "spreadsheet measurement case requires an exact table row"
                )
            location["table_row"] = table_row
        grouped[document_id].append(location)
    if not grouped:
        raise ValueError("measurement case requires at least one cited source location")

    by_id = _documents_for_project(
        session, candidate.project_id, set(grouped)
    )
    return {
        "candidate_id": candidate.id,
        "extraction_run_id": candidate.extraction_run_id,
        "documents": [
            {
                "document_id": document_id,
                "sha256": by_id[document_id].sha256,
                "locations": grouped[document_id],
            }
            for document_id in sorted(grouped)
        ],
    }


def _evidence_source_identity(session: Session, candidate: Candidate, evidence) -> dict:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for item in evidence:
        grouped[item.document_id].append(
            {
                "kind": "page_passage",
                "page": item.page_no,
                "quote": item.quote,
            }
        )
    if not grouped:
        raise ValueError("measurement case requires supporting source evidence")
    documents = _documents_for_project(
        session, candidate.project_id, set(grouped)
    )
    return {
        "candidate_id": candidate.id,
        "extraction_run_id": candidate.extraction_run_id,
        "documents": [
            {
                "document_id": document_id,
                "sha256": documents[document_id].sha256,
                "locations": grouped[document_id],
            }
            for document_id in sorted(grouped)
        ],
    }


def _timing_json(timing) -> dict:
    return {
        "text": timing.text,
        "precision": timing.precision,
        "start_date": timing.start_date.isoformat() if timing.start_date else None,
        "end_date": timing.end_date.isoformat() if timing.end_date else None,
    }


def _documents_for_project(
    session: Session, project_id: int, document_ids: set[int]
) -> dict[int, Document]:
    documents = session.scalars(
        select(Document).where(Document.id.in_(document_ids or {0}))
    ).all()
    by_id = {document.id: document for document in documents}
    if set(by_id) != document_ids or any(
        document.project_id != project_id for document in documents
    ):
        raise ValueError("measurement case source is outside its project")
    return by_id


def _candidate_citation_identity(
    session: Session, candidate: Candidate, citation: dict
) -> tuple[int | None, int | None, str | None, int | None]:
    table_row = None
    if (candidate.payload_json or {}).get("text_source") == "cells":
        table_row = _candidate_table_row(session, candidate, citation)
    return (
        citation.get("document_id"),
        citation.get("page"),
        citation.get("quote"),
        table_row,
    )


def _candidate_table_row(
    session: Session, candidate: Candidate, citation: dict
) -> int | None:
    direct = citation.get("table_row")
    if isinstance(direct, int) and not isinstance(direct, bool) and direct > 0:
        return direct
    if candidate.extraction_run_id is None:
        return None
    run = session.get(ExtractionRun, candidate.extraction_run_id)
    receipt = run.row_accounting_json if run is not None else None
    if not isinstance(receipt, dict) or receipt.get("reader_path") != "spreadsheet_cells":
        return None
    candidate_ids = list(
        session.scalars(
            select(Candidate.id)
            .where(Candidate.extraction_run_id == candidate.extraction_run_id)
            .order_by(Candidate.id)
        ).all()
    )
    extracted_rows = [
        row
        for row in receipt.get("rows") or []
        if row.get("disposition") == "extracted"
    ]
    try:
        ordinal = candidate_ids.index(candidate.id)
        row_number = extracted_rows[ordinal].get("row_number")
    except (ValueError, IndexError, AttributeError):
        return None
    return (
        row_number
        if isinstance(row_number, int)
        and not isinstance(row_number, bool)
        and row_number > 0
        else None
    )
