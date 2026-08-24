"""Resolve one support transfer back to its attributable Admission.

Supersession Review routes current work while the support-transfer proof freezes
one exact mutation input.  Both need the same recursive receipt validation, but
neither should import the other's implementation.  This module owns that shared
read: given durable Admission and transfer receipts, it either returns one exact
lineage or a stable refusal reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.extraction_runs import candidate_input_snapshot
from corridor.models import (
    Candidate,
    Dependency,
    DependencyEvidenceSufficiency,
    EvidenceLink,
    RevisionComparisonFinding,
)
from corridor.operative_support import (
    SupersededOperativeScope,
    evidence_is_scoped_to_dependency,
    readiness_frontier_before_audit,
)
from corridor.revision_comparison import (
    RevisionComparisonError,
    RevisionComparisonReadback,
    read_revision_comparison,
)


class UnsafeSuccessorCitation(ValueError):
    """An immutable Candidate input does not prove one exact citation."""


class CandidateInput(TypedDict, total=False):
    """Immutable extraction input persisted on a Revision Comparison receipt."""

    candidate_id: int
    source_document_id: int
    extraction_run_id: int
    citations_verified: bool
    payload_json: dict


class VerifiedCitation(TypedDict):
    """The exact citation a support transfer may materialize as Evidence."""

    document_id: int
    page: int
    quote: str
    verified: bool


@dataclass(frozen=True)
class AdmissionLineage:
    """One current Candidate traced to its original attributable Admission."""

    origin: audit.AdmissionRecord
    candidate: Candidate
    admitted_fields: dict | None
    latest_support_transfer_audit_id: int | None = None


def input_by_candidate_id(
    inputs: tuple[dict, ...], candidate_id: int
) -> CandidateInput | None:
    """Return one exact immutable input, refusing missing or duplicate identity."""
    matches = tuple(
        item for item in inputs if item.get("candidate_id") == candidate_id
    )
    return cast(CandidateInput, matches[0]) if len(matches) == 1 else None


def finding_by_id(
    readback: RevisionComparisonReadback, finding_id: int
) -> RevisionComparisonFinding | None:
    """Return one exact Comparison finding, refusing missing or duplicate identity."""
    matches = tuple(
        finding for finding in readback.findings if finding.id == finding_id
    )
    return matches[0] if len(matches) == 1 else None


def verified_successor_citation(
    candidate_input: CandidateInput, successor_document_id: int | None
) -> VerifiedCitation:
    """Return the one immutable, exact, verified successor citation."""
    payload = candidate_input.get("payload_json")
    citations = (
        tuple(payload.get("citations") or ())
        if isinstance(payload, dict)
        else ()
    )
    if candidate_input.get("citations_verified") is not True or len(citations) != 1:
        raise UnsafeSuccessorCitation(
            "support transfer requires exactly one immutable verified citation"
        )
    citation = citations[0]
    if (
        not isinstance(citation, dict)
        or citation.get("document_id") != successor_document_id
        or citation.get("verified") is not True
        or isinstance(citation.get("page"), bool)
        or not isinstance(citation.get("page"), int)
        or citation["page"] <= 0
        or not isinstance(citation.get("quote"), str)
        or not citation["quote"].strip()
    ):
        raise UnsafeSuccessorCitation(
            "the immutable successor citation is not exact and verified"
        )
    return cast(VerifiedCitation, citation)


def has_one_verified_input_citation(
    candidate_input: CandidateInput,
    *,
    document_id: int,
    page_no: int,
    quote: str,
) -> bool:
    """Match one Evidence identity to exactly one immutable citation."""
    payload = candidate_input.get("payload_json")
    if (
        candidate_input.get("citations_verified") is not True
        or not isinstance(payload, dict)
        or not isinstance(payload.get("citations"), list)
    ):
        return False
    matches = tuple(
        citation
        for citation in payload["citations"]
        if isinstance(citation, dict)
        and citation.get("verified") is True
        and citation.get("document_id") == document_id
        and citation.get("page") == page_no
        and citation.get("quote") == quote
    )
    return len(matches) == 1


def scope_has_one_verified_input_citation(
    scope: SupersededOperativeScope, candidate_input: CandidateInput
) -> bool:
    """Bind one moved support scope to one immutable Candidate citation."""
    if scope.evidence.verified is not True:
        return False
    return has_one_verified_input_citation(
        candidate_input,
        document_id=scope.evidence.document_id,
        page_no=scope.evidence.page_no,
        quote=scope.evidence.quote,
    )


def admission_for_scope(
    session: Session,
    dependency: Dependency,
    predecessor_document_id: int,
    admission_records: tuple[audit.AdmissionRecord, ...],
    support_transfer_records: tuple[audit.SupportTransferRecord, ...],
) -> tuple[AdmissionLineage | None, tuple[int, ...], str | None]:
    """Resolve one predecessor scope to one attributable Admission lineage."""
    candidate_ids: set[int] = set()
    admission_corrupt = False
    transfer_corrupt = False
    for record in admission_records:
        if not record.candidate_link_valid:
            admission_corrupt = True
            continue
        candidate = session.get(Candidate, record.candidate_id)
        if candidate is None:
            admission_corrupt = True
            continue
        if candidate.source_document_id == predecessor_document_id:
            candidate_ids.add(candidate.id)
    for record in support_transfer_records:
        if not record.identity_valid:
            transfer_corrupt = True
        if record.successor_candidate_id is None:
            continue
        candidate = session.get(Candidate, record.successor_candidate_id)
        if candidate is None:
            transfer_corrupt = True
            continue
        if candidate.source_document_id == predecessor_document_id:
            candidate_ids.add(candidate.id)

    ordered_candidate_ids = tuple(sorted(candidate_ids))
    if transfer_corrupt:
        return None, ordered_candidate_ids, "reconfirmation_history_corrupt"
    if admission_corrupt:
        return None, ordered_candidate_ids, "admission_history_corrupt"
    if len(ordered_candidate_ids) != 1:
        return None, ordered_candidate_ids, (
            "admission_link_unavailable"
            if not ordered_candidate_ids
            else "admission_link_ambiguous"
        )

    admission, reason = _candidate_lineage(
        session,
        dependency=dependency,
        candidate_id=ordered_candidate_ids[0],
        expected_document_id=predecessor_document_id,
        admission_records=admission_records,
        support_transfer_records=support_transfer_records,
        seen_transfer_ids=frozenset(),
    )
    if admission is None:
        return None, ordered_candidate_ids, reason
    return admission, ordered_candidate_ids, None


def _candidate_lineage(
    session: Session,
    *,
    dependency: Dependency,
    candidate_id: int,
    expected_document_id: int,
    admission_records: tuple[audit.AdmissionRecord, ...],
    support_transfer_records: tuple[audit.SupportTransferRecord, ...],
    seen_transfer_ids: frozenset[int],
) -> tuple[AdmissionLineage | None, str | None]:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        return None, "admission_history_corrupt"
    if candidate.source_document_id != expected_document_id:
        return None, "admission_document_mismatch"
    if candidate.project_id != dependency.project_id:
        return None, "admission_project_mismatch"

    direct = tuple(
        record for record in admission_records if record.candidate_id == candidate_id
    )
    if len(direct) > 1:
        return None, "admission_link_ambiguous"
    if direct:
        record = direct[0]
        if not record.attributable:
            return None, "admission_not_attributable"
        state_consistent = (
            candidate.state == "accepted" and candidate.merged_into is None
            if record.action == audit.ACCEPT_CANDIDATE
            else candidate.state == "merged" and candidate.merged_into == dependency.id
        )
        if not state_consistent:
            return None, "admission_state_inconsistent"
        return AdmissionLineage(record, candidate, record.fields), None

    prior = tuple(
        record
        for record in support_transfer_records
        if record.successor_candidate_id == candidate_id
    )
    if len(prior) != 1:
        return None, (
            "admission_link_unavailable" if not prior else "admission_link_ambiguous"
        )
    transfer = prior[0]
    if not transfer.identity_valid:
        return None, "reconfirmation_history_corrupt"
    if transfer.audit_id in seen_transfer_ids:
        return None, "reconfirmation_lineage_cycle"
    if not transfer.attributable:
        return None, "admission_not_attributable"
    if candidate.state != "pending" or candidate.merged_into is not None:
        return None, "admission_state_inconsistent"

    assert transfer.comparison_id is not None
    assert transfer.finding_id is not None
    assert transfer.predecessor_candidate_id is not None
    assert transfer.successor_candidate_id is not None
    assert transfer.new_evidence_link_id is not None
    assert transfer.origin_admission_audit_id is not None
    if (
        transfer.origin_admission_audit_id >= transfer.audit_id
        or (
            transfer.predecessor_reconfirmation_audit_id is not None
            and transfer.predecessor_reconfirmation_audit_id >= transfer.audit_id
        )
    ):
        return None, "reconfirmation_lineage_chronology_invalid"
    try:
        readback = read_revision_comparison(session, transfer.comparison_id)
    except RevisionComparisonError:
        return None, "reconfirmation_history_corrupt"
    comparison = readback.comparison
    if (
        comparison.project_id != dependency.project_id
        or comparison.successor_document_id != expected_document_id
        or candidate.extraction_run_id != comparison.successor_extraction_run_id
    ):
        return None, "reconfirmation_identity_mismatch"
    finding = finding_by_id(readback, transfer.finding_id)
    if (
        finding is None
        or finding.state != "unchanged"
        or finding.predecessor_candidate_ids != [transfer.predecessor_candidate_id]
        or finding.successor_candidate_ids != [transfer.successor_candidate_id]
    ):
        return None, "reconfirmation_identity_mismatch"

    predecessor, reason = _candidate_lineage(
        session,
        dependency=dependency,
        candidate_id=transfer.predecessor_candidate_id,
        expected_document_id=comparison.predecessor_document_id,
        admission_records=admission_records,
        support_transfer_records=support_transfer_records,
        seen_transfer_ids=seen_transfer_ids | {transfer.audit_id},
    )
    if predecessor is None:
        return None, reason
    if (
        predecessor.origin.audit_id != transfer.origin_admission_audit_id
        or predecessor.latest_support_transfer_audit_id
        != transfer.predecessor_reconfirmation_audit_id
    ):
        return None, "reconfirmation_lineage_mismatch"

    predecessor_input = input_by_candidate_id(
        readback.predecessor_inputs, transfer.predecessor_candidate_id
    )
    successor_input = input_by_candidate_id(
        readback.successor_inputs, transfer.successor_candidate_id
    )
    if predecessor_input is None or successor_input is None:
        return None, "reconfirmation_identity_mismatch"
    if predecessor.candidate.extraction_run_id != comparison.predecessor_extraction_run_id:
        return None, "reconfirmation_identity_mismatch"
    if not _sources_match_receipt(
        session,
        dependency_id=dependency.id,
        predecessor_document_id=comparison.predecessor_document_id,
        successor_document_id=comparison.successor_document_id,
        predecessor_input=predecessor_input,
        record=transfer,
    ):
        return None, "reconfirmation_history_corrupt"
    predecessor_fields = (predecessor_input.get("payload_json") or {}).get("fields")
    if not isinstance(predecessor_fields, dict):
        return None, "reconfirmation_history_corrupt"
    if (
        predecessor.admitted_fields != predecessor_fields
        and transfer.action != audit.AUTOMATIC_CARRY_FORWARD
    ):
        return None, "reconfirmation_lineage_changed"
    live_input = {
        **candidate_input_snapshot(candidate),
        "extraction_run_id": candidate.extraction_run_id,
    }
    if live_input != successor_input:
        return None, "reconfirmation_candidate_changed"
    try:
        citation = verified_successor_citation(successor_input, expected_document_id)
    except UnsafeSuccessorCitation:
        return None, "reconfirmation_provenance_unsafe"
    evidence = session.get(EvidenceLink, transfer.new_evidence_link_id)
    if (
        evidence is None
        or evidence.dependency_id != dependency.id
        or evidence.document_id != expected_document_id
        or evidence.verified is not True
        or evidence.page_no != citation["page"]
        or evidence.quote != citation["quote"]
    ):
        return None, "reconfirmation_evidence_mismatch"
    successor_fields = (successor_input.get("payload_json") or {}).get("fields")
    if not isinstance(successor_fields, dict):
        return None, "reconfirmation_history_corrupt"
    if (
        transfer.action == audit.AUTOMATIC_CARRY_FORWARD
        and successor_fields != predecessor.admitted_fields
    ):
        return None, "reconfirmation_lineage_changed"
    return (
        AdmissionLineage(
            origin=predecessor.origin,
            candidate=candidate,
            admitted_fields=successor_fields,
            latest_support_transfer_audit_id=transfer.audit_id,
        ),
        None,
    )


def _sources_match_receipt(
    session: Session,
    *,
    dependency_id: int,
    predecessor_document_id: int,
    successor_document_id: int,
    predecessor_input: CandidateInput,
    record: audit.SupportTransferRecord,
) -> bool:
    current_readiness_ids = audit.current_readiness_evidence_ids_from_audit(
        session, dependency_id
    )
    stored_current_readiness_ids = frozenset(
        session.scalars(
            select(DependencyEvidenceSufficiency.evidence_link_id).where(
                DependencyEvidenceSufficiency.dependency_id == dependency_id
            )
        ).all()
    )
    if current_readiness_ids is None or current_readiness_ids != stored_current_readiness_ids:
        return False
    readiness_frontier = readiness_frontier_before_audit(
        session,
        dependency_id,
        record.audit_id,
        known_terminal_document_id=successor_document_id,
    )
    if readiness_frontier is None:
        return False
    if {
        scope.evidence_link_id
        for scope in record.operative_scopes
        if scope.role == "readiness"
    } != {support.evidence_link_id for support in readiness_frontier}:
        return False
    for scope in record.operative_scopes:
        evidence = session.get(EvidenceLink, scope.evidence_link_id)
        if (
            evidence is None
            or not evidence_is_scoped_to_dependency(session, evidence, dependency_id)
            or evidence.document_id != predecessor_document_id
            or evidence.verified is not True
            or not has_one_verified_input_citation(
                predecessor_input,
                document_id=evidence.document_id,
                page_no=evidence.page_no,
                quote=evidence.quote,
            )
        ):
            return False
    return True
