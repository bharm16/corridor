"""Policy-authorized machine transfer of exact unchanged operative support.

Automatic Carry-Forward is deliberately not Admission and not human
Reconfirmation under a borrowed identity.  A human authorizes one immutable,
project-bound policy.  The server may then transfer only the complete support
scope already grounded in an attributable human Admission, and records the
act as a distinct machine receipt. Every uncertain row remains unresolved with
a stable refusal reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from corridor import audit
from corridor import extraction_runs as extraction_runs_module
from corridor import models as models_module
from corridor import operative_support as operative_support_module
from corridor import principals as principals_module
from corridor import project_lock as project_lock_module
from corridor import revision_comparison as revision_comparison_module
from corridor import supersession as supersession_module
from corridor import supersession_review as supersession_review_module
from corridor.extraction_runs import candidate_input_snapshot
from corridor.models import (
    ActiveAutomaticCarryForwardPolicy,
    AuditLog,
    AutomaticCarryForwardOutcome,
    AutomaticCarryForwardPolicyApproval,
    AutomaticCarryForwardReceipt,
    AutomaticCarryForwardRun,
    Candidate,
    Dependency,
    EvidenceLink,
    Project,
)
from corridor.operative_support import (
    UnsafeSupportTransfer,
    _transfer_operative_scopes_under_lock,
)
from corridor.principals import (
    HumanPrincipal,
    InvalidHumanPrincipal,
    require_human_principal,
)
from corridor.project_lock import lock_project
from corridor.revision_comparison import (
    DEFAULT_MATCHER_CONFIG,
    DEFAULT_MATCHER_VERSION,
    RevisionComparisonError,
    read_revision_comparison,
)
from corridor.supersession_review import (
    SupersessionReview,
    _admission_for_scope,
    _finding_by_id,
    _input_by_candidate_id,
    _verified_successor_citation,
    build_reviewer_worklist,
)


POLICY_VERSION = "automatic-carry-forward-v1"
MACHINE_ACTOR = audit.AUTOMATIC_CARRY_FORWARD_ACTOR
ABSTENTION_REASON_VERSION = "automatic-carry-forward-abstentions-v1"
OUTCOME_CARRIED = "carried"
OUTCOME_ABSTAINED = "abstained"
ABSTENTION_REASONS = frozenset(
    {
        "comparison_policy_unapproved",
        "review_stale",
        "dependency_unavailable",
        "comparison_not_one_to_one",
        "comparison_integrity_failure",
        "predecessor_finding_unavailable",
        "comparison_changed",
        "comparison_dropped",
        "comparison_ambiguous",
        "comparison_unmatched",
        "comparison_input_unavailable",
        "successor_fields_not_exact",
        "successor_not_actionable",
        "successor_candidate_changed",
        "successor_provenance_unsafe",
        "unsupported_operative_support_role",
        "readiness_source_changed",
        "readiness_history_untrusted",
        "supersession_registry_unavailable",
        "multi_hop_supersession",
        "successor_active_run_invalid",
        "predecessor_active_run_unavailable",
        "multiple_exact_comparisons",
        "admission_scope_identity_mismatch",
        "partial_scope_transfer",
        "support_scope_changed",
        "predecessor_support_provenance_unsafe",
        "admission_run_mismatch",
        "admission_fields_changed",
        "admission_history_corrupt",
        "admission_link_unavailable",
        "admission_link_ambiguous",
        "admission_document_mismatch",
        "admission_project_mismatch",
        "admission_not_attributable",
        "admission_state_inconsistent",
        "reconfirmation_history_corrupt",
        "reconfirmation_lineage_cycle",
        "reconfirmation_lineage_chronology_invalid",
        "reconfirmation_identity_mismatch",
        "reconfirmation_lineage_mismatch",
        "reconfirmation_lineage_changed",
        "reconfirmation_candidate_changed",
        "reconfirmation_provenance_unsafe",
        "reconfirmation_evidence_mismatch",
        "successor_candidate_already_reconfirmed",
        "successor_candidate_link_ambiguous",
        "awaiting_extraction",
        "extraction_failed",
        "awaiting_active_run",
        "awaiting_comparison",
        "comparison_selection_ambiguous",
        "comparison_ready",
        "blocked",
        "unclassified_unsafe",
    }
)


@dataclass(frozen=True)
class AutomaticCarryForwardAbstention:
    """One row the approved policy refused, with a stable reason code."""

    dependency_id: int
    predecessor_candidate_id: int | None
    successor_candidate_id: int | None
    comparison_id: int | None
    finding_id: int | None
    reason: str
    reason_version: str = ABSTENTION_REASON_VERSION
    detail: str | None = None


@dataclass(frozen=True)
class AutomaticCarryForwardResult:
    """The exact receipts written and the rows deliberately left unresolved."""

    carried: tuple[AutomaticCarryForwardReceipt, ...]
    abstentions: tuple[AutomaticCarryForwardAbstention, ...]


@dataclass(frozen=True)
class AutomaticCarryForwardStatus:
    """Current read-only policy and routing status for one project."""

    project_id: int
    enabled: bool
    policy_current: bool
    policy_approval_id: int | None
    policy_version: str | None
    policy_sha256: str | None
    approved_by: str | None
    carried_count: int
    eligible_count: int
    abstention_reason_version: str
    abstention_counts: dict[str, int]


@dataclass(frozen=True, kw_only=True)
class AutomaticCarryForwardRuntime:
    """Server-owned immutable policy inputs for one top-level call."""

    safety_sources: tuple[tuple[str, bytes], ...]
    matcher_version: str = DEFAULT_MATCHER_VERSION
    matcher_config: dict | None = None

    def __post_init__(self) -> None:
        if self.matcher_config is None:
            object.__setattr__(
                self,
                "matcher_config",
                deepcopy(DEFAULT_MATCHER_CONFIG),
            )
        else:
            object.__setattr__(
                self,
                "matcher_config",
                deepcopy(self.matcher_config),
            )

    def canonical_policy_json(self) -> dict:
        matcher_config = deepcopy(self.matcher_config)
        return {
            "policy_version": POLICY_VERSION,
            "eligible_comparison_state": "unchanged",
            "field_equality": "exact-admitted-fields-v1",
            "provenance": "exactly-one-verified-citation-v1",
            "support_scope": "all-operative-scopes-v1",
            "rules_digest_method": "sha256-safety-source-files-v1",
            "rules_digest": self.rules_digest(),
            "matcher_version": self.matcher_version,
            "matcher_config": matcher_config,
            "matcher_config_sha256": _json_sha256(matcher_config),
        }

    def rules_digest(self) -> str:
        digest = hashlib.sha256()
        for module_name, source_bytes in self.safety_sources:
            digest.update(module_name.encode())
            digest.update(b"\0")
            digest.update(source_bytes)
            digest.update(b"\0")
        return digest.hexdigest()

    @classmethod
    def deployed(
        cls,
        *,
        source_overrides: Mapping[str, bytes] | None = None,
        matcher_version: str = DEFAULT_MATCHER_VERSION,
        matcher_config: dict | None = None,
    ) -> "AutomaticCarryForwardRuntime":
        overrides = dict(source_overrides or {})
        return cls(
            safety_sources=tuple(
                (module_name, overrides.get(module_name, path.read_bytes()))
                for module_name, path in _safety_source_paths()
            ),
            matcher_version=matcher_version,
            matcher_config=(
                deepcopy(matcher_config)
                if matcher_config is not None
                else deepcopy(DEFAULT_MATCHER_CONFIG)
            ),
        )


def _effective_runtime(
    runtime: AutomaticCarryForwardRuntime | None,
) -> AutomaticCarryForwardRuntime:
    return runtime if runtime is not None else AutomaticCarryForwardRuntime.deployed()


def authorize_automatic_carry_forward(
    session: Session,
    project_id: int,
    principal: HumanPrincipal,
    *,
    _runtime: AutomaticCarryForwardRuntime | None = None,
) -> AutomaticCarryForwardPolicyApproval:
    """Append and activate the current server-owned policy for one project."""

    principal = require_human_principal(principal)
    with session.begin_nested():
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"project {project_id} does not exist")
        session.flush()
        lock_project(session, project_id)
        session.expire_all()
        if session.get(Project, project_id) is None:
            raise ValueError(f"project {project_id} does not exist")

        runtime = _effective_runtime(_runtime)
        policy_json = _canonical_policy_json(runtime=runtime)
        approval = AutomaticCarryForwardPolicyApproval(
            project_id=project_id,
            policy_version=POLICY_VERSION,
            approved_by=principal.subject,
            policy_json=policy_json,
            policy_sha256=_json_sha256(policy_json),
        )
        session.add(approval)
        session.flush([approval])

        active = session.get(
            ActiveAutomaticCarryForwardPolicy,
            project_id,
            populate_existing=True,
        )
        if active is None:
            session.add(
                ActiveAutomaticCarryForwardPolicy(
                    project_id=project_id,
                    policy_approval_id=approval.id,
                )
            )
        else:
            session.execute(
                update(ActiveAutomaticCarryForwardPolicy)
                .where(
                    ActiveAutomaticCarryForwardPolicy.project_id
                    == project_id
                )
                .values(
                    policy_approval_id=approval.id,
                    activated_at=func.now(),
                )
            )
        audit.record(
            session,
            principal=principal,
            action=audit.AUTHORIZE_AUTOMATIC_CARRY_FORWARD,
            entity_type=audit.PROJECT,
            entity_id=project_id,
            after={
                "policy_approval_id": approval.id,
                "policy_version": approval.policy_version,
                "policy_sha256": approval.policy_sha256,
            },
        )
        session.flush()
        return approval


def disable_automatic_carry_forward(
    session: Session,
    project_id: int,
    principal: HumanPrincipal,
) -> AutomaticCarryForwardPolicyApproval | None:
    """Remove only the active pointer; approvals and receipts remain history."""

    principal = require_human_principal(principal)
    with session.begin_nested():
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"project {project_id} does not exist")
        session.flush()
        lock_project(session, project_id)
        session.expire_all()
        active = session.get(
            ActiveAutomaticCarryForwardPolicy,
            project_id,
            populate_existing=True,
        )
        if active is None:
            return None
        approval = session.get(
            AutomaticCarryForwardPolicyApproval,
            active.policy_approval_id,
        )
        session.execute(
            delete(ActiveAutomaticCarryForwardPolicy).where(
                ActiveAutomaticCarryForwardPolicy.project_id == project_id
            )
        )
        audit.record(
            session,
            principal=principal,
            action=audit.DISABLE_AUTOMATIC_CARRY_FORWARD,
            entity_type=audit.PROJECT,
            entity_id=project_id,
            before={
                "policy_approval_id": active.policy_approval_id,
            },
            after={"policy_approval_id": None},
        )
        return approval


def active_carry_forward_policy(
    session: Session,
    project_id: int,
) -> AutomaticCarryForwardPolicyApproval | None:
    """Return the explicitly active approval, never an inferred latest row."""

    return session.scalar(
        select(AutomaticCarryForwardPolicyApproval)
        .join(
            ActiveAutomaticCarryForwardPolicy,
            ActiveAutomaticCarryForwardPolicy.policy_approval_id
            == AutomaticCarryForwardPolicyApproval.id,
        )
        .where(
            ActiveAutomaticCarryForwardPolicy.project_id == project_id,
            AutomaticCarryForwardPolicyApproval.project_id == project_id,
        )
    )


def automatic_carry_forward_status(
    session: Session,
    project_id: int,
    *,
    worklist=None,
    _runtime: AutomaticCarryForwardRuntime | None = None,
) -> AutomaticCarryForwardStatus:
    """Derive reviewer-visible eligibility and Abstentions without writing."""

    if session.get(Project, project_id) is None:
        raise ValueError(f"project {project_id} does not exist")
    runtime = _effective_runtime(_runtime)
    approval = active_carry_forward_policy(session, project_id)
    carried_count, reason_counts = _durable_outcome_counts(session, project_id)
    durable_abstentions = _durable_abstention_identities(session, project_id)
    if worklist is None:
        worklist = build_reviewer_worklist(session, project_id)

    def count_if_new(review: SupersessionReview, reason: str) -> None:
        abstention = _abstention(review, reason)
        if _abstention_identity(abstention) in durable_abstentions:
            return
        reason_counts[abstention.reason] = (
            reason_counts.get(abstention.reason, 0) + 1
        )

    if approval is None:
        return AutomaticCarryForwardStatus(
            project_id=project_id,
            enabled=False,
            policy_current=False,
            policy_approval_id=None,
            policy_version=None,
            policy_sha256=None,
            approved_by=None,
            carried_count=carried_count,
            eligible_count=0,
            abstention_reason_version=ABSTENTION_REASON_VERSION,
            abstention_counts=dict(sorted(reason_counts.items())),
    )

    policy_current = _approval_is_current_policy(
        session,
        approval,
        runtime=runtime,
    )
    if not policy_current:
        for review in worklist.reviews:
            if review.dependency_id is None:
                continue
            count_if_new(review, "comparison_policy_unapproved")
        eligible_count = 0
    else:
        edited_conclusion_reviews = tuple(
            review
            for review in worklist.ordinary
            if review.dependency_id is not None
            and review.reason == "admission_fields_changed"
        )
        for review in worklist.ordinary:
            if review.dependency_id is None or review in edited_conclusion_reviews:
                continue
            count_if_new(review, _ordinary_reason(review))
        eligible_count = 0
        for review in worklist.reconfirmation + edited_conclusion_reviews:
            _receipt, reason = _carry_one(
                session,
                project_id=project_id,
                approval=approval,
                review=review,
                assess_only=True,
            )
            if reason is None:
                eligible_count += 1
            else:
                count_if_new(review, reason)

    return AutomaticCarryForwardStatus(
        project_id=project_id,
        enabled=True,
        policy_current=policy_current,
        policy_approval_id=approval.id,
        policy_version=approval.policy_version,
        policy_sha256=approval.policy_sha256,
        approved_by=approval.approved_by,
        carried_count=carried_count,
        eligible_count=eligible_count,
        abstention_reason_version=ABSTENTION_REASON_VERSION,
        abstention_counts=dict(sorted(reason_counts.items())),
    )


def run_automatic_carry_forward(
    session: Session,
    project_id: int,
    *,
    _runtime: AutomaticCarryForwardRuntime | None = None,
) -> AutomaticCarryForwardResult:
    """Run one serialized, idempotent project batch with per-row savepoints."""

    runtime = _effective_runtime(_runtime)
    if active_carry_forward_policy(session, project_id) is None:
        return AutomaticCarryForwardResult(carried=(), abstentions=())

    carried: list[AutomaticCarryForwardReceipt] = []
    abstentions: list[AutomaticCarryForwardAbstention] = []
    with session.begin_nested():
        session.flush()
        lock_project(session, project_id)
        session.expire_all()
        approval = active_carry_forward_policy(session, project_id)
        if approval is None:
            return AutomaticCarryForwardResult(carried=(), abstentions=())
        if not _approval_is_current_policy(
            session,
            approval,
            runtime=runtime,
        ):
            worklist = build_reviewer_worklist(session, project_id)
            result = AutomaticCarryForwardResult(
                carried=(),
                abstentions=tuple(
                    _abstention(review, "comparison_policy_unapproved")
                    for review in worklist.reviews
                    if review.dependency_id is not None
                ),
            )
            _record_run_outcomes(
                session,
                project_id=project_id,
                approval=approval,
                result=result,
            )
            return result

        worklist = build_reviewer_worklist(session, project_id)
        edited_conclusion_reviews = tuple(
            review
            for review in worklist.ordinary
            if review.reason == "admission_fields_changed"
        )
        abstentions.extend(
            _abstention(review, _ordinary_reason(review))
            for review in worklist.ordinary
            if (
                review.dependency_id is not None
                and review not in edited_conclusion_reviews
            )
        )

        carry_candidates = (
            worklist.reconfirmation + edited_conclusion_reviews
        )
        for seed in sorted(carry_candidates, key=_review_key):
            with session.begin_nested():
                current = _current_review(session, project_id, seed)
                if current is None:
                    abstentions.append(_abstention(seed, "review_stale"))
                    continue
                receipt, reason = _carry_one(
                    session,
                    project_id=project_id,
                    approval=approval,
                    review=current,
                )
                if reason is not None:
                    abstentions.append(_abstention(current, reason))
                else:
                    assert receipt is not None
                    carried.append(receipt)

        result = AutomaticCarryForwardResult(
            carried=tuple(carried),
            abstentions=tuple(abstentions),
        )
        _record_run_outcomes(
            session,
            project_id=project_id,
            approval=approval,
            result=result,
        )
        return result

    return AutomaticCarryForwardResult(
        carried=tuple(carried),
        abstentions=tuple(abstentions),
    )


def _carry_one(
    session: Session,
    *,
    project_id: int,
    approval: AutomaticCarryForwardPolicyApproval,
    review: SupersessionReview,
    assess_only: bool = False,
) -> tuple[AutomaticCarryForwardReceipt | None, str | None]:
    dependency = session.get(Dependency, review.dependency_id)
    if dependency is None or dependency.project_id != project_id:
        return None, "dependency_unavailable"
    if (
        review.predecessor_document_id is None
        or review.successor_document_id is None
        or review.comparison_id is None
        or review.finding_id is None
        or review.predecessor_candidate_id is None
        or review.successor_candidate_id is None
    ):
        return None, "comparison_not_one_to_one"

    try:
        readback = read_revision_comparison(session, review.comparison_id)
    except RevisionComparisonError:
        return None, "comparison_integrity_failure"
    comparison = readback.comparison
    if (
        comparison.matcher_version
        != approval.policy_json.get("matcher_version")
        or _json_sha256(comparison.matcher_config)
        != approval.policy_json.get("matcher_config_sha256")
    ):
        return None, "comparison_policy_unapproved"
    finding = _finding_by_id(readback, review.finding_id)
    if finding is None:
        return None, "predecessor_finding_unavailable"
    if finding.state != "unchanged":
        return None, f"comparison_{finding.state}"

    admissions = audit.admission_records_for_dependencies(
        session, (dependency.id,)
    ).get(dependency.id, ())
    support_transfers = audit.support_transfer_records_for_dependencies(
        session, (dependency.id,)
    ).get(dependency.id, ())
    admission, _, admission_reason = _admission_for_scope(
        session,
        dependency,
        review.predecessor_document_id,
        admissions,
        support_transfers,
    )
    if admission is None:
        return None, admission_reason or "admission_link_unavailable"

    predecessor_input = _input_by_candidate_id(
        readback.predecessor_inputs,
        review.predecessor_candidate_id,
    )
    successor_input = _input_by_candidate_id(
        readback.successor_inputs,
        review.successor_candidate_id,
    )
    if predecessor_input is None or successor_input is None:
        return None, "comparison_input_unavailable"
    successor_fields = (
        successor_input.get("payload_json") or {}
    ).get("fields")
    if (
        not isinstance(admission.admitted_fields, dict)
        or not isinstance(successor_fields, dict)
        or successor_fields != admission.admitted_fields
    ):
        return None, "successor_fields_not_exact"

    successor = session.get(Candidate, review.successor_candidate_id)
    if successor is None:
        return None, "successor_not_actionable"
    live_input = {
        **candidate_input_snapshot(successor),
        "extraction_run_id": successor.extraction_run_id,
    }
    if live_input != successor_input:
        return None, "successor_candidate_changed"
    try:
        citation = _verified_successor_citation(
            successor_input,
            review.successor_document_id,
        )
    except ValueError:
        return None, "successor_provenance_unsafe"

    for scope in review.superseded_scopes:
        if scope.role == "publication":
            continue
        if scope.role != "readiness":
            return None, "unsupported_operative_support_role"
        prior_readiness = session.get(
            EvidenceLink,
            scope.evidence.evidence_link_id,
        )
        if (
            prior_readiness is None
            or prior_readiness.dependency_id != dependency.id
            or prior_readiness.satisfies_requirement is not True
        ):
            return None, "readiness_source_changed"

    if assess_only:
        return None, None

    try:
        transfer = _transfer_operative_scopes_under_lock(
            session,
            dependency_id=dependency.id,
            successor_document_id=review.successor_document_id,
            citation=citation,
            scopes=review.superseded_scopes,
            designated_by=MACHINE_ACTOR,
        )
    except UnsafeSupportTransfer:
        return None, "support_scope_changed"
    new_evidence = transfer.evidence
    before_receipt = transfer.before_json
    after_receipt = {
        "policy_approval_id": approval.id,
        "policy_sha256": approval.policy_sha256,
        "comparison_id": comparison.id,
        "finding_id": finding.id,
        "predecessor_candidate_id": review.predecessor_candidate_id,
        "successor_candidate_id": review.successor_candidate_id,
        "new_evidence_link_id": new_evidence.id,
        "scope_fingerprint": [list(item) for item in review.scope_fingerprint],
        "origin_admission_audit_id": admission.origin.audit_id,
        "predecessor_support_transfer_audit_id": (
            admission.latest_support_transfer_audit_id
        ),
        "moved_scopes": list(transfer.moved_scopes),
    }
    audit_entry = audit.record(
        session,
        actor=MACHINE_ACTOR,
        action=audit.AUTOMATIC_CARRY_FORWARD,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        before=before_receipt,
        after=after_receipt,
    )
    receipt = AutomaticCarryForwardReceipt(
        audit_log_id=audit_entry.id,
        project_id=project_id,
        policy_approval_id=approval.id,
        dependency_id=dependency.id,
        comparison_id=comparison.id,
        finding_id=finding.id,
        predecessor_candidate_id=review.predecessor_candidate_id,
        successor_candidate_id=review.successor_candidate_id,
        new_evidence_link_id=new_evidence.id,
        origin_admission_audit_id=admission.origin.audit_id,
        predecessor_support_transfer_audit_id=(
            admission.latest_support_transfer_audit_id
        ),
        before_json=before_receipt,
        after_json=after_receipt,
    )
    session.add(receipt)
    session.flush([receipt])
    return receipt, None


def _canonical_policy_json(
    *,
    runtime: AutomaticCarryForwardRuntime,
) -> dict:
    return runtime.canonical_policy_json()


def _safety_source_paths() -> tuple[tuple[str, Path], ...]:
    """Files whose deployed bytes define machine eligibility and mutation."""

    return (
        ("corridor.automatic_carry_forward", Path(__file__)),
        ("corridor.audit", Path(audit.__file__)),
        ("corridor.extraction_runs", Path(extraction_runs_module.__file__)),
        ("corridor.models", Path(models_module.__file__)),
        ("corridor.operative_support", Path(operative_support_module.__file__)),
        ("corridor.principals", Path(principals_module.__file__)),
        ("corridor.project_lock", Path(project_lock_module.__file__)),
        ("corridor.revision_comparison", Path(revision_comparison_module.__file__)),
        ("corridor.supersession", Path(supersession_module.__file__)),
        ("corridor.supersession_review", Path(supersession_review_module.__file__)),
        (
            "corridor.migrations.9d4f2a7c1e83",
            Path(__file__).parent
            / "migrations/versions/9d4f2a7c1e83_add_automatic_carry_forward.py",
        ),
    )


def _approval_is_current_policy(
    session: Session,
    approval: AutomaticCarryForwardPolicyApproval,
    *,
    runtime: AutomaticCarryForwardRuntime,
) -> bool:
    try:
        HumanPrincipal(approval.approved_by)
    except InvalidHumanPrincipal:
        return False
    expected = _canonical_policy_json(runtime=runtime)
    if not (
        approval.policy_version == POLICY_VERSION
        and approval.policy_json == expected
        and approval.policy_sha256 == _json_sha256(expected)
    ):
        return False
    authorization_entries = tuple(
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == audit.PROJECT,
                AuditLog.entity_id == approval.project_id,
                AuditLog.action
                == audit.AUTHORIZE_AUTOMATIC_CARRY_FORWARD,
            )
        ).all()
    )
    expected_after = {
        "policy_approval_id": approval.id,
        "policy_version": approval.policy_version,
        "policy_sha256": approval.policy_sha256,
    }
    matches = tuple(
        entry
        for entry in authorization_entries
        if (
            entry.actor == approval.approved_by
            and entry.human_principal == approval.approved_by
            and entry.after_json == expected_after
        )
    )
    return len(matches) == 1


def _durable_outcome_counts(
    session: Session,
    project_id: int,
) -> tuple[int, dict[str, int]]:
    carried_count = int(
        session.scalar(
            select(func.count(AutomaticCarryForwardOutcome.id)).where(
                AutomaticCarryForwardOutcome.project_id == project_id,
                AutomaticCarryForwardOutcome.outcome == OUTCOME_CARRIED,
            )
        )
        or 0
    )
    abstentions = tuple(
        session.execute(
            select(
                AutomaticCarryForwardOutcome.reason,
                func.count(AutomaticCarryForwardOutcome.id),
            )
            .where(
                AutomaticCarryForwardOutcome.project_id == project_id,
                AutomaticCarryForwardOutcome.outcome == OUTCOME_ABSTAINED,
                AutomaticCarryForwardOutcome.reason_version
                == ABSTENTION_REASON_VERSION,
                AutomaticCarryForwardOutcome.reason.in_(ABSTENTION_REASONS),
            )
            .group_by(AutomaticCarryForwardOutcome.reason)
        ).all()
    )
    return carried_count, {
        reason: count
        for reason, count in sorted(abstentions)
        if reason is not None
    }


def _durable_abstention_identities(
    session: Session,
    project_id: int,
) -> frozenset[tuple[int, int | None, int | None, int | None, int | None, str]]:
    return frozenset(
        (
            outcome.dependency_id,
            outcome.predecessor_candidate_id,
            outcome.successor_candidate_id,
            outcome.comparison_id,
            outcome.finding_id,
            outcome.reason,
        )
        for outcome in session.scalars(
            select(AutomaticCarryForwardOutcome).where(
                AutomaticCarryForwardOutcome.project_id == project_id,
                AutomaticCarryForwardOutcome.outcome == OUTCOME_ABSTAINED,
                AutomaticCarryForwardOutcome.reason_version
                == ABSTENTION_REASON_VERSION,
                AutomaticCarryForwardOutcome.reason.in_(ABSTENTION_REASONS),
            )
        ).all()
        if outcome.reason is not None
    )


def _abstention_identity(
    abstention: AutomaticCarryForwardAbstention,
) -> tuple[int, int | None, int | None, int | None, int | None, str]:
    return (
        abstention.dependency_id,
        abstention.predecessor_candidate_id,
        abstention.successor_candidate_id,
        abstention.comparison_id,
        abstention.finding_id,
        abstention.reason,
    )


def _record_run_outcomes(
    session: Session,
    *,
    project_id: int,
    approval: AutomaticCarryForwardPolicyApproval,
    result: AutomaticCarryForwardResult,
) -> AutomaticCarryForwardRun:
    existing_carried_receipt_ids = frozenset(
        receipt_audit_log_id
        for receipt_audit_log_id in session.scalars(
            select(AutomaticCarryForwardOutcome.receipt_audit_log_id).where(
                AutomaticCarryForwardOutcome.project_id == project_id,
                AutomaticCarryForwardOutcome.outcome == OUTCOME_CARRIED,
                AutomaticCarryForwardOutcome.receipt_audit_log_id.is_not(None),
            )
        ).all()
        if receipt_audit_log_id is not None
    )
    existing_abstention_identities = _durable_abstention_identities(
        session, project_id
    )
    seen_carried_receipt_ids: set[int] = set()
    new_receipts_list: list[AutomaticCarryForwardReceipt] = []
    for receipt in result.carried:
        if (
            receipt.audit_log_id in existing_carried_receipt_ids
            or receipt.audit_log_id in seen_carried_receipt_ids
        ):
            continue
        seen_carried_receipt_ids.add(receipt.audit_log_id)
        new_receipts_list.append(receipt)
    new_receipts = tuple(new_receipts_list)
    seen_abstention_identities: set[
        tuple[int, int | None, int | None, int | None, int | None, str]
    ] = set()
    new_abstentions_list: list[AutomaticCarryForwardAbstention] = []
    for abstention in result.abstentions:
        identity = _abstention_identity(abstention)
        if (
            identity in existing_abstention_identities
            or identity in seen_abstention_identities
        ):
            continue
        seen_abstention_identities.add(identity)
        new_abstentions_list.append(abstention)
    new_abstentions = tuple(new_abstentions_list)
    run = AutomaticCarryForwardRun(
        project_id=project_id,
        policy_approval_id=approval.id,
        policy_version=approval.policy_version,
        policy_sha256=approval.policy_sha256,
        abstention_reason_version=ABSTENTION_REASON_VERSION,
        carried_count=len(new_receipts),
        abstained_count=len(new_abstentions),
    )
    session.add(run)
    session.flush([run])
    for receipt in new_receipts:
        session.add(
            AutomaticCarryForwardOutcome(
                run_id=run.id,
                project_id=project_id,
                policy_approval_id=approval.id,
                dependency_id=receipt.dependency_id,
                outcome=OUTCOME_CARRIED,
                reason=None,
                reason_version=None,
                receipt_audit_log_id=receipt.audit_log_id,
                comparison_id=receipt.comparison_id,
                finding_id=receipt.finding_id,
                predecessor_candidate_id=receipt.predecessor_candidate_id,
                successor_candidate_id=receipt.successor_candidate_id,
            )
        )
    for abstention in new_abstentions:
        session.add(
            AutomaticCarryForwardOutcome(
                run_id=run.id,
                project_id=project_id,
                policy_approval_id=approval.id,
                dependency_id=abstention.dependency_id,
                outcome=OUTCOME_ABSTAINED,
                reason=abstention.reason,
                reason_version=abstention.reason_version,
                receipt_audit_log_id=None,
                comparison_id=abstention.comparison_id,
                finding_id=abstention.finding_id,
                predecessor_candidate_id=abstention.predecessor_candidate_id,
                successor_candidate_id=abstention.successor_candidate_id,
            )
        )
    session.flush()
    return run


def _json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _current_review(
    session: Session,
    project_id: int,
    seed: SupersessionReview,
) -> SupersessionReview | None:
    matches = tuple(
        review
        for review in build_reviewer_worklist(session, project_id).reviews
        if (
            review.dependency_id == seed.dependency_id
            and review.predecessor_document_id
            == seed.predecessor_document_id
            and review.successor_candidate_id == seed.successor_candidate_id
            and review.comparison_id == seed.comparison_id
            and review.finding_id == seed.finding_id
            and review.scope_fingerprint == seed.scope_fingerprint
            and (
                review.reconfirmation_available
                or review.reason == "admission_fields_changed"
            )
        )
    )
    return matches[0] if len(matches) == 1 else None


def _ordinary_reason(review: SupersessionReview) -> str:
    if review.reason:
        return review.reason
    if review.status in {
        "changed",
        "dropped",
        "ambiguous",
        "unmatched",
    }:
        return f"comparison_{review.status}"
    return review.status


def _stable_abstention_reason(reason: str) -> tuple[str, str | None]:
    """Keep public reason codes closed while retaining an unknown detail."""

    if reason in ABSTENTION_REASONS:
        return reason, None
    return "unclassified_unsafe", reason


def _abstention(
    review: SupersessionReview,
    reason: str,
) -> AutomaticCarryForwardAbstention:
    assert review.dependency_id is not None
    stable_reason, detail = _stable_abstention_reason(reason)
    return AutomaticCarryForwardAbstention(
        dependency_id=review.dependency_id,
        predecessor_candidate_id=review.predecessor_candidate_id,
        successor_candidate_id=review.successor_candidate_id,
        comparison_id=review.comparison_id,
        finding_id=review.finding_id,
        reason=stable_reason,
        detail=detail,
    )


def _review_key(review: SupersessionReview) -> tuple[int, int, int]:
    return (
        review.dependency_id or 0,
        review.predecessor_document_id or 0,
        review.successor_candidate_id or 0,
    )
