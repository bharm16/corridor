"""Live Supersession Review and human Reconfirmation.

Revision Comparisons are immutable exact-run receipts.  They are useful
evidence for a reviewer, but they are not the live answer to "what remains to
be reviewed?"  This module derives that answer from declared Supersession and
current Operative Support, then enriches it only when one integrity-checked
Comparison exists for the exact Active Runs.

Reconfirmation is deliberately narrower than Admission.  It can move every
role-scoped support designation for one unchanged row to verified evidence on
the terminal successor revision; it cannot admit a Candidate, rewrite a
Dependency conclusion, or infer which run, comparison, citation, or admission
history the reviewer meant.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.extraction_runs import candidate_input_snapshot, is_completed_run
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    Document,
    EvidenceLink,
    ExtractionRun,
    RevisionComparisonFinding,
)
from corridor.operative_support import (
    ResolvedSupport,
    SupersededOperativeScope,
    designate_publication_support,
    resolve_operative_support,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.revision_comparison import (
    RevisionComparisonError,
    RevisionComparisonReadback,
    list_revision_comparisons,
    read_revision_comparison,
)
from corridor.supersession import actionable_candidate, actionable_candidate_query


ReviewRoute = Literal["ordinary", "reconfirmation"]
ScopeFingerprint = tuple[tuple[str, str | None, int], ...]


class ReconfirmationUnavailable(ValueError):
    """The selected row is not a safe, current Reconfirmation decision."""


def normalize_scope_fingerprint(value: object) -> ScopeFingerprint:
    """Validate and canonicalize a domain or decoded-JSON fingerprint."""

    if not isinstance(value, (list, tuple)):
        raise ValueError("scope fingerprint must be a sequence")
    normalized: list[tuple[str, str | None, int]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise ValueError("scope fingerprint entries must have three values")
        role, field_name, evidence_link_id = item
        if not isinstance(role, str) or role not in {
            "publication",
            "readiness",
        }:
            raise ValueError("scope fingerprint has an unsupported role")
        if field_name is not None and (
            not isinstance(field_name, str)
            or not field_name
            or field_name != field_name.strip()
            or len(field_name) > 64
        ):
            raise ValueError("scope fingerprint has an invalid field name")
        if role == "readiness" and field_name is not None:
            raise ValueError("readiness scope cannot name a field")
        if (
            isinstance(evidence_link_id, bool)
            or not isinstance(evidence_link_id, int)
            or evidence_link_id <= 0
        ):
            raise ValueError("scope fingerprint has an invalid evidence id")
        normalized.append((role, field_name, evidence_link_id))
    if len(set(normalized)) != len(normalized):
        raise ValueError("scope fingerprint contains duplicate entries")
    return tuple(
        sorted(
            normalized,
            key=lambda item: (item[0], item[1] or "", item[2]),
        )
    )


@dataclass(frozen=True)
class SupersessionReview:
    """One live unit of reviewer work; never a persisted queue row."""

    dependency_id: int | None
    predecessor_document_id: int | None
    successor_document_id: int | None
    predecessor_candidate_ids: tuple[int, ...]
    successor_candidate_ids: tuple[int, ...]
    status: str
    comparison_id: int | None = None
    finding_id: int | None = None
    superseded_scopes: tuple[SupersededOperativeScope, ...] = ()
    reason: str | None = None
    route: ReviewRoute = "ordinary"

    @property
    def reconfirmation_available(self) -> bool:
        return self.route == "reconfirmation"

    @property
    def predecessor_candidate_id(self) -> int | None:
        if len(self.predecessor_candidate_ids) == 1:
            return self.predecessor_candidate_ids[0]
        return None

    @property
    def successor_candidate_id(self) -> int | None:
        if len(self.successor_candidate_ids) == 1:
            return self.successor_candidate_ids[0]
        return None

    @property
    def scope_fingerprint(self) -> ScopeFingerprint:
        """Exact operative scopes this reviewer action was rendered against."""

        return normalize_scope_fingerprint(
            [
                (
                    scope.role,
                    scope.field_name,
                    scope.evidence.evidence_link_id,
                )
                for scope in self.superseded_scopes
            ]
        )


# Kept as an explicit alias because the web read model used this name while
# the domain contract was being implemented.  There is still only one type.
SupersessionReviewItem = SupersessionReview


@dataclass(frozen=True)
class ReviewerWorklist:
    """The two mutually exclusive lanes behind one reviewer interface."""

    reconfirmation: tuple[SupersessionReview, ...]
    ordinary: tuple[SupersessionReview, ...]

    @property
    def reviews(self) -> tuple[SupersessionReview, ...]:
        return self.reconfirmation + self.ordinary


@dataclass(frozen=True)
class _Admission:
    origin: audit.AdmissionRecord
    candidate: Candidate
    admitted_fields: dict | None
    latest_reconfirmation_audit_id: int | None = None
    support_evidence_link_id: int | None = None


def build_reviewer_worklist(
    session: Session, project_id: int
) -> ReviewerWorklist:
    """Derive current work without storing state or selecting by recency."""

    dependencies = tuple(
        session.scalars(
            select(Dependency)
            .where(Dependency.project_id == project_id)
            .order_by(Dependency.id)
        ).all()
    )
    dependency_ids = tuple(dependency.id for dependency in dependencies)
    support_by_dependency = resolve_operative_support(session, dependency_ids)
    admissions_by_dependency = audit.admission_records_for_dependencies(
        session, dependency_ids
    )
    reconfirmations_by_dependency = (
        audit.reconfirmation_records_for_dependencies(session, dependency_ids)
    )
    actionable = {
        candidate.id: candidate
        for candidate in session.scalars(
            actionable_candidate_query(project_id).order_by(Candidate.id)
        ).all()
    }
    already_reconfirmed = audit.reconfirmed_successor_candidate_ids(
        reconfirmations_by_dependency
    )

    ordinary: list[SupersessionReview] = []
    reconfirmation: list[SupersessionReview] = []
    represented_candidate_ids: set[int] = set()

    for dependency in dependencies:
        resolved = support_by_dependency.get(
            dependency.id, ResolvedSupport.empty(dependency.id)
        )
        all_scopes = _terminal_superseded_scopes(
            session, tuple(resolved.superseded_scopes)
        )
        for predecessor_document_id, scopes in _group_scopes(all_scopes).items():
            item = _support_review(
                session,
                project_id=project_id,
                dependency=dependency,
                predecessor_document_id=predecessor_document_id,
                scopes=scopes,
                all_scopes=all_scopes,
                actionable=actionable,
                admission_records=admissions_by_dependency.get(
                    dependency.id, ()
                ),
                reconfirmation_records=reconfirmations_by_dependency.get(
                    dependency.id, ()
                ),
            )
            if already_reconfirmed.intersection(item.successor_candidate_ids):
                item = replace(
                    item,
                    route="ordinary",
                    successor_candidate_ids=tuple(
                        candidate_id
                        for candidate_id in item.successor_candidate_ids
                        if candidate_id not in already_reconfirmed
                    ),
                    reason="successor_candidate_already_reconfirmed",
                )
            represented_candidate_ids.update(item.successor_candidate_ids)
            if item.reconfirmation_available:
                reconfirmation.append(item)
            else:
                ordinary.append(item)

    ordinary, reconfirmation = _demote_ambiguous_successor_links(
        ordinary, reconfirmation
    )

    reconfirmation_ids = {
        candidate_id
        for item in reconfirmation
        for candidate_id in item.successor_candidate_ids
    }
    for candidate_id, candidate in actionable.items():
        if (
            candidate_id in represented_candidate_ids
            or candidate_id in reconfirmation_ids
            or candidate_id in already_reconfirmed
        ):
            continue
        ordinary.append(
            SupersessionReview(
                dependency_id=None,
                predecessor_document_id=None,
                successor_document_id=candidate.source_document_id,
                predecessor_candidate_ids=(),
                successor_candidate_ids=(candidate.id,),
                status="candidate_adjudication",
                reason="current_active_candidate",
            )
        )

    ordinary.sort(key=_review_sort_key)
    reconfirmation.sort(key=_review_sort_key)
    return ReviewerWorklist(
        reconfirmation=tuple(reconfirmation), ordinary=tuple(ordinary)
    )


def _demote_ambiguous_successor_links(
    ordinary: list[SupersessionReview],
    reconfirmation: list[SupersessionReview],
) -> tuple[list[SupersessionReview], list[SupersessionReview]]:
    """Keep a current Candidate out of Reconfirmation when links fan in."""

    reviews = ordinary + reconfirmation
    occurrences: dict[int, int] = {}
    for review in reviews:
        if review.dependency_id is None:
            continue
        for candidate_id in set(review.successor_candidate_ids):
            occurrences[candidate_id] = occurrences.get(candidate_id, 0) + 1
    collisions = {
        candidate_id
        for candidate_id, count in occurrences.items()
        if count > 1
    }
    if not collisions:
        return ordinary, reconfirmation

    resolved_ordinary: list[SupersessionReview] = []
    resolved_reconfirmation: list[SupersessionReview] = []
    for review in reviews:
        if collisions.intersection(review.successor_candidate_ids):
            resolved_ordinary.append(
                replace(
                    review,
                    route="ordinary",
                    reason="successor_candidate_link_ambiguous",
                )
            )
        elif review.reconfirmation_available:
            resolved_reconfirmation.append(review)
        else:
            resolved_ordinary.append(review)
    return resolved_ordinary, resolved_reconfirmation


def ordinary_candidate_ids(session: Session, project_id: int) -> frozenset[int]:
    """Candidate ids that belong to the default ordinary lane now."""

    return frozenset(
        candidate_id
        for item in build_reviewer_worklist(session, project_id).ordinary
        for candidate_id in item.successor_candidate_ids
    )


def ordinary_candidate_for_update(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    historical_document_id: int | None = None,
) -> Candidate | None:
    """Lock and resolve one Candidate through the authoritative lane policy.

    Explicit historical review remains an intentional override.  Default
    writes, however, must be members of the derived ordinary lane so a crafted
    form post cannot admit a row reserved for Reconfirmation or a successor
    row whose support was already reconfirmed.
    """

    session.flush()
    lock_project(session, project_id)
    session.expire_all()
    candidate = actionable_candidate(
        session,
        project_id,
        candidate_id,
        historical_document_id=historical_document_id,
    )
    if candidate is None:
        return None
    if historical_document_id is not None:
        return candidate
    if candidate_id not in ordinary_candidate_ids(session, project_id):
        return None
    return candidate


def reconfirm_operative_support(
    session: Session,
    *,
    project_id: int,
    dependency_id: int,
    predecessor_document_id: int,
    successor_candidate_id: int,
    comparison_id: int,
    finding_id: int,
    scope_fingerprint: ScopeFingerprint,
    principal: HumanPrincipal,
) -> EvidenceLink:
    """Move all safe operative scopes to one exact successor citation.

    Every identifier the reviewer saw is carried into this boundary.  The
    work is fully re-derived under the project lock and enclosed in a
    savepoint, so a stale or malformed request leaves no partial Evidence,
    designation, or audit row.
    """

    principal = require_human_principal(principal)
    try:
        submitted_scope_fingerprint = normalize_scope_fingerprint(
            scope_fingerprint
        )
    except ValueError as exc:
        raise ReconfirmationUnavailable(str(exc)) from exc
    with session.begin_nested():
        dependency = session.get(Dependency, dependency_id)
        if dependency is None or dependency.project_id != project_id:
            raise ReconfirmationUnavailable(
                "Dependency is outside the selected project"
            )

        session.flush()
        lock_project(session, project_id)
        session.expire_all()
        dependency = session.get(Dependency, dependency_id)
        if dependency is None or dependency.project_id != project_id:
            raise ReconfirmationUnavailable(
                "Dependency changed while Reconfirmation was being locked"
            )
        worklist = build_reviewer_worklist(session, project_id)
        matches = tuple(
            item
            for item in worklist.reconfirmation
            if item.dependency_id == dependency_id
            and item.predecessor_document_id == predecessor_document_id
            and item.successor_candidate_ids == (successor_candidate_id,)
            and item.comparison_id == comparison_id
            and item.finding_id == finding_id
            and item.scope_fingerprint == submitted_scope_fingerprint
        )
        if len(matches) != 1:
            raise ReconfirmationUnavailable(
                "the selected Reconfirmation is stale or no longer safe"
            )
        review = matches[0]
        admission_records = audit.admission_records_for_dependencies(
            session, (dependency_id,)
        ).get(dependency_id, ())
        reconfirmation_records = (
            audit.reconfirmation_records_for_dependencies(
                session, (dependency_id,)
            ).get(dependency_id, ())
        )
        admission, _, admission_reason = _admission_for_scope(
            session,
            dependency,
            predecessor_document_id,
            review.superseded_scopes,
            admission_records,
            reconfirmation_records,
        )
        if admission is None:
            raise ReconfirmationUnavailable(
                admission_reason or "Admission lineage is unavailable"
            )

        try:
            readback = read_revision_comparison(session, comparison_id)
        except RevisionComparisonError as exc:
            raise ReconfirmationUnavailable(str(exc)) from exc
        finding = _finding_by_id(readback, finding_id)
        if finding is None:
            raise ReconfirmationUnavailable(
                "the selected comparison finding does not exist"
            )
        successor_input = _input_by_candidate_id(
            readback.successor_inputs, successor_candidate_id
        )
        if successor_input is None:
            raise ReconfirmationUnavailable(
                "the selected successor is not an immutable comparison input"
            )
        citation = _verified_successor_citation(
            successor_input, review.successor_document_id
        )

        readiness_transferred = any(
            scope.role == "readiness" for scope in review.superseded_scopes
        )
        new_evidence = EvidenceLink(
            dependency_id=dependency_id,
            document_id=review.successor_document_id,
            page_no=citation["page"],
            quote=citation["quote"],
            verified=True,
            satisfies_requirement=readiness_transferred,
        )
        session.add(new_evidence)
        session.flush([new_evidence])

        prior_scopes: list[dict[str, object]] = []
        moved_scopes: list[dict[str, object]] = []
        for scope in sorted(review.superseded_scopes, key=_scope_sort_key):
            if scope.role == "publication":
                designate_publication_support(
                    session,
                    dependency_id,
                    new_evidence.id,
                    principal=principal,
                    field_name=scope.field_name,
                )
            elif scope.role == "readiness":
                prior_readiness = session.get(
                    EvidenceLink, scope.evidence.evidence_link_id
                )
                if (
                    prior_readiness is None
                    or prior_readiness.dependency_id != dependency_id
                    or prior_readiness.satisfies_requirement is not True
                ):
                    raise ReconfirmationUnavailable(
                        "the selected readiness support changed during "
                        "Reconfirmation"
                    )
            else:
                raise ReconfirmationUnavailable(
                    f"unsupported operative support role {scope.role!r}"
                )
            prior_scopes.append(
                {
                    "role": scope.role,
                    "field_name": scope.field_name,
                    "evidence_link_id": scope.evidence.evidence_link_id,
                }
            )
            moved_scopes.append(
                {
                    "role": scope.role,
                    "field_name": scope.field_name,
                    "from_evidence_link_id": scope.evidence.evidence_link_id,
                    "to_evidence_link_id": new_evidence.id,
                }
            )

        audit.record(
            session,
            principal=principal,
            action=audit.RECONFIRM_OPERATIVE_SUPPORT,
            entity_type=audit.DEPENDENCY,
            entity_id=dependency_id,
            before={"operative_scopes": prior_scopes},
            after={
                "comparison_id": comparison_id,
                "finding_id": finding_id,
                "predecessor_candidate_id": review.predecessor_candidate_id,
                "successor_candidate_id": successor_candidate_id,
                "new_evidence_link_id": new_evidence.id,
                "scope_fingerprint": [
                    list(item) for item in submitted_scope_fingerprint
                ],
                "origin_admission_audit_id": admission.origin.audit_id,
                "predecessor_reconfirmation_audit_id": (
                    admission.latest_reconfirmation_audit_id
                ),
                "moved_scopes": moved_scopes,
            },
        )
        session.flush()
        return new_evidence


def _support_review(
    session: Session,
    *,
    project_id: int,
    dependency: Dependency,
    predecessor_document_id: int,
    scopes: tuple[SupersededOperativeScope, ...],
    all_scopes: tuple[SupersededOperativeScope, ...],
    actionable: dict[int, Candidate],
    admission_records: tuple[audit.AdmissionRecord, ...],
    reconfirmation_records: tuple[audit.ReconfirmationRecord, ...],
) -> SupersessionReview:
    predecessor = session.get(Document, predecessor_document_id)
    admission, predecessor_candidate_ids, admission_reason = _admission_for_scope(
        session,
        dependency,
        predecessor_document_id,
        scopes,
        admission_records,
        reconfirmation_records,
    )
    base = dict(
        dependency_id=dependency.id,
        predecessor_document_id=predecessor_document_id,
        predecessor_candidate_ids=predecessor_candidate_ids,
        superseded_scopes=scopes,
    )
    if (
        predecessor is None
        or predecessor.project_id != project_id
        or predecessor.superseded_by is None
    ):
        return SupersessionReview(
            **base,
            successor_document_id=None,
            successor_candidate_ids=(),
            status="blocked",
            reason="supersession_registry_unavailable",
        )

    successor = session.get(Document, predecessor.superseded_by)
    if successor is None or successor.project_id != project_id:
        return SupersessionReview(
            **base,
            successor_document_id=None,
            successor_candidate_ids=(),
            status="blocked",
            reason="supersession_registry_unavailable",
        )
    base["successor_document_id"] = successor.id
    if successor.superseded_by is not None:
        return SupersessionReview(
            **base,
            successor_candidate_ids=(),
            status="blocked",
            reason="multi_hop_supersession",
        )

    attempts = tuple(
        session.scalars(
            select(ExtractionRun)
            .where(ExtractionRun.document_id == successor.id)
            .order_by(ExtractionRun.id)
        ).all()
    )
    successor_active = _active_run(session, successor.id)
    if not attempts:
        return SupersessionReview(
            **base,
            successor_candidate_ids=(),
            status="awaiting_extraction",
            reason=admission_reason,
        )
    if successor_active is None:
        completed_exists = any(is_completed_run(run) for run in attempts)
        return SupersessionReview(
            **base,
            successor_candidate_ids=(),
            status=("awaiting_active_run" if completed_exists else "extraction_failed"),
            reason=admission_reason,
        )
    if successor_active.document_id != successor.id or not is_completed_run(
        successor_active
    ):
        return SupersessionReview(
            **base,
            successor_candidate_ids=(),
            status="blocked",
            reason="successor_active_run_invalid",
        )

    active_successor_ids = tuple(
        sorted(
            candidate.id
            for candidate in actionable.values()
            if candidate.source_document_id == successor.id
            and candidate.extraction_run_id == successor_active.id
        )
    )
    predecessor_active = _active_run(session, predecessor.id)
    if predecessor_active is None or not is_completed_run(predecessor_active):
        return SupersessionReview(
            **base,
            successor_candidate_ids=active_successor_ids,
            status="blocked",
            reason="predecessor_active_run_unavailable",
        )

    comparisons = list_revision_comparisons(
        session, predecessor_active.id, successor_active.id
    )
    if not comparisons:
        return SupersessionReview(
            **base,
            successor_candidate_ids=active_successor_ids,
            status="awaiting_comparison",
            reason=admission_reason,
        )
    if len(comparisons) != 1:
        return SupersessionReview(
            **base,
            successor_candidate_ids=active_successor_ids,
            status="comparison_selection_ambiguous",
            reason="multiple_exact_comparisons",
        )

    comparison = comparisons[0]
    try:
        readback = read_revision_comparison(session, comparison.id)
    except RevisionComparisonError:
        return SupersessionReview(
            **base,
            successor_candidate_ids=active_successor_ids,
            status="blocked",
            comparison_id=comparison.id,
            reason="comparison_integrity_failure",
        )

    if admission is None:
        return SupersessionReview(
            **base,
            successor_candidate_ids=(),
            status="comparison_ready",
            comparison_id=comparison.id,
            reason=admission_reason or "admission_link_unavailable",
        )
    findings = tuple(
        finding
        for finding in readback.findings
        if admission.candidate.id in finding.predecessor_candidate_ids
    )
    if len(findings) != 1:
        return SupersessionReview(
            **base,
            successor_candidate_ids=(),
            status="comparison_ready",
            comparison_id=comparison.id,
            reason="predecessor_finding_unavailable",
        )
    finding = findings[0]
    finding_successors = tuple(
        candidate_id
        for candidate_id in finding.successor_candidate_ids
        if candidate_id in actionable
    )
    safety_reason = _reconfirmation_refusal(
        predecessor=predecessor,
        successor=successor,
        admission=admission,
        scopes=scopes,
        all_scopes=all_scopes,
        readback=readback,
        finding=finding,
        actionable=actionable,
    )
    if safety_reason is None:
        return SupersessionReview(
            **base,
            successor_candidate_ids=finding_successors,
            status="unchanged",
            comparison_id=comparison.id,
            finding_id=finding.id,
            route="reconfirmation",
        )
    return SupersessionReview(
        **base,
        successor_candidate_ids=finding_successors,
        status=finding.state,
        comparison_id=comparison.id,
        finding_id=finding.id,
        reason=safety_reason,
    )


def _reconfirmation_refusal(
    *,
    predecessor: Document,
    successor: Document,
    admission: _Admission,
    scopes: tuple[SupersededOperativeScope, ...],
    all_scopes: tuple[SupersededOperativeScope, ...],
    readback: RevisionComparisonReadback,
    finding: RevisionComparisonFinding,
    actionable: dict[int, Candidate],
) -> str | None:
    if successor.superseded_by is not None:
        return "multi_hop_supersession"
    if finding.state != "unchanged":
        return f"comparison_{finding.state}"
    if (
        len(finding.predecessor_candidate_ids) != 1
        or len(finding.successor_candidate_ids) != 1
        or finding.predecessor_candidate_ids[0] != admission.candidate.id
    ):
        return "comparison_not_one_to_one"
    if _scope_signatures(scopes) != _scope_signatures(all_scopes):
        return "partial_scope_transfer"
    if any(
        scope.evidence.document_id != predecessor.id
        or scope.role not in {"publication", "readiness"}
        for scope in all_scopes
    ):
        return "partial_scope_transfer"

    predecessor_input = _input_by_candidate_id(
        readback.predecessor_inputs, admission.candidate.id
    )
    successor_candidate_id = finding.successor_candidate_ids[0]
    successor_input = _input_by_candidate_id(
        readback.successor_inputs, successor_candidate_id
    )
    if predecessor_input is None or successor_input is None:
        return "comparison_input_unavailable"
    if any(
        not _scope_has_one_verified_input_citation(scope, predecessor_input)
        for scope in all_scopes
    ):
        return "predecessor_support_provenance_unsafe"
    if admission.candidate.extraction_run_id != (
        readback.comparison.predecessor_extraction_run_id
    ):
        return "admission_run_mismatch"
    admitted_fields = admission.admitted_fields
    snapshot_fields = (predecessor_input.get("payload_json") or {}).get("fields")
    if not isinstance(admitted_fields, dict) or admitted_fields != snapshot_fields:
        return "admission_fields_changed"

    live_successor = actionable.get(successor_candidate_id)
    if live_successor is None or live_successor.extraction_run_id != (
        readback.comparison.successor_extraction_run_id
    ):
        return "successor_not_actionable"
    live_input = {
        **candidate_input_snapshot(live_successor),
        "extraction_run_id": live_successor.extraction_run_id,
    }
    if live_input != successor_input:
        return "successor_candidate_changed"
    try:
        _verified_successor_citation(successor_input, successor.id)
    except ReconfirmationUnavailable:
        return "successor_provenance_unsafe"
    return None


def _scope_has_one_verified_input_citation(
    scope: SupersededOperativeScope, candidate_input: dict
) -> bool:
    """Bind one moved support scope to one immutable Candidate citation."""

    payload = candidate_input.get("payload_json")
    if (
        candidate_input.get("citations_verified") is not True
        or not isinstance(payload, dict)
        or not isinstance(payload.get("citations"), list)
        or scope.evidence.verified is not True
    ):
        return False
    matches = tuple(
        citation
        for citation in payload["citations"]
        if isinstance(citation, dict)
        and citation.get("verified") is True
        and citation.get("document_id") == scope.evidence.document_id
        and citation.get("page") == scope.evidence.page_no
        and citation.get("quote") == scope.evidence.quote
    )
    return len(matches) == 1


def _admission_for_scope(
    session: Session,
    dependency: Dependency,
    predecessor_document_id: int,
    scopes: tuple[SupersededOperativeScope, ...],
    admission_records: tuple[audit.AdmissionRecord, ...],
    reconfirmation_records: tuple[audit.ReconfirmationRecord, ...],
) -> tuple[_Admission | None, tuple[int, ...], str | None]:
    candidate_ids: set[int] = set()
    corrupt = False
    for record in admission_records:
        if not record.candidate_link_valid:
            corrupt = True
            continue
        candidate = session.get(Candidate, record.candidate_id)
        if candidate is None:
            corrupt = True
            continue
        if candidate.source_document_id == predecessor_document_id:
            candidate_ids.add(candidate.id)
    for record in reconfirmation_records:
        if not record.identity_valid:
            corrupt = True
            continue
        candidate = session.get(Candidate, record.successor_candidate_id)
        if candidate is None:
            corrupt = True
            continue
        if candidate.source_document_id == predecessor_document_id:
            candidate_ids.add(candidate.id)

    ordered_candidate_ids = tuple(sorted(candidate_ids))
    if corrupt:
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
        reconfirmation_records=reconfirmation_records,
        seen_reconfirmation_ids=frozenset(),
    )
    if admission is None:
        return None, ordered_candidate_ids, reason
    if admission.support_evidence_link_id is not None and {
        scope.evidence.evidence_link_id for scope in scopes
    } != {admission.support_evidence_link_id}:
        return None, ordered_candidate_ids, "reconfirmation_support_mismatch"
    return admission, ordered_candidate_ids, None


def _candidate_lineage(
    session: Session,
    *,
    dependency: Dependency,
    candidate_id: int,
    expected_document_id: int,
    admission_records: tuple[audit.AdmissionRecord, ...],
    reconfirmation_records: tuple[audit.ReconfirmationRecord, ...],
    seen_reconfirmation_ids: frozenset[int],
) -> tuple[_Admission | None, str | None]:
    """Resolve one Candidate to an original Admission through exact receipts."""

    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        return None, "admission_history_corrupt"
    if candidate.source_document_id != expected_document_id:
        return None, "admission_document_mismatch"
    if candidate.project_id != dependency.project_id:
        return None, "admission_project_mismatch"

    direct = tuple(
        record
        for record in admission_records
        if record.candidate_id == candidate_id
    )
    if len(direct) > 1:
        return None, "admission_link_ambiguous"
    if direct:
        record = direct[0]
        if not record.attributable:
            return None, "admission_not_attributable"
        if record.action == audit.ACCEPT_CANDIDATE:
            state_consistent = (
                candidate.state == "accepted" and candidate.merged_into is None
            )
        else:
            state_consistent = (
                candidate.state == "merged"
                and candidate.merged_into == dependency.id
            )
        if not state_consistent:
            return None, "admission_state_inconsistent"
        return (
            _Admission(
                origin=record,
                candidate=candidate,
                admitted_fields=record.fields,
            ),
            None,
        )

    prior = tuple(
        record
        for record in reconfirmation_records
        if record.successor_candidate_id == candidate_id
    )
    if len(prior) != 1:
        return None, (
            "admission_link_unavailable" if not prior else "admission_link_ambiguous"
        )
    reconfirmation = prior[0]
    if not reconfirmation.identity_valid:
        return None, "admission_history_corrupt"
    if reconfirmation.audit_id in seen_reconfirmation_ids:
        return None, "reconfirmation_lineage_cycle"
    if not reconfirmation.attributable:
        return None, "admission_not_attributable"
    if candidate.state != "pending" or candidate.merged_into is not None:
        return None, "admission_state_inconsistent"

    assert reconfirmation.comparison_id is not None
    assert reconfirmation.finding_id is not None
    assert reconfirmation.predecessor_candidate_id is not None
    assert reconfirmation.successor_candidate_id is not None
    assert reconfirmation.new_evidence_link_id is not None
    assert reconfirmation.origin_admission_audit_id is not None
    try:
        readback = read_revision_comparison(
            session, reconfirmation.comparison_id
        )
    except RevisionComparisonError:
        return None, "reconfirmation_history_corrupt"
    comparison = readback.comparison
    if (
        comparison.project_id != dependency.project_id
        or comparison.successor_document_id != expected_document_id
        or candidate.extraction_run_id
        != comparison.successor_extraction_run_id
    ):
        return None, "reconfirmation_identity_mismatch"
    finding = _finding_by_id(readback, reconfirmation.finding_id)
    if (
        finding is None
        or finding.state != "unchanged"
        or finding.predecessor_candidate_ids
        != [reconfirmation.predecessor_candidate_id]
        or finding.successor_candidate_ids
        != [reconfirmation.successor_candidate_id]
    ):
        return None, "reconfirmation_identity_mismatch"

    predecessor_lineage, reason = _candidate_lineage(
        session,
        dependency=dependency,
        candidate_id=reconfirmation.predecessor_candidate_id,
        expected_document_id=comparison.predecessor_document_id,
        admission_records=admission_records,
        reconfirmation_records=reconfirmation_records,
        seen_reconfirmation_ids=(
            seen_reconfirmation_ids | {reconfirmation.audit_id}
        ),
    )
    if predecessor_lineage is None:
        return None, reason
    if (
        predecessor_lineage.origin.audit_id
        != reconfirmation.origin_admission_audit_id
        or predecessor_lineage.latest_reconfirmation_audit_id
        != reconfirmation.predecessor_reconfirmation_audit_id
    ):
        return None, "reconfirmation_lineage_mismatch"

    predecessor_input = _input_by_candidate_id(
        readback.predecessor_inputs,
        reconfirmation.predecessor_candidate_id,
    )
    successor_input = _input_by_candidate_id(
        readback.successor_inputs,
        reconfirmation.successor_candidate_id,
    )
    if predecessor_input is None or successor_input is None:
        return None, "reconfirmation_identity_mismatch"
    if predecessor_lineage.candidate.extraction_run_id != (
        comparison.predecessor_extraction_run_id
    ):
        return None, "reconfirmation_identity_mismatch"
    predecessor_fields = (predecessor_input.get("payload_json") or {}).get(
        "fields"
    )
    if (
        not isinstance(predecessor_fields, dict)
        or predecessor_lineage.admitted_fields != predecessor_fields
    ):
        return None, "reconfirmation_lineage_changed"
    live_input = {
        **candidate_input_snapshot(candidate),
        "extraction_run_id": candidate.extraction_run_id,
    }
    if live_input != successor_input:
        return None, "reconfirmation_candidate_changed"

    try:
        citation = _verified_successor_citation(
            successor_input, expected_document_id
        )
    except ReconfirmationUnavailable:
        return None, "reconfirmation_provenance_unsafe"
    evidence = session.get(EvidenceLink, reconfirmation.new_evidence_link_id)
    if (
        evidence is None
        or evidence.dependency_id != dependency.id
        or evidence.document_id != expected_document_id
        or evidence.verified is not True
        or evidence.page_no != citation["page"]
        or evidence.quote != citation["quote"]
    ):
        return None, "reconfirmation_evidence_mismatch"
    successor_fields = (successor_input.get("payload_json") or {}).get(
        "fields"
    )
    if not isinstance(successor_fields, dict):
        return None, "reconfirmation_history_corrupt"
    return (
        _Admission(
            origin=predecessor_lineage.origin,
            candidate=candidate,
            admitted_fields=successor_fields,
            latest_reconfirmation_audit_id=reconfirmation.audit_id,
            support_evidence_link_id=reconfirmation.new_evidence_link_id,
        ),
        None,
    )


def _verified_successor_citation(
    candidate_input: dict, successor_document_id: int | None
) -> dict:
    payload = candidate_input.get("payload_json")
    citations = (
        tuple(payload.get("citations") or ())
        if isinstance(payload, dict)
        else ()
    )
    if candidate_input.get("citations_verified") is not True or len(citations) != 1:
        raise ReconfirmationUnavailable(
            "Reconfirmation requires exactly one immutable verified citation"
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
        raise ReconfirmationUnavailable(
            "the immutable successor citation is not exact and verified"
        )
    return citation


def _group_scopes(
    scopes: tuple[SupersededOperativeScope, ...]
) -> dict[int, tuple[SupersededOperativeScope, ...]]:
    grouped: dict[int, list[SupersededOperativeScope]] = {}
    for scope in scopes:
        grouped.setdefault(scope.evidence.document_id, []).append(scope)
    return {
        document_id: tuple(values)
        for document_id, values in sorted(grouped.items())
    }


def _terminal_superseded_scopes(
    session: Session, scopes: tuple[SupersededOperativeScope, ...]
) -> tuple[SupersededOperativeScope, ...]:
    """Historical ancestors route to neither lane once a later stale scope exists."""

    filtered: list[SupersededOperativeScope] = []
    for scope in scopes:
        if any(
            other is not scope
            and other.role == scope.role
            and other.field_name == scope.field_name
            and _document_is_ancestor(
                session, scope.evidence.document_id, other.evidence.document_id
            )
            for other in scopes
        ):
            continue
        filtered.append(scope)
    return tuple(filtered)


def _document_is_ancestor(
    session: Session, candidate_ancestor_id: int, descendant_id: int
) -> bool:
    if candidate_ancestor_id == descendant_id:
        return False
    current = session.get(Document, candidate_ancestor_id)
    while current is not None and current.superseded_by is not None:
        if current.superseded_by == descendant_id:
            return True
        current = session.get(Document, current.superseded_by)
    return False


def _active_run(session: Session, document_id: int) -> ExtractionRun | None:
    return session.scalar(
        select(ExtractionRun)
        .join(
            ActiveExtractionRun,
            ActiveExtractionRun.extraction_run_id == ExtractionRun.id,
        )
        .where(ActiveExtractionRun.document_id == document_id)
    )


def _input_by_candidate_id(
    inputs: tuple[dict, ...], candidate_id: int
) -> dict | None:
    matches = tuple(
        item for item in inputs if item.get("candidate_id") == candidate_id
    )
    return matches[0] if len(matches) == 1 else None


def _finding_by_id(
    readback: RevisionComparisonReadback, finding_id: int
) -> RevisionComparisonFinding | None:
    matches = tuple(
        finding for finding in readback.findings if finding.id == finding_id
    )
    return matches[0] if len(matches) == 1 else None


def _scope_signatures(
    scopes: tuple[SupersededOperativeScope, ...]
) -> tuple[tuple[str, str | None, int, int], ...]:
    return tuple(
        sorted(
            [
                (
                    scope.role,
                    scope.field_name,
                    scope.evidence.evidence_link_id,
                    scope.evidence.document_id,
                )
                for scope in scopes
            ],
            key=lambda item: (item[0], item[1] or "", item[2], item[3]),
        )
    )


def _scope_sort_key(scope: SupersededOperativeScope):
    return (
        scope.role,
        scope.field_name or "",
        scope.evidence.evidence_link_id,
    )


def _review_sort_key(review: SupersessionReview):
    return (
        review.dependency_id is None,
        review.dependency_id or 0,
        review.predecessor_document_id or 0,
        review.successor_document_id or 0,
        review.successor_candidate_ids,
        review.status,
    )
