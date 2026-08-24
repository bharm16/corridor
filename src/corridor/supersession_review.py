"""Live Supersession Review and human Reconfirmation.

Revision Comparisons are immutable exact-run receipts.  They are useful
evidence for a reviewer, but they are not the live answer to "what remains to
be reviewed?"  This module derives that answer from declared Supersession and
current Operative Support, then enriches it only when one integrity-checked
Comparison exists for the exact Active Runs.

Reconfirmation is deliberately narrower than Admission. It is the human-only
write seam for moving every role-scoped support designation on one unchanged
row to verified evidence on the terminal successor revision; it cannot admit a
Candidate, rewrite a Dependency conclusion, or infer which run, comparison,
citation, or admission history the reviewer meant. Mixed human and machine
lineage is read back through actor-neutral support-transfer records.
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
    DependencyEvidenceSufficiency,
    Document,
    EvidenceLink,
    ExtractionRun,
    ReconfirmationReceipt,
    RevisionComparisonFinding,
)
from corridor.operative_support import (
    ResolvedSupport,
    SupersededOperativeScope,
    evidence_is_scoped_to_dependency,
    readiness_frontier_before_audit,
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
    publication_owners = [
        (role, field_name)
        for role, field_name, _ in normalized
        if role == "publication"
    ]
    if len(publication_owners) != len(set(publication_owners)):
        raise ValueError(
            "scope fingerprint contains duplicate publication scopes"
        )
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
    latest_support_transfer_audit_id: int | None = None


def build_reviewer_worklist(
    session: Session, project_id: int
) -> ReviewerWorklist:
    """Derive current work without storing state or selecting by recency."""

    dependencies = tuple(
        session.scalars(
            select(Dependency)
            .where(
                Dependency.project_id == project_id,
                # A worklist must not offer work on a record that was
                # thrown out (ADR-0032).
                Dependency.dismissed_at.is_(None),
            )
            .order_by(Dependency.id)
        ).all()
    )
    dependency_ids = tuple(dependency.id for dependency in dependencies)
    support_by_dependency = resolve_operative_support(session, dependency_ids)
    admissions_by_dependency = audit.admission_records_for_dependencies(
        session, dependency_ids
    )
    support_transfers_by_dependency = (
        audit.support_transfer_records_for_dependencies(
            session, dependency_ids
        )
    )
    actionable = {
        candidate.id: candidate
        for candidate in session.scalars(
            actionable_candidate_query(project_id).order_by(Candidate.id)
        ).all()
    }
    already_reconfirmed = _reconfirmed_candidate_ids(
        session, support_transfers_by_dependency, actionable
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
                readiness_history_trusted=(
                    resolved.readiness_history_trusted
                ),
                actionable=actionable,
                admission_records=admissions_by_dependency.get(
                    dependency.id, ()
                ),
                support_transfer_records=support_transfers_by_dependency.get(
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

    owner_by_candidate = {
        candidate_id: min(
            (
                index
                for index, review in enumerate(reviews)
                if candidate_id in review.successor_candidate_ids
            ),
            key=lambda index: (_review_sort_key(reviews[index]), index),
        )
        for candidate_id in collisions
    }

    resolved_ordinary: list[SupersessionReview] = []
    resolved_reconfirmation: list[SupersessionReview] = []
    for index, review in enumerate(reviews):
        if collisions.intersection(review.successor_candidate_ids):
            resolved_ordinary.append(
                replace(
                    review,
                    route="ordinary",
                    successor_candidate_ids=tuple(
                        candidate_id
                        for candidate_id in review.successor_candidate_ids
                        if candidate_id not in collisions
                        or owner_by_candidate[candidate_id] == index
                    ),
                    reason="successor_candidate_link_ambiguous",
                )
            )
        elif review.reconfirmation_available:
            resolved_reconfirmation.append(review)
        else:
            resolved_ordinary.append(review)
    return resolved_ordinary, resolved_reconfirmation


def _reconfirmed_candidate_ids(
    session: Session,
    records_by_dependency: dict[
        int, tuple[audit.SupportTransferRecord, ...]
    ],
    actionable: dict[int, Candidate],
) -> frozenset[int]:
    """Exclude Candidates bound to a Reconfirmation by independent facts."""

    candidate_ids: set[int] = set()
    for records in records_by_dependency.values():
        for record in records:
            candidate_ids.update(
                _recover_reconfirmed_candidate_ids(
                    session, record, actionable
                )
            )
    return frozenset(candidate_ids)


def _recover_reconfirmed_candidate_ids(
    session: Session,
    record: audit.SupportTransferRecord,
    actionable: dict[int, Candidate],
) -> set[int]:
    """Bind a past act through durable facts, never pointer majority votes."""

    if record.durable_successor_candidate_id is not None:
        durable_candidate = session.get(
            Candidate, record.durable_successor_candidate_id
        )
        if durable_candidate is not None:
            return {durable_candidate.id}

    readbacks = _reconfirmation_readbacks(session, record)
    source_bound_ids = _candidate_ids_bound_by_reconfirmation_sources(
        session,
        record=record,
        readbacks=readbacks,
    )

    evidence_target_ids: list[int] = []
    if record.new_evidence_link_id is not None:
        evidence_target_ids.append(record.new_evidence_link_id)
    evidence_target_ids.extend(
        sorted(
            {
                move.to_evidence_link_id
                for move in record.moved_scopes
            }
        )
    )
    target_bound_ids: set[int] = set()
    for evidence_link_id in dict.fromkeys(evidence_target_ids):
        target_bound_ids.update(
            _candidate_ids_for_reconfirmation_evidence(
                session,
                evidence_link_id=evidence_link_id,
                dependency_id=record.dependency_id,
                readbacks=readbacks,
                actionable=actionable,
            )
        )

    # Exact source Evidence joined through an immutable 1:1 finding is the
    # strongest recoverable binding. A matching target confirms a singleton;
    # contradictory grounded bindings quarantine their union. We never let a
    # raw JSON Candidate or finding pointer win a vote.
    if source_bound_ids:
        if len(source_bound_ids) == 1:
            if not target_bound_ids or source_bound_ids == target_bound_ids:
                return source_bound_ids
        return source_bound_ids | target_bound_ids
    if target_bound_ids:
        return target_bound_ids

    # If every durable hint is gone, the record cannot safely distinguish one
    # current row in this project. A raw comparison pointer is part of the
    # same mutable JSON claim family, so it cannot safely localize the blast
    # radius. Broad quarantine is preferable to silently making the
    # already-used Candidate writable again.
    return set(actionable)


def _candidate_ids_bound_by_reconfirmation_sources(
    session: Session,
    *,
    record: audit.SupportTransferRecord,
    readbacks: tuple[RevisionComparisonReadback, ...],
) -> set[int]:
    """Join receipt sources to one-to-one immutable comparison findings."""

    if not record.operative_scopes:
        return set()
    source_evidence: list[EvidenceLink] = []
    for scope in record.operative_scopes:
        evidence = session.get(EvidenceLink, scope.evidence_link_id)
        if (
            evidence is None
            or not evidence_is_scoped_to_dependency(
                session, evidence, record.dependency_id
            )
            or evidence.verified is not True
        ):
            return set()
        source_evidence.append(evidence)

    candidate_ids: set[int] = set()
    for readback in readbacks:
        predecessor_ids = {
            candidate_id
            for candidate_input in readback.predecessor_inputs
            if (
                (candidate_id := candidate_input.get("candidate_id"))
                is not None
                and not isinstance(candidate_id, bool)
                and isinstance(candidate_id, int)
                and candidate_id > 0
                and all(
                    evidence.document_id
                    == readback.comparison.predecessor_document_id
                    and _input_matches_evidence(candidate_input, evidence)
                    for evidence in source_evidence
                )
            )
        }
        for finding in readback.findings:
            candidate_id = _unchanged_successor_candidate_id(finding)
            if (
                candidate_id is not None
                and finding.predecessor_candidate_ids
                and set(finding.predecessor_candidate_ids).issubset(
                    predecessor_ids
                )
                and session.get(Candidate, candidate_id) is not None
            ):
                candidate_ids.add(candidate_id)
    return candidate_ids


def _candidate_ids_for_reconfirmation_evidence(
    session: Session,
    *,
    evidence_link_id: int,
    dependency_id: int,
    readbacks: tuple[RevisionComparisonReadback, ...],
    actionable: dict[int, Candidate],
) -> set[int]:
    evidence = session.get(EvidenceLink, evidence_link_id)
    if (
        evidence is None
        or not evidence_is_scoped_to_dependency(session, evidence, dependency_id)
        or evidence.verified is not True
    ):
        return set()

    comparison_matches: set[int] = set()
    for readback in readbacks:
        if readback.comparison.successor_document_id != evidence.document_id:
            continue
        for finding in readback.findings:
            candidate_id = _unchanged_successor_candidate_id(finding)
            if candidate_id is None:
                continue
            candidate_input = _input_by_candidate_id(
                readback.successor_inputs, candidate_id
            )
            if candidate_input is not None and _input_matches_evidence(
                candidate_input, evidence
            ) and session.get(Candidate, candidate_id) is not None:
                comparison_matches.add(candidate_id)
    if comparison_matches:
        return comparison_matches
    return {
        candidate_id
        for candidate_id, candidate in actionable.items()
        if _input_matches_evidence(
            candidate_input_snapshot(candidate), evidence
        )
    }


def _reconfirmation_readbacks(
    session: Session, record: audit.SupportTransferRecord
) -> tuple[RevisionComparisonReadback, ...]:
    comparison_ids: set[int] = set()
    if record.comparison_id is not None:
        comparison_ids.add(record.comparison_id)
    if record.finding_id is not None:
        finding = session.get(RevisionComparisonFinding, record.finding_id)
        if finding is not None:
            comparison_ids.add(finding.revision_comparison_run_id)

    readbacks: list[RevisionComparisonReadback] = []
    for comparison_id in sorted(comparison_ids):
        try:
            readbacks.append(read_revision_comparison(session, comparison_id))
        except RevisionComparisonError:
            continue
    return tuple(readbacks)


def _unchanged_successor_candidate_id(
    finding: RevisionComparisonFinding,
) -> int | None:
    if (
        finding.state != "unchanged"
        or len(finding.predecessor_candidate_ids) != 1
        or len(finding.successor_candidate_ids) != 1
    ):
        return None
    [candidate_id] = finding.successor_candidate_ids
    return candidate_id


def _input_matches_evidence(
    candidate_input: dict, evidence: EvidenceLink
) -> bool:
    return (
        candidate_input.get("source_document_id") == evidence.document_id
        and _has_one_verified_input_citation(
            candidate_input,
            document_id=evidence.document_id,
            page_no=evidence.page_no,
            quote=evidence.quote,
        )
    )


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
        from corridor.support_transfer import (
            SupportTransferProofRefusal,
            apply_proven_support_transfer,
            prove_support_transfer,
        )
        try:
            proof = prove_support_transfer(
                session,
                project_id=project_id,
                review=review,
            )
            transfer = apply_proven_support_transfer(
                session, proof,
                designated_by=principal.subject,
            )
        except (SupportTransferProofRefusal, ValueError) as exc:
            raise ReconfirmationUnavailable(str(exc)) from exc
        new_evidence = transfer.evidence
        before_receipt = transfer.before_json
        after_receipt = {
            "comparison_id": proof.comparison.id,
            "finding_id": proof.finding.id,
            "predecessor_candidate_id": review.predecessor_candidate_id,
            "successor_candidate_id": successor_candidate_id,
            "new_evidence_link_id": new_evidence.id,
            "scope_fingerprint": [list(item) for item in proof.scope_fingerprint],
            "origin_admission_audit_id": proof.admission.origin.audit_id,
            "predecessor_support_transfer_audit_id": (
                proof.admission.latest_support_transfer_audit_id
            ),
            "predecessor_reconfirmation_audit_id": (
                proof.admission.latest_support_transfer_audit_id
            ),
            "moved_scopes": list(transfer.moved_scopes),
        }
        audit_entry = audit.record(
            session,
            principal=principal,
            action=audit.RECONFIRM_OPERATIVE_SUPPORT,
            entity_type=audit.DEPENDENCY,
            entity_id=dependency_id,
            before=before_receipt,
            after=after_receipt,
        )
        session.add(
            ReconfirmationReceipt(
                audit_log_id=audit_entry.id,
                dependency_id=dependency_id,
                successor_candidate_id=successor_candidate_id,
                before_json=before_receipt,
                after_json=after_receipt,
            )
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
    readiness_history_trusted: bool,
    actionable: dict[int, Candidate],
    admission_records: tuple[audit.AdmissionRecord, ...],
    support_transfer_records: tuple[audit.SupportTransferRecord, ...],
) -> SupersessionReview:
    predecessor = session.get(Document, predecessor_document_id)
    admission, predecessor_candidate_ids, admission_reason = _admission_for_scope(
        session,
        dependency,
        predecessor_document_id,
        admission_records,
        support_transfer_records,
    )
    base = dict(
        dependency_id=dependency.id,
        predecessor_document_id=predecessor_document_id,
        predecessor_candidate_ids=predecessor_candidate_ids,
        superseded_scopes=scopes,
    )
    if (
        admission_reason is None
        and not readiness_history_trusted
    ):
        admission_reason = "readiness_history_untrusted"
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

    known_predecessors = set(predecessor_candidate_ids)
    evidence_predecessors = _predecessor_ids_matching_scopes(
        scopes, readback.predecessor_inputs
    )
    admission_scope_conflict = (
        admission is not None
        and bool(evidence_predecessors)
        and admission.candidate.id not in evidence_predecessors
    )
    if admission is None or admission_scope_conflict:
        relevant_predecessors = (
            evidence_predecessors or known_predecessors
        )
        finding_successors = tuple(
            sorted(
                {
                    candidate_id
                    for finding in readback.findings
                    if (
                        relevant_predecessors
                        and relevant_predecessors.intersection(
                            finding.predecessor_candidate_ids
                        )
                    )
                    for candidate_id in finding.successor_candidate_ids
                    if candidate_id in actionable
                }
            )
        )
        return SupersessionReview(
            **base,
            successor_candidate_ids=finding_successors,
            status="comparison_ready",
            comparison_id=comparison.id,
            reason=(
                "admission_scope_identity_mismatch"
                if admission_scope_conflict
                else admission_reason or "admission_link_unavailable"
            ),
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
        readiness_history_trusted=readiness_history_trusted,
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
    readiness_history_trusted: bool,
    readback: RevisionComparisonReadback,
    finding: RevisionComparisonFinding,
    actionable: dict[int, Candidate],
) -> str | None:
    if not readiness_history_trusted:
        return "readiness_history_untrusted"
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


def _predecessor_ids_matching_scopes(
    scopes: tuple[SupersededOperativeScope, ...],
    predecessor_inputs: tuple[dict, ...],
) -> set[int]:
    """Recover plausible predecessor rows from exact operative citations."""

    matches: set[int] = set()
    for candidate_input in predecessor_inputs:
        candidate_id = candidate_input.get("candidate_id")
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id <= 0
        ):
            continue
        if scopes and all(
            _scope_has_one_verified_input_citation(scope, candidate_input)
            for scope in scopes
        ):
            matches.add(candidate_id)
    return matches


def _scope_has_one_verified_input_citation(
    scope: SupersededOperativeScope, candidate_input: dict
) -> bool:
    """Bind one moved support scope to one immutable Candidate citation."""

    if scope.evidence.verified is not True:
        return False
    return _has_one_verified_input_citation(
        candidate_input,
        document_id=scope.evidence.document_id,
        page_no=scope.evidence.page_no,
        quote=scope.evidence.quote,
    )


def _has_one_verified_input_citation(
    candidate_input: dict,
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


def _reconfirmation_sources_match_receipt(
    session: Session,
    *,
    dependency_id: int,
    predecessor_document_id: int,
    successor_document_id: int,
    predecessor_input: dict,
    record: audit.SupportTransferRecord,
) -> bool:
    """Validate every historical transfer source against durable facts."""

    current_readiness_ids = (
        audit.current_readiness_evidence_ids_from_audit(
            session, dependency_id
        )
    )
    # Audit history tracks the live sufficiency designation, not whether its
    # document remains terminal. Every Evidence shape now keeps that role in
    # one explicit per-Dependency sufficiency row.
    stored_current_readiness_ids = frozenset(
        session.scalars(
            select(DependencyEvidenceSufficiency.evidence_link_id).where(
                DependencyEvidenceSufficiency.dependency_id == dependency_id
            )
        ).all()
    )
    if (
        current_readiness_ids is None
        or current_readiness_ids != stored_current_readiness_ids
    ):
        return False

    readiness_frontier = readiness_frontier_before_audit(
        session,
        dependency_id,
        record.audit_id,
        known_terminal_document_id=successor_document_id,
    )
    if readiness_frontier is None:
        return False
    expected_readiness_sources = {
        support.evidence_link_id for support in readiness_frontier
    }
    receipt_readiness_sources = {
        scope.evidence_link_id
        for scope in record.operative_scopes
        if scope.role == "readiness"
    }
    if receipt_readiness_sources != expected_readiness_sources:
        return False

    for scope in record.operative_scopes:
        evidence = session.get(EvidenceLink, scope.evidence_link_id)
        if (
            evidence is None
            or not evidence_is_scoped_to_dependency(session, evidence, dependency_id)
            or evidence.document_id != predecessor_document_id
            or evidence.verified is not True
            or not _has_one_verified_input_citation(
                predecessor_input,
                document_id=evidence.document_id,
                page_no=evidence.page_no,
                quote=evidence.quote,
            )
        ):
            return False
    return True


def _admission_for_scope(
    session: Session,
    dependency: Dependency,
    predecessor_document_id: int,
    admission_records: tuple[audit.AdmissionRecord, ...],
    support_transfer_records: tuple[audit.SupportTransferRecord, ...],
) -> tuple[_Admission | None, tuple[int, ...], str | None]:
    candidate_ids: set[int] = set()
    admission_corrupt = False
    reconfirmation_corrupt = False
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
            reconfirmation_corrupt = True
        if record.successor_candidate_id is None:
            continue
        candidate = session.get(Candidate, record.successor_candidate_id)
        if candidate is None:
            reconfirmation_corrupt = True
            continue
        if candidate.source_document_id == predecessor_document_id:
            candidate_ids.add(candidate.id)

    ordered_candidate_ids = tuple(sorted(candidate_ids))
    if reconfirmation_corrupt:
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
        seen_reconfirmation_ids=frozenset(),
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
        for record in support_transfer_records
        if record.successor_candidate_id == candidate_id
    )
    if len(prior) != 1:
        return None, (
            "admission_link_unavailable" if not prior else "admission_link_ambiguous"
        )
    reconfirmation = prior[0]
    if not reconfirmation.identity_valid:
        return None, "reconfirmation_history_corrupt"
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
    if (
        reconfirmation.origin_admission_audit_id
        >= reconfirmation.audit_id
        or (
            reconfirmation.predecessor_reconfirmation_audit_id is not None
            and reconfirmation.predecessor_reconfirmation_audit_id
            >= reconfirmation.audit_id
        )
    ):
        return None, "reconfirmation_lineage_chronology_invalid"
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
        support_transfer_records=support_transfer_records,
        seen_reconfirmation_ids=(
            seen_reconfirmation_ids | {reconfirmation.audit_id}
        ),
    )
    if predecessor_lineage is None:
        return None, reason
    if (
        predecessor_lineage.origin.audit_id
        != reconfirmation.origin_admission_audit_id
        or predecessor_lineage.latest_support_transfer_audit_id
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
    if not _reconfirmation_sources_match_receipt(
        session,
        dependency_id=dependency.id,
        predecessor_document_id=comparison.predecessor_document_id,
        successor_document_id=comparison.successor_document_id,
        predecessor_input=predecessor_input,
        record=reconfirmation,
    ):
        return None, "reconfirmation_history_corrupt"
    predecessor_fields = (predecessor_input.get("payload_json") or {}).get(
        "fields"
    )
    if not isinstance(predecessor_fields, dict):
        return None, "reconfirmation_history_corrupt"
    predecessor_was_human_edited = (
        predecessor_lineage.admitted_fields != predecessor_fields
    )
    if (
        predecessor_was_human_edited
        and reconfirmation.action != audit.AUTOMATIC_CARRY_FORWARD
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
    if (
        reconfirmation.action == audit.AUTOMATIC_CARRY_FORWARD
        and successor_fields != predecessor_lineage.admitted_fields
    ):
        return None, "reconfirmation_lineage_changed"
    return (
        _Admission(
            origin=predecessor_lineage.origin,
            candidate=candidate,
            admitted_fields=successor_fields,
            latest_support_transfer_audit_id=reconfirmation.audit_id,
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


def _review_sort_key(review: SupersessionReview):
    return (
        review.dependency_id is None,
        review.dependency_id or 0,
        review.predecessor_document_id or 0,
        review.successor_document_id or 0,
        review.successor_candidate_ids,
        review.status,
    )
