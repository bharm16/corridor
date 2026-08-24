"""One exact proof before human or policy-directed Operative Support transfer.

Supersession Review and Automatic Carry-Forward used to reconstruct the same
Admission lineage, immutable Revision Comparison inputs, live successor
Candidate, and verified citation through private helpers in separate modules.
That made the machine path depend on the review module's implementation.

This module freezes those facts behind one interface.  It does not decide who
may move support and it does not write a receipt: human Reconfirmation and the
Carry-Forward Policy remain distinct authorities and retain their own records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from corridor import audit, policy
from corridor.extraction_runs import candidate_input_snapshot
from corridor.models import (
    Candidate,
    Dependency,
    EvidenceLink,
    RevisionComparisonFinding,
    RevisionComparisonRun,
)
from corridor.operative_support import (
    SupportTransferMutation,
    SupersededOperativeScope,
    evidence_is_scoped_to_dependency,
    transfer_operative_scopes_under_lock,
)
from corridor.revision_comparison import (
    RevisionComparisonError,
    RevisionComparisonReadback,
    read_revision_comparison,
)
from corridor.support_transfer_lineage import (
    admission_for_scope,
    finding_by_id,
    input_by_candidate_id,
    verified_successor_citation,
)


ScopeFingerprint = tuple[tuple[str, str | None, int], ...]


class SupportTransferReview(Protocol):
    """The immutable worklist facts needed to prove one support transfer."""

    dependency_id: int | None
    predecessor_document_id: int | None
    successor_document_id: int | None
    comparison_id: int | None
    finding_id: int | None
    superseded_scopes: tuple[SupersededOperativeScope, ...]

    @property
    def predecessor_candidate_id(self) -> int | None: ...

    @property
    def successor_candidate_id(self) -> int | None: ...

    @property
    def scope_fingerprint(self) -> ScopeFingerprint: ...


class SupportTransferProofRefusal(ValueError):
    """The selected review no longer proves one safe support transfer."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProvenAdmission:
    """The original attributable Admission carried through support history."""

    origin: audit.AdmissionRecord
    admitted_fields: dict | None
    latest_support_transfer_audit_id: int | None


@dataclass(frozen=True)
class SupportTransferProof:
    """One current, immutable reading sufficient to move Operative Support."""

    dependency: Dependency
    comparison: RevisionComparisonRun
    finding: RevisionComparisonFinding
    admission: ProvenAdmission
    successor_candidate: Candidate
    predecessor_input: dict
    successor_input: dict
    citation: dict
    successor_document_id: int
    scopes: tuple[SupersededOperativeScope, ...]
    scope_fingerprint: ScopeFingerprint


def prove_support_transfer(
    session: Session,
    *,
    project_id: int,
    review: SupportTransferReview,
    expected_matcher_version: str | None = None,
    expected_matcher_config_sha256: str | None = None,
    require_exact_admitted_fields: bool = False,
) -> SupportTransferProof:
    """Freeze the exact Admission, Comparison, Candidate, and Evidence reading.

    The caller takes the project lock before calling this function.  Optional
    matcher and field checks are policy predicates used by Automatic
    Carry-Forward; human Reconfirmation consumes the already-routed review.
    """
    dependency = session.get(Dependency, review.dependency_id)
    if dependency is None or dependency.project_id != project_id:
        raise SupportTransferProofRefusal("dependency_unavailable")
    if (
        review.predecessor_document_id is None
        or review.successor_document_id is None
        or review.comparison_id is None
        or review.finding_id is None
        or review.predecessor_candidate_id is None
        or review.successor_candidate_id is None
    ):
        raise SupportTransferProofRefusal("comparison_not_one_to_one")

    try:
        readback: RevisionComparisonReadback = read_revision_comparison(
            session, review.comparison_id
        )
    except RevisionComparisonError as exc:
        raise SupportTransferProofRefusal("comparison_integrity_failure") from exc
    comparison = readback.comparison
    if (
        expected_matcher_version is not None
        and comparison.matcher_version != expected_matcher_version
    ):
        raise SupportTransferProofRefusal("comparison_policy_unapproved")
    if (
        expected_matcher_config_sha256 is not None
        and policy.canonical_sha256(comparison.matcher_config)
        != expected_matcher_config_sha256
    ):
        raise SupportTransferProofRefusal("comparison_policy_unapproved")

    finding = finding_by_id(readback, review.finding_id)
    if finding is None:
        raise SupportTransferProofRefusal("predecessor_finding_unavailable")
    if finding.state != "unchanged":
        raise SupportTransferProofRefusal(f"comparison_{finding.state}")

    admissions = audit.admission_records_for_dependencies(
        session, (dependency.id,)
    ).get(dependency.id, ())
    support_transfers = audit.support_transfer_records_for_dependencies(
        session, (dependency.id,)
    ).get(dependency.id, ())
    admission, _, admission_reason = admission_for_scope(
        session,
        dependency,
        review.predecessor_document_id,
        admissions,
        support_transfers,
    )
    if admission is None:
        raise SupportTransferProofRefusal(
            admission_reason or "admission_link_unavailable"
        )

    predecessor_input = input_by_candidate_id(
        readback.predecessor_inputs, review.predecessor_candidate_id
    )
    successor_input = input_by_candidate_id(
        readback.successor_inputs, review.successor_candidate_id
    )
    if predecessor_input is None or successor_input is None:
        raise SupportTransferProofRefusal("comparison_input_unavailable")
    successor_fields = (successor_input.get("payload_json") or {}).get("fields")
    if require_exact_admitted_fields and (
        not isinstance(admission.admitted_fields, dict)
        or not isinstance(successor_fields, dict)
        or successor_fields != admission.admitted_fields
    ):
        raise SupportTransferProofRefusal("successor_fields_not_exact")

    successor = session.get(Candidate, review.successor_candidate_id)
    if successor is None:
        raise SupportTransferProofRefusal("successor_not_actionable")
    live_input = {
        **candidate_input_snapshot(successor),
        "extraction_run_id": successor.extraction_run_id,
    }
    if live_input != successor_input:
        raise SupportTransferProofRefusal("successor_candidate_changed")
    try:
        citation = verified_successor_citation(
            successor_input, review.successor_document_id
        )
    except ValueError as exc:
        raise SupportTransferProofRefusal("successor_provenance_unsafe") from exc

    for scope in review.superseded_scopes:
        if scope.role == "publication":
            continue
        if scope.role != "readiness":
            raise SupportTransferProofRefusal(
                "unsupported_operative_support_role"
            )
        prior_readiness = session.get(
            EvidenceLink, scope.evidence.evidence_link_id
        )
        if not evidence_is_scoped_to_dependency(
            session,
            prior_readiness,
            dependency.id,
            require_sufficiency=True,
        ):
            raise SupportTransferProofRefusal("readiness_source_changed")

    return SupportTransferProof(
        dependency=dependency,
        comparison=comparison,
        finding=finding,
        admission=ProvenAdmission(
            origin=admission.origin,
            admitted_fields=admission.admitted_fields,
            latest_support_transfer_audit_id=(
                admission.latest_support_transfer_audit_id
            ),
        ),
        successor_candidate=successor,
        predecessor_input=predecessor_input,
        successor_input=successor_input,
        citation=citation,
        successor_document_id=review.successor_document_id,
        scopes=review.superseded_scopes,
        scope_fingerprint=review.scope_fingerprint,
    )


def apply_proven_support_transfer(
    session: Session,
    proof: SupportTransferProof,
    *,
    designated_by: str,
) -> SupportTransferMutation:
    """Move only the scopes frozen by one proof under the caller's authority."""
    return transfer_operative_scopes_under_lock(
        session,
        dependency_id=proof.dependency.id,
        successor_document_id=proof.successor_document_id,
        citation=proof.citation,
        scopes=proof.scopes,
        designated_by=designated_by,
    )
