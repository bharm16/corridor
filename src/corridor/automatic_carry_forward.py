"""Corridor-managed transfer of exact unchanged Operative Support.

Automatic Carry-Forward is deliberately not Admission and not human
Reconfirmation under a borrowed identity. The released policy may transfer only
the complete support scope already grounded in an attributable Admission. Every
uncertain row remains unresolved with a stable Abstention reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import audit
from corridor import policy
from corridor import extraction_runs as extraction_runs_module
from corridor import models as models_module
from corridor import operative_support as operative_support_module
from corridor import principals as principals_module
from corridor import project_lock as project_lock_module
from corridor import revision_comparison as revision_comparison_module
from corridor import supersession as supersession_module
from corridor import supersession_review as supersession_review_module
from corridor import support_transfer as support_transfer_module
from corridor.models import (
    AutomaticCarryForwardOutcome,
    AutomaticCarryForwardReceipt,
    PolicyRun,
    Project,
)
from corridor.operative_support import UnsafeSupportTransfer
from corridor.project_lock import lock_project
from corridor.revision_comparison import (
    DEFAULT_MATCHER_CONFIG,
    DEFAULT_MATCHER_VERSION,
)
from corridor.support_transfer import (
    SupportTransferProofRefusal,
    apply_proven_support_transfer,
    prove_support_transfer,
)
from corridor.supersession_review import (
    SupersessionReview,
    build_reviewer_worklist,
)


POLICY_VERSION = "automatic-carry-forward-v2"
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
    """Current read-only released-policy and routing status for one project."""

    project_id: int
    policy_version: str
    policy_sha256: str
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
            "matcher_config_sha256": policy.canonical_sha256(matcher_config),
        }

    def rules_digest(self) -> str:
        return policy.source_digest(self.safety_sources)

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
    policy_json = _canonical_policy_json(runtime=runtime)
    policy_sha256 = policy.canonical_sha256(policy_json)
    carried_count, reason_counts = _durable_outcome_counts(session, project_id)
    durable_abstentions = _durable_abstention_identities(session, project_id)
    if worklist is None:
        worklist = build_reviewer_worklist(session, project_id)

    def count_if_new(review: SupersessionReview, reason: str) -> None:
        abstention = _abstention(review, reason)
        if _abstention_identity(abstention) in durable_abstentions:
            return
        reason_counts[abstention.reason] = reason_counts.get(abstention.reason, 0) + 1

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
            policy_json=policy_json,
            policy_sha256=policy_sha256,
            review=review,
            assess_only=True,
        )
        if reason is None:
            eligible_count += 1
        else:
            count_if_new(review, reason)

    return AutomaticCarryForwardStatus(
        project_id=project_id,
        policy_version=POLICY_VERSION,
        policy_sha256=policy_sha256,
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
    policy_json = _canonical_policy_json(runtime=runtime)
    policy_sha256 = policy.canonical_sha256(policy_json)

    carried: list[AutomaticCarryForwardReceipt] = []
    abstentions: list[AutomaticCarryForwardAbstention] = []
    with session.begin_nested():
        session.flush()
        lock_project(session, project_id)
        session.expire_all()
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

        carry_candidates = worklist.reconfirmation + edited_conclusion_reviews
        for seed in sorted(carry_candidates, key=_review_key):
            with session.begin_nested():
                current = _current_review(session, project_id, seed)
                if current is None:
                    abstentions.append(_abstention(seed, "review_stale"))
                    continue
                receipt, reason = _carry_one(
                    session,
                    project_id=project_id,
                    policy_json=policy_json,
                    policy_sha256=policy_sha256,
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
            policy_sha256=policy_sha256,
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
    policy_json: dict,
    policy_sha256: str,
    review: SupersessionReview,
    assess_only: bool = False,
) -> tuple[AutomaticCarryForwardReceipt | None, str | None]:
    try:
        proof = prove_support_transfer(
            session,
            project_id=project_id,
            review=review,
            expected_matcher_version=policy_json.get("matcher_version"),
            expected_matcher_config_sha256=policy_json.get("matcher_config_sha256"),
            require_exact_admitted_fields=True,
        )
    except SupportTransferProofRefusal as exc:
        return None, exc.reason

    if assess_only:
        return None, None

    try:
        transfer = apply_proven_support_transfer(
            session,
            proof,
            designated_by=MACHINE_ACTOR,
        )
    except UnsafeSupportTransfer:
        return None, "support_scope_changed"
    new_evidence = transfer.evidence
    before_receipt = transfer.before_json
    after_receipt = {
        "policy_version": POLICY_VERSION,
        "policy_sha256": policy_sha256,
        "comparison_id": proof.comparison.id,
        "finding_id": proof.finding.id,
        "predecessor_candidate_id": review.predecessor_candidate_id,
        "successor_candidate_id": review.successor_candidate_id,
        "new_evidence_link_id": new_evidence.id,
        "scope_fingerprint": [list(item) for item in proof.scope_fingerprint],
        "origin_admission_audit_id": proof.admission.origin.audit_id,
        "predecessor_support_transfer_audit_id": (
            proof.admission.latest_support_transfer_audit_id
        ),
        "moved_scopes": list(transfer.moved_scopes),
    }
    audit_entry = audit.record(
        session,
        actor=MACHINE_ACTOR,
        action=audit.AUTOMATIC_CARRY_FORWARD,
        entity_type=audit.DEPENDENCY,
        entity_id=proof.dependency.id,
        before=before_receipt,
        after=after_receipt,
    )
    receipt = AutomaticCarryForwardReceipt(
        audit_log_id=audit_entry.id,
        project_id=project_id,
        policy_approval_id=None,
        policy_version=POLICY_VERSION,
        policy_sha256=policy_sha256,
        dependency_id=proof.dependency.id,
        comparison_id=proof.comparison.id,
        finding_id=proof.finding.id,
        predecessor_candidate_id=review.predecessor_candidate_id,
        successor_candidate_id=review.successor_candidate_id,
        new_evidence_link_id=new_evidence.id,
        origin_admission_audit_id=proof.admission.origin.audit_id,
        predecessor_support_transfer_audit_id=(
            proof.admission.latest_support_transfer_audit_id
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
        ("corridor.policy", Path(policy.__file__)),
        ("corridor.principals", Path(principals_module.__file__)),
        ("corridor.project_lock", Path(project_lock_module.__file__)),
        ("corridor.revision_comparison", Path(revision_comparison_module.__file__)),
        ("corridor.supersession", Path(supersession_module.__file__)),
        ("corridor.supersession_review", Path(supersession_review_module.__file__)),
        ("corridor.support_transfer", Path(support_transfer_module.__file__)),
        (
            "corridor.migrations.9d4f2a7c1e83",
            Path(__file__).parent
            / "migrations/versions/9d4f2a7c1e83_add_automatic_carry_forward.py",
        ),
        (
            "corridor.migrations.f315b4c6d8e0",
            Path(__file__).parent
            / "migrations/versions/f315b4c6d8e0_carry_forward_is_corridor_managed.py",
        ),
    )


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
        reason: count for reason, count in sorted(abstentions) if reason is not None
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
    policy_sha256: str,
    result: AutomaticCarryForwardResult,
) -> PolicyRun:
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
    existing_abstention_identities = _durable_abstention_identities(session, project_id)
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
    run = PolicyRun(
        project_id=project_id,
        family="automatic-carry-forward",
        policy_approval_id=None,
        policy_version=POLICY_VERSION,
        policy_sha256=policy_sha256,
        abstention_reason_version=ABSTENTION_REASON_VERSION,
        applied_count=len(new_receipts),
        abstained_count=len(new_abstentions),
    )
    session.add(run)
    session.flush([run])
    for receipt in new_receipts:
        session.add(
            AutomaticCarryForwardOutcome(
                run_id=run.id,
                project_id=project_id,
                policy_approval_id=None,
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
                policy_approval_id=None,
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
            and review.predecessor_document_id == seed.predecessor_document_id
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
