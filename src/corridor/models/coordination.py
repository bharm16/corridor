"""Coordination receipts: what a person settled, and what carried forward.

Statement coordination, its reversal and reversal effects, follow-up plans,
reconfirmation, and automatic carry-forward. A reversal is a new row rather than
a delete so the coordination history stays readable, and carry-forward records
its outcome per subject because the alternative -- inferring it from the absence
of a new statement -- could not distinguish "unchanged" from "never looked at".
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base


__all__ = [
    "AutomaticCarryForwardOutcome",
    "AutomaticCarryForwardReceipt",
    "FollowUpPlanReceipt",
    "FollowUpPlanReversal",
    "ReconfirmationReceipt",
    "StatementCoordinationReceipt",
    "StatementCoordinationReversal",
    "StatementCoordinationReversalEffect",
    "StatementSuggestionEligibilityDeclaration",
    "StatementSuggestionProtection",
    "StatementSuggestionProtectionEnd",
]


class StatementSuggestionEligibilityDeclaration(Base):
    """One explicit approval to expose deterministic statement ordering."""

    __tablename__ = "statement_suggestion_eligibility_declarations"
    __table_args__ = (
        CheckConstraint(
            "length(trim(contract_version)) > 0",
            name="ck_statement_suggestion_eligibility_contract",
        ),
        UniqueConstraint(
            "candidate_id", name="uq_statement_suggestion_eligibility_candidate"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    contract_version: Mapped[str] = mapped_column(String(128))
    declared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementSuggestionProtection(Base):
    """One declared cohort window during which statement ordering is withheld."""

    __tablename__ = "statement_suggestion_protections"
    __table_args__ = (
        CheckConstraint(
            "kind in ('shadow_cohort', 'no_agent_baseline')",
            name="ck_statement_suggestion_protection_kind",
        ),
        CheckConstraint(
            "length(trim(observation_contract)) > 0",
            name="ck_statement_suggestion_protection_contract",
        ),
        UniqueConstraint(
            "candidate_id",
            "kind",
            "observation_contract",
            name="uq_statement_suggestion_protection_window",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    observation_contract: Mapped[str] = mapped_column(String(128))
    declared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementSuggestionProtectionEnd(Base):
    """Append-only conclusion of one declared suggestion-protection window."""

    __tablename__ = "statement_suggestion_protection_ends"
    __table_args__ = (
        UniqueConstraint(
            "protection_id", name="uq_statement_suggestion_protection_end"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    protection_id: Mapped[int] = mapped_column(
        ForeignKey("statement_suggestion_protections.id"), index=True
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StatementCoordinationReceipt(Base):
    """The immutable grouping identity for one guided statement Save.

    The rows named here remain independent statement, scope, Work Decision,
    Evidence, and audit facts.  This receipt only states which exact rows the
    coordinator saved together and which predecessors the screen had read.
    """

    __tablename__ = "statement_coordination_receipts"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(expected_predecessors_json) = 'object'",
            name="ck_statement_coordination_receipt_predecessors_object",
        ),
        CheckConstraint(
            "jsonb_typeof(accepted_facts_json) = 'object'",
            name="ck_statement_coordination_receipt_facts_object",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_statement_coordination_receipt_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    candidate_disposition_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_dispositions.id"), unique=True
    )
    commitment_lineage_id: Mapped[int] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    dependency_event_id: Mapped[int] = mapped_column(
        ForeignKey("dependency_events.id"), unique=True
    )
    scope_decision_id: Mapped[int] = mapped_column(
        ForeignKey("dependency_event_scope_decisions.id"), unique=True
    )
    internal_owner_roster_entry_id: Mapped[int] = mapped_column(
        ForeignKey("project_roster_entries.id")
    )
    internal_owner_decision_id: Mapped[int] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    next_action_decision_id: Mapped[int] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    milestone_impact_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    expected_predecessors_json: Mapped[dict] = mapped_column(JSONB)
    accepted_facts_json: Mapped[dict] = mapped_column(JSONB)
    candidate_payload_sha256: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementCoordinationReversal(Base):
    """The attributable compensating act for one guided result.

    A reversal names either one grouped Save or one Not Relevant disposition.
    It changes only current projections; the source rows and their original
    receipts remain immutable history.
    """

    __tablename__ = "statement_coordination_reversals"
    __table_args__ = (
        CheckConstraint(
            "(receipt_id is not null and candidate_disposition_id is null) or "
            "(receipt_id is null and candidate_disposition_id is not null)",
            name="ck_statement_coordination_reversals_one_source",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_statement_coordination_reversals_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int | None] = mapped_column(
        ForeignKey("statement_coordination_receipts.id"), unique=True
    )
    candidate_disposition_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_dispositions.id"), unique=True
    )
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementCoordinationReversalEffect(Base):
    """One exact result made noncurrent by a compensating command."""

    __tablename__ = "statement_coordination_reversal_effects"
    __table_args__ = (
        CheckConstraint(
            "effect_kind in ("
            "'statement', 'scope_decision', 'work_decision', 'milestone_link', "
            "'candidate_disposition', 'candidate_projection', 'lineage_projection', "
            "'audit_pointer', 'grouping_receipt'"
            ")",
            name="ck_statement_coordination_reversal_effects_kind",
        ),
        UniqueConstraint(
            "reversal_id",
            "effect_kind",
            "target_id",
            name="uq_statement_coordination_reversal_effect",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reversal_id: Mapped[int] = mapped_column(
        ForeignKey("statement_coordination_reversals.id")
    )
    effect_kind: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FollowUpPlanReceipt(Base):
    """The immutable grouping identity for one Constraint Follow-up Plan Save.

    The named rows remain independent append-only Work Decisions (ADR-0038);
    this receipt only states which decisions one Save committed together,
    which predecessors the screen had read, and which exact roster row
    supplied the rendered Assigned To name.  No statement is manufactured to
    give a Constraint a grouping receipt (#333).
    """

    __tablename__ = "follow_up_plan_receipts"
    __table_args__ = (
        CheckConstraint(
            "internal_owner_decision_id is not null "
            "or next_action_decision_id is not null",
            name="ck_follow_up_plan_receipt_one_result",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_predecessors_json) = 'object'",
            name="ck_follow_up_plan_receipt_predecessors_object",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_follow_up_plan_receipt_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), index=True
    )
    internal_owner_roster_entry_id: Mapped[int] = mapped_column(
        ForeignKey("project_roster_entries.id")
    )
    internal_owner_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    next_action_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    resumed_deferral_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    expected_predecessors_json: Mapped[dict] = mapped_column(JSONB)
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FollowUpPlanReversal(Base):
    """The attributable compensating act for one grouped plan Save.

    Undo never edits or deletes the original decisions.  It appends one
    reversal Work Decision per grouped chain, restoring each predecessor
    value, and this row names those appended reversals so the grouped act
    stays auditable as one.
    """

    __tablename__ = "follow_up_plan_reversals"
    __table_args__ = (
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_follow_up_plan_reversal_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("follow_up_plan_receipts.id"), unique=True
    )
    internal_owner_reversal_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    next_action_reversal_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    deferral_reversal_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReconfirmationReceipt(Base):
    """Immutable binding behind one human Reconfirmation audit entry.

    Audit JSON remains the readable history. This sealed copy prevents a
    malformed or edited JSON pointer from making the already-used successor
    Candidate writable again or from inventing a different transferred scope.
    """

    __tablename__ = "reconfirmation_receipts"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(before_json) = 'object'",
            name="ck_reconfirmation_receipt_before_object",
        ),
        CheckConstraint(
            "jsonb_typeof(after_json) = 'object'",
            name="ck_reconfirmation_receipt_after_object",
        ),
    )

    audit_log_id: Mapped[int] = mapped_column(
        ForeignKey("audit_log.id", ondelete="CASCADE"), primary_key=True
    )
    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), index=True
    )
    successor_candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    before_json: Mapped[dict] = mapped_column(JSONB)
    after_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AutomaticCarryForwardReceipt(Base):
    """Immutable machine-transfer identity bound to its readable audit row."""

    __tablename__ = "automatic_carry_forward_receipts"
    __table_args__ = (
        UniqueConstraint(
            "dependency_id",
            "successor_candidate_id",
            name="uq_automatic_carry_forward_dependency_successor",
        ),
        UniqueConstraint(
            "new_evidence_link_id",
            name="uq_automatic_carry_forward_new_evidence",
        ),
        CheckConstraint(
            "jsonb_typeof(before_json) = 'object'",
            name="ck_automatic_carry_forward_receipt_before_object",
        ),
        CheckConstraint(
            "jsonb_typeof(after_json) = 'object'",
            name="ck_automatic_carry_forward_receipt_after_object",
        ),
        CheckConstraint(
            "family = 'automatic-carry-forward'",
            name="ck_automatic_carry_forward_receipt_family",
        ),
        ForeignKeyConstraint(
            ["project_id", "family", "policy_approval_id"],
            [
                "policy_approvals.project_id",
                "policy_approvals.family",
                "policy_approvals.id",
            ],
            name="fk_automatic_carry_forward_receipt_policy_project",
        ),
        ForeignKeyConstraint(
            ["dependency_id", "new_evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_automatic_carry_forward_receipt_dependency_evidence",
        ),
    )

    audit_log_id: Mapped[int] = mapped_column(
        ForeignKey("audit_log.id"), primary_key=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    family: Mapped[str] = mapped_column(
        String(32), server_default="automatic-carry-forward"
    )
    policy_approval_id: Mapped[int | None] = mapped_column(BigInteger)
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), index=True
    )
    comparison_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    finding_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_findings.id")
    )
    predecessor_candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    successor_candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    new_evidence_link_id: Mapped[int] = mapped_column(BigInteger)
    origin_admission_audit_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"))
    predecessor_support_transfer_audit_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_log.id")
    )
    before_json: Mapped[dict] = mapped_column(JSONB)
    after_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AutomaticCarryForwardOutcome(Base):
    """One immutable row outcome within a Carry-Forward batch receipt."""

    __tablename__ = "automatic_carry_forward_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "id",
            name="uq_automatic_carry_forward_outcome_project_id",
        ),
        UniqueConstraint(
            "receipt_audit_log_id",
            name="uq_automatic_carry_forward_outcome_receipt_audit",
        ),
        CheckConstraint(
            "outcome in ('carried', 'abstained')",
            name="ck_automatic_carry_forward_outcome_value",
        ),
        CheckConstraint(
            "("
            "outcome = 'carried' and reason is null and reason_version is null "
            "and receipt_audit_log_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and reason_version is not null and receipt_audit_log_id is null"
            ")",
            name="ck_automatic_carry_forward_outcome_kind",
        ),
        CheckConstraint(
            "family = 'automatic-carry-forward'",
            name="ck_automatic_carry_forward_outcome_family",
        ),
        ForeignKeyConstraint(
            ["project_id", "family", "run_id"],
            [
                "policy_runs.project_id",
                "policy_runs.family",
                "policy_runs.id",
            ],
            name="fk_automatic_carry_forward_outcome_run_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "family", "policy_approval_id"],
            [
                "policy_approvals.project_id",
                "policy_approvals.family",
                "policy_approvals.id",
            ],
            name="fk_automatic_carry_forward_outcome_policy_project",
        ),
        Index(
            "uq_automatic_carry_forward_outcome_abstained_identity",
            "project_id",
            "policy_approval_id",
            "dependency_id",
            text("coalesce(comparison_id, -1)"),
            text("coalesce(finding_id, -1)"),
            text("coalesce(predecessor_candidate_id, -1)"),
            text("coalesce(successor_candidate_id, -1)"),
            "reason",
            "reason_version",
            unique=True,
            postgresql_where=text("outcome = 'abstained'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(
        String(32), server_default="automatic-carry-forward"
    )
    policy_approval_id: Mapped[int | None] = mapped_column(BigInteger)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    outcome: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(128))
    reason_version: Mapped[str | None] = mapped_column(String(64))
    receipt_audit_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("automatic_carry_forward_receipts.audit_log_id")
    )
    comparison_id: Mapped[int | None] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("revision_comparison_findings.id")
    )
    predecessor_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id")
    )
    successor_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
