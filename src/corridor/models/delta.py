"""Proposed Deltas and their resolution: the difference, then the decision.

A Proposed Delta is a typed difference from the accepted record, and the record
is unchanged while it stays open. Resolve Delta turns one into a single atomic
Project Record Revision. The dispositions, deferrals, supersessions and Review
Packet children are all here because they are the same transaction's parts: an
earlier design resolved deltas one row at a time and could leave a packet half
applied, which #526 closed by making the packet one transaction.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base


__all__ = [
    "DELTA_CHANGE_TYPES",
    "DELTA_DISPOSITIONS",
    "DELTA_EFFECT_KINDS",
    "DELTA_ORGANIZATION_CHANGE_KINDS",
    "DELTA_TARGET_TYPES",
    "DeltaCaptureCorrection",
    "DeltaDecisionSupport",
    "DeltaDeferral",
    "DeltaDisposition",
    "DeltaFollowUpPlan",
    "DeltaFollowUpPlanClosure",
    "DeltaFollowUpPlanEvidence",
    "DeltaGroup",
    "DeltaRecordDecision",
    "DeltaReviewPacketChild",
    "DeltaReviewPacketReceipt",
    "DeltaReviewPacketReversal",
    "DeltaReviewPacketSupport",
    "DeltaSupersession",
    "CANCELLATION_REASONS",
    "CAPTURE_CORRECTION_OUTCOMES",
    "CLOSURE_KINDS",
    "CaptureCorrectionRequest",
    "CaptureCorrectionResult",
    "OutgoingRequest",
    "OutgoingRequestPlan",
    "OutgoingRequestResponse",
    "PACKET_CHILD_OUTCOMES",
    "PACKET_GROUPING_KEY_KINDS",
    "PACKET_SEMANTIC_OUTCOMES",
    "ProposedDelta",
    "ProposedDeltaImpactDerivation",
]


DELTA_CHANGE_TYPES = ("add", "modify", "apparent_removal")
DELTA_TARGET_TYPES = ("existing_subject", "proposed_subject")
DELTA_DISPOSITIONS = ("accept", "edit", "reject")
# The typed effect one resolved delta has on the accepted record (#519,
# ADR-0076 as amended by ADR-0083 and ADR-0084).
DELTA_EFFECT_KINDS = (
    "new_subject",
    "changed_field",
    "timing",
    "organization",
    "apparent_removal",
    "contradiction",
    "schedule_key_date",
    "closure",
)
DELTA_ORGANIZATION_CHANGE_KINDS = ("correction", "changed_ownership")
_DELTA_EFFECT_KINDS_SQL = ", ".join(f"'{value}'" for value in DELTA_EFFECT_KINDS)


class DeltaGroup(Base):
    """One atomic source change binding proposed deltas for source lineage (#518)."""

    __tablename__ = "delta_groups"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_delta_groups_project_id"),
        # One atomic source change per source version (#457): replaying a
        # version, or taking it in more than one batch, joins the group that
        # version already opened instead of leaving another behind. The
        # document and the statement are part of the identity and either may
        # be absent, so two absences are the same absence.
        UniqueConstraint(
            "project_id",
            "source_family",
            "source_revision",
            "document_id",
            "statement_id",
            name="uq_delta_groups_source_change",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_family: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[str] = mapped_column(String(128))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    statement_id: Mapped[int | None] = mapped_column(ForeignKey("dependency_events.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProposedDelta(Base):
    """One immutable occurrence of a proposed delta against accepted record (#518)."""

    __tablename__ = "proposed_deltas"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_proposed_deltas_project_id"),
        UniqueConstraint("content_sha256", name="uq_proposed_deltas_content"),
        ForeignKeyConstraint(
            ["project_id", "group_id"],
            ["delta_groups.project_id", "delta_groups.id"],
            name="fk_proposed_deltas_group",
        ),
        CheckConstraint(
            "change_type in ('add', 'modify', 'apparent_removal')",
            name="ck_proposed_deltas_change_type",
        ),
        CheckConstraint(
            "target_type in ('existing_subject', 'proposed_subject')",
            name="ck_proposed_deltas_target_type",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_proposed_deltas_content_sha256",
        ),
        Index("ix_proposed_deltas_target", "project_id", "target_subject_identity"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    change_type: Mapped[str] = mapped_column(String(32))
    target_type: Mapped[str] = mapped_column(String(32))
    target_subject_identity: Mapped[str] = mapped_column(String(128))
    target_field: Mapped[str | None] = mapped_column(String(64))
    accepted_value: Mapped[Any | None] = mapped_column(JSONB)
    proposed_value: Mapped[Any | None] = mapped_column(JSONB)
    source_family: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[str] = mapped_column(String(128))
    comparison_rule_version: Mapped[str] = mapped_column(String(64))
    accepted_baseline_revision: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProposedDeltaImpactDerivation(Base):
    """Immutable versioned consequences, never Source Facts or accepted values."""

    __tablename__ = "proposed_delta_impact_derivations"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"]),
        UniqueConstraint("project_id", "delta_id", "rule", "rule_version", "input_sha256"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    delta_id: Mapped[int] = mapped_column(BigInteger)
    rule: Mapped[str] = mapped_column(Text)
    rule_version: Mapped[str] = mapped_column(Text)
    accepted_revision_id: Mapped[int | None] = mapped_column(ForeignKey("project_record_revisions.id"))
    inputs: Mapped[dict] = mapped_column(JSONB)
    input_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    affected_constraint_ids: Mapped[list] = mapped_column(JSONB)
    affected_key_dates: Mapped[list] = mapped_column(JSONB)
    derivation_sha256: Mapped[str] = mapped_column(String(64))


class DeltaDisposition(Base):
    """Semantic resolution (accept, edit, reject) of a proposed delta (#518)."""

    __tablename__ = "delta_dispositions"
    __table_args__ = (
        UniqueConstraint("delta_id", name="uq_delta_dispositions_delta"),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_dispositions_delta",
        ),
        CheckConstraint(
            "disposition in ('accept', 'edit', 'reject')",
            name="ck_delta_dispositions_disposition",
        ),
        CheckConstraint(
            "(decided_by_principal is null) <> (decided_by_policy is null)",
            name="ck_delta_dispositions_authority_xor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    disposition: Mapped[str] = mapped_column(String(32))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by_principal: Mapped[str | None] = mapped_column(String(128))
    decided_by_policy: Mapped[str | None] = mapped_column(String(128))
    rationale: Mapped[str | None] = mapped_column(Text)
    effective_value: Mapped[Any | None] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaSupersession(Base):
    """Lineage link when a newer source revision supersedes a prior delta (#518)."""

    __tablename__ = "delta_supersessions"
    __table_args__ = (
        UniqueConstraint("prior_delta_id", name="uq_delta_supersessions_prior"),
        ForeignKeyConstraint(
            ["project_id", "source_reading_id"],
            ["inbound_thread_readings.project_id", "inbound_thread_readings.id"],
            use_alter=True, name="fk_delta_supersessions_reading_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "prior_delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_supersessions_prior",
        ),
        ForeignKeyConstraint(
            ["project_id", "superseding_delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_supersessions_superseding",
        ),
        CheckConstraint(
            "prior_delta_id <> superseding_delta_id",
            name="ck_delta_supersessions_not_self",
        ),
        ForeignKeyConstraint(["project_id", "minutes_capture_id"], ["minutes_captures.project_id", "minutes_captures.id"],
                             use_alter=True, name="fk_delta_supersession_minutes"),
        CheckConstraint("superseding_delta_id is not null or source_reading_id is not null or minutes_capture_id is not null",
                        name="ck_delta_supersessions_successor"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    prior_delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    superseding_delta_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    source_reading_id: Mapped[int | None] = mapped_column(BigInteger)
    minutes_capture_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(64))
    superseded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaDeferral(Base):
    """Attributable Work List scheduling leaving delta open (#518, ADR-0035)."""

    __tablename__ = "delta_deferrals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_deferrals_delta",
        ),
        # Scheduling writes no Project Record revision (ADR-0084), so the act
        # carries no idempotency key; the delta, the instant it was scheduled
        # at, and the person who scheduled it are its identity, and a retried
        # Defer returns the receipt already written (#457).
        UniqueConstraint(
            "delta_id",
            "deferred_at",
            "scheduled_by_principal",
            name="uq_delta_deferrals_occurrence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    deferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deferred_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wake_condition: Mapped[str | None] = mapped_column(String(128))
    scheduled_by_principal: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaRecordDecision(Base):
    """The authority binding of one resolved Proposed Delta (#519).

    ``DeltaDisposition`` says the delta resolved; this row says by whose
    authority, in which Project Record revision, with which typed effect, and
    — for an edit — on which constrained basis the edited value is still
    source-backed.  Written only by ``resolve_proposed_delta_decision``; a
    wrong decision is corrected by a later attributable decision, never by an
    update (ADR-0076, ADR-0084).
    """

    __tablename__ = "delta_record_decisions"
    __table_args__ = (
        UniqueConstraint(
            "disposition_id", name="uq_delta_record_decisions_disposition"
        ),
        UniqueConstraint("delta_id", name="uq_delta_record_decisions_delta"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_delta_record_decisions_key"
        ),
        UniqueConstraint(
            "project_id", "id", name="uq_delta_record_decisions_project_id"
        ),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_record_decisions_delta",
        ),
        CheckConstraint(
            "disposition in ('accept', 'edit', 'reject')",
            name="ck_delta_record_decisions_disposition",
        ),
        CheckConstraint(
            f"effect_kind in ({_DELTA_EFFECT_KINDS_SQL})",
            name="ck_delta_record_decisions_effect_kind",
        ),
        CheckConstraint(
            "(effect_kind = 'organization' and organization_change_kind in "
            "('correction', 'changed_ownership')) or "
            "(effect_kind <> 'organization' and organization_change_kind is null)",
            name="ck_delta_record_decisions_organization",
        ),
        CheckConstraint(
            "(disposition = 'edit') = (edit_basis is not null)",
            name="ck_delta_record_decisions_edit_basis",
        ),
        CheckConstraint(
            "length(btrim(decided_by_principal)) > 0",
            name="ck_delta_record_decisions_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    delta_id: Mapped[int] = mapped_column(BigInteger)
    disposition_id: Mapped[int] = mapped_column(
        ForeignKey("delta_dispositions.id")
    )
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("project_record_revisions.id"), index=True
    )
    disposition: Mapped[str] = mapped_column(String(32))
    effect_kind: Mapped[str] = mapped_column(String(32))
    organization_change_kind: Mapped[str | None] = mapped_column(String(32))
    decided_by_principal: Mapped[str] = mapped_column(String(128))
    observed_accepted_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    edit_basis: Mapped[Any | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaDecisionSupport(Base):
    """One effective Support Assessment a delta decision relied on (#519, #530).

    The decision names its support; a passed Source Passage Check is never
    read here and can never stand in for it (ADR-0082).
    """

    __tablename__ = "delta_decision_supports"
    __table_args__ = (
        UniqueConstraint(
            "decision_id",
            "support_assessment_id",
            name="uq_delta_decision_supports_member",
        ),
        UniqueConstraint(
            "decision_id", "ordinal", name="uq_delta_decision_supports_ordinal"
        ),
        ForeignKeyConstraint(
            ["project_id", "decision_id"],
            [
                "delta_record_decisions.project_id",
                "delta_record_decisions.id",
            ],
            name="fk_delta_decision_supports_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "support_assessment_id"],
            ["support_assessments.project_id", "support_assessments.id"],
            name="fk_delta_decision_supports_assessment",
        ),
        CheckConstraint("ordinal > 0", name="ck_delta_decision_supports_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    decision_id: Mapped[int] = mapped_column(BigInteger)
    support_assessment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


# The five outcomes one Review Packet child may carry (#526, ADR-0085).  The
# first four are ADR-0085's primary decisions; the fifth is its secondary
# dated Defer, which is Work List scheduling and not a semantic disposition
# (ADR-0084).
PACKET_CHILD_OUTCOMES = (
    "apply",
    "keep_current",
    "edit_and_apply",
    "needs_coordination",
    "defer",
)
PACKET_SEMANTIC_OUTCOMES = ("apply", "keep_current", "edit_and_apply")
# How a packet was keyed, so the receipt preserves the grouping basis a later
# reader would otherwise have to guess at (ADR-0085).
PACKET_GROUPING_KEY_KINDS = (
    "source_revision",
    "coordination_question",
    "shared_commitment",
)
# What a Follow-up Plan closure says happened to the plan (#835).
# ``superseded`` names the plan that replaced it; ``cancelled`` names a
# structured reason it is no longer needed.
CLOSURE_KINDS = ("superseded", "cancelled")
# The structured reasons a cancellation may carry.  ADR-0038 requires one on
# the legacy plan lifecycle and refuses free text as a substitute; the spine's
# plan is the same act, so it carries the same rule.  Prose belongs in the
# closure's ``note`` beside one of these, never instead of one.
CANCELLATION_REASONS = (
    "answered_another_way",
    "no_longer_needed",
    "raised_in_error",
    "asked_of_the_wrong_party",
)

_CLOSURE_KINDS_SQL = ", ".join(f"'{value}'" for value in CLOSURE_KINDS)
_CANCELLATION_REASONS_SQL = ", ".join(
    f"'{value}'" for value in CANCELLATION_REASONS
)
_PACKET_CHILD_OUTCOMES_SQL = ", ".join(f"'{value}'" for value in PACKET_CHILD_OUTCOMES)
_PACKET_GROUPING_KEY_KINDS_SQL = ", ".join(
    f"'{value}'" for value in PACKET_GROUPING_KEY_KINDS
)


class DeltaFollowUpPlan(Base):
    """The Follow-up Plan decision a Needs coordination outcome records (#526).

    ADR-0085 keeps Needs coordination among the four primary packet decisions
    and ADR-0084 forbids settling an external fact with free text, so the
    coordinator's answer to "I cannot settle this yet" has to be a recorded
    decision rather than a dropped selection.  The row is a separately
    identified decision inside the packet's one Project Record revision: it
    names the exact question, the person or organization who owes the answer,
    the date the question returns, the scope it affects, and the evidence that
    raised it.  It leaves the proposed value unaccepted and the Proposed Delta
    open, which is why it writes no ``delta_dispositions`` row.

    This is the spine's Follow-up Plan.  ``follow_up_plan_receipts`` is the
    frozen legacy grouping receipt over ``work_decisions`` (ADR-0081) and is
    not extended for adopted-baseline projects (ADR-0084 §3).
    """

    __tablename__ = "delta_follow_up_plans"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_delta_follow_up_plans_project_id"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_delta_follow_up_plans_key"
        ),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_follow_up_plans_delta",
        ),
        CheckConstraint(
            "length(btrim(open_question)) > 0",
            name="ck_delta_follow_up_plans_question",
        ),
        CheckConstraint(
            "responsible_principal is not null "
            "or responsible_organization is not null",
            name="ck_delta_follow_up_plans_responsible",
        ),
        CheckConstraint(
            "jsonb_typeof(affected_scope) = 'object'",
            name="ck_delta_follow_up_plans_scope_object",
        ),
        CheckConstraint(
            "length(btrim(recorded_by_principal)) > 0",
            name="ck_delta_follow_up_plans_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("project_record_revisions.id"), index=True
    )
    open_question: Mapped[str] = mapped_column(Text)
    responsible_principal: Mapped[str | None] = mapped_column(String(128))
    responsible_organization: Mapped[str | None] = mapped_column(String(255))
    return_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    affected_scope: Mapped[Any] = mapped_column(JSONB)
    recorded_by_principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaFollowUpPlanClosure(Base):
    """How one Follow-up Plan stopped being an outside ask (#835).

    ``DeltaFollowUpPlan`` is insert-only, and until this row existed a plan
    left the two readers that decide whether it is still waiting for exactly
    two reasons: its Proposed Delta stopped being open, or the packet act that
    recorded it was reversed.  A coordinator who had recorded the wrong
    question, or who no longer needed an outside answer, had nothing to record
    — and appending a corrected plan produced *two live asks for one question*,
    because nothing said one replaced the other.

    ``superseded`` names the plan that replaced this one; ``cancelled`` names a
    structured reason it is no longer needed (ADR-0038's rule, on the spine's
    relation).  Written only by ``close_delta_follow_up_plan``; a wrong closure
    is corrected by recording a new plan, never by an update.  It is not
    ``DeltaReviewPacketReversal``: Undo says the recorded act never stood and
    takes the packet's revision and citations back with it, where a closure
    says the ask was real and is finished.  It writes no Project Record
    revision (ADR-0084).
    """

    __tablename__ = "delta_follow_up_plan_closures"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_delta_follow_up_plan_closures_project_id"
        ),
        # A plan closes once: a second closure is two statements about one
        # plan, and the readers would have to choose between them.
        UniqueConstraint("plan_id", name="uq_delta_follow_up_plan_closures_plan"),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_delta_follow_up_plan_closures_key",
        ),
        ForeignKeyConstraint(
            ["project_id", "plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_delta_follow_up_plan_closures_plan",
        ),
        ForeignKeyConstraint(
            ["project_id", "successor_plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_delta_follow_up_plan_closures_successor",
        ),
        CheckConstraint(
            f"closure_kind in ({_CLOSURE_KINDS_SQL})",
            name="ck_delta_follow_up_plan_closures_kind",
        ),
        CheckConstraint(
            "(closure_kind = 'superseded' and successor_plan_id is not null "
            "and cancellation_reason is null) "
            "or (closure_kind = 'cancelled' and successor_plan_id is null "
            "and cancellation_reason is not null)",
            name="ck_delta_follow_up_plan_closures_shape",
        ),
        CheckConstraint(
            "cancellation_reason is null or cancellation_reason in "
            f"({_CANCELLATION_REASONS_SQL})",
            name="ck_delta_follow_up_plan_closures_reason",
        ),
        CheckConstraint(
            "successor_plan_id is null or successor_plan_id <> plan_id",
            name="ck_delta_follow_up_plan_closures_not_self",
        ),
        CheckConstraint(
            "length(btrim(closed_by_principal)) > 0",
            name="ck_delta_follow_up_plan_closures_principal",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_delta_follow_up_plan_closures_key_text",
        ),
        CheckConstraint(
            "note is null or (length(btrim(note)) > 0 and length(note) <= 2000)",
            name="ck_delta_follow_up_plan_closures_note",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    plan_id: Mapped[int] = mapped_column(BigInteger, index=True)
    closure_kind: Mapped[str] = mapped_column(String(32))
    successor_plan_id: Mapped[int | None] = mapped_column(BigInteger)
    cancellation_reason: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    closed_by_principal: Mapped[str] = mapped_column(String(128))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaFollowUpPlanEvidence(Base):
    """One effective Support Assessment a Follow-up Plan cited (#526, #530)."""

    __tablename__ = "delta_follow_up_plan_evidence"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "support_assessment_id",
            name="uq_delta_follow_up_plan_evidence_member",
        ),
        UniqueConstraint(
            "plan_id", "ordinal", name="uq_delta_follow_up_plan_evidence_ordinal"
        ),
        ForeignKeyConstraint(
            ["project_id", "plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_delta_follow_up_plan_evidence_plan",
        ),
        ForeignKeyConstraint(
            ["project_id", "support_assessment_id"],
            ["support_assessments.project_id", "support_assessments.id"],
            name="fk_delta_follow_up_plan_evidence_assessment",
        ),
        CheckConstraint("ordinal > 0", name="ck_delta_follow_up_plan_evidence_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    plan_id: Mapped[int] = mapped_column(BigInteger)
    support_assessment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


class OutgoingRequest(Base):
    """One retained outgoing request, and the response boundary it declared (#652).

    The chase list's ``unanswered_request`` band is the one place Corridor may
    say a specific thing has gone unanswered, and ADR-0090 retired the legacy
    ``STALE`` alert precisely because silence is not evidence: nobody sending a
    document does not mean anybody failed to answer.  A no-response fact
    therefore needs a *retained request* behind it, and until this table existed
    ``follow_up_bundles.read_retained_outgoing_requests`` truthfully returned
    nothing.  This is that record: what was asked, of which External
    Organization, covering which Utility Conflicts, where the exact sent content
    is retained and what it digests to, the declared expected-response boundary
    (silence before it is not a finding), the attributable sender and day, and
    the person who recorded the send.

    **The plans it advances are a relation, not a column** (#837, finishing the
    accepted #652 contract).  A follow-up bundle already groups several
    questions into one communication, so one email covers as many Follow-up
    Plans as the coordinator addressed in it, and one plan may take several
    requests before it is answered.  A singular ``follow_up_plan_id`` could only
    record the first of those honestly, so ``outgoing_request_plans`` carries
    the relation and this table carries no plan column at all.

    **The sender and the recorder are two people.**  ``sent_by_principal`` is
    whoever sent the message from their own mail client -- possibly a colleague
    the roster does not know, because Corridor sends nothing -- and
    ``recorded_by_principal`` is the person sitting in front of Corridor
    entering it.  They are often the same string and the schema never assumes
    it; "who sent this" and "who says it was sent" are different claims and a
    single column would merge them.

    It is Corridor-originated correspondence, not source-derived evidence and
    not an accepted-record decision, so it does not join the spine's append
    matrix.  It is written only through ``append_outgoing_request`` under the
    record-decision role, and it is append-only: a guard trigger refuses every
    update, delete, and truncate, and refuses an insert that does not arrive as
    that role, so not even the schema owner can write one raw (#492 idiom).

    **The exact sent content is retained, and a digest alone is not.**
    ``sent_content_key`` is the object-storage key the bytes live under
    (ADR-0079's one storage interface, never a ``bytea`` column), and because
    that key *contains* the digest, ``ck_outgoing_requests_content_key`` proves
    the two agree inside PostgreSQL without the bytes being present.  A chase
    for "no response" has to be able to show a coordinator what was actually
    sent, which a digest cannot do.

    **Correction is append-only.**  A request recorded wrongly is corrected by
    appending a corrected request naming the original in
    ``supersedes_request_id`` with its ``correction_reason``; the original stays
    exactly as it was, and being superseded is derived from the successor's
    existence rather than stored as a status.  ``expected_response_by`` is the
    resolved boundary the band reads; ``boundary_rule_version`` and
    ``boundary_interval_days`` record how it was derived when an interval and a
    rule produced it rather than a date stated outright -- but the date is
    always explicit and always required, because a silently derived interval is
    how somebody gets accused of not answering a question nobody set a date on.
    """

    __tablename__ = "outgoing_requests"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_outgoing_requests_project_id"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_outgoing_requests_key"
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_request_id"],
            ["outgoing_requests.project_id", "outgoing_requests.id"],
            name="fk_outgoing_requests_supersedes",
        ),
        CheckConstraint(
            "length(btrim(external_organization)) > 0",
            name="ck_outgoing_requests_organization",
        ),
        CheckConstraint(
            "length(btrim(question)) > 0", name="ck_outgoing_requests_question"
        ),
        CheckConstraint(
            "length(btrim(sent_by_principal)) > 0",
            name="ck_outgoing_requests_principal",
        ),
        CheckConstraint(
            "length(btrim(recorded_by_principal)) > 0",
            name="ck_outgoing_requests_recorder",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_outgoing_requests_digest"
        ),
        # The storage key carries the digest in its own name, so the agreement
        # between "what was sent" and "what it hashes to" is provable here
        # without the bytes: `<sha[:2]>/<sha><suffix>` is `object_storage`'s
        # only layout.
        CheckConstraint(
            "sent_content_key = substr(content_sha256, 1, 2) || '/' "
            "|| content_sha256 "
            "|| substr(sent_content_key, 68)",
            name="ck_outgoing_requests_content_key",
        ),
        CheckConstraint(
            "expected_response_by >= sent_on", name="ck_outgoing_requests_boundary"
        ),
        CheckConstraint(
            "jsonb_typeof(covered_subject_keys) = 'array'",
            name="ck_outgoing_requests_subjects",
        ),
        CheckConstraint(
            "boundary_interval_days is null or boundary_interval_days >= 0",
            name="ck_outgoing_requests_interval",
        ),
        # A correction names what it corrects and why, or is not a correction.
        CheckConstraint(
            "(supersedes_request_id is null) = (correction_reason is null)",
            name="ck_outgoing_requests_correction",
        ),
        # One correction per corrected request: a chain, never a fork, so
        # "which record stands" has one answer.
        Index(
            "uq_outgoing_requests_correction",
            "project_id",
            "supersedes_request_id",
            unique=True,
            postgresql_where=text("supersedes_request_id is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    external_organization: Mapped[str] = mapped_column(String(255))
    responsible_role: Mapped[str | None] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text)
    covered_subject_keys: Mapped[Any] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    sent_content_key: Mapped[str] = mapped_column(String(160))
    sent_on: Mapped[date] = mapped_column(Date)
    sent_by_principal: Mapped[str] = mapped_column(String(128))
    recorded_by_principal: Mapped[str] = mapped_column(String(128))
    expected_response_by: Mapped[date] = mapped_column(Date)
    boundary_rule_version: Mapped[str | None] = mapped_column(String(64))
    boundary_interval_days: Mapped[int | None] = mapped_column(Integer)
    supersedes_request_id: Mapped[int | None] = mapped_column(BigInteger)
    correction_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def recorded_by_the_sender(self) -> bool:
        """Whether the person who sent it is the person who recorded it.

        Derived, never stored. The page says which of the two happened rather
        than leaving a reader to assume that a recorded send was a first-hand
        one.
        """

        return self.sent_by_principal == self.recorded_by_principal


class OutgoingRequestPlan(Base):
    """One Follow-up Plan one retained outgoing request advances (#837).

    The accepted #652 contract makes this a relation for a reason a column
    cannot carry: a follow-up bundle is *one interaction covering several
    questions*, so a coordinator who writes to City Water about five Utility
    Conflicts sends one email advancing five Follow-up Plans, and a plan that
    goes unanswered takes a second and a third request. Recording that email
    against one plan would state something false about what was asked, and
    recording it five times would state five emails that never existed.

    A row here is therefore the only place "this request covered that plan" is
    written, and the absence of a row is just as load-bearing: a request that
    advanced three of a bundle's five plans leaves two uncovered, and the
    follow-up section says so rather than letting the bundle imply otherwise.

    Append-only under the same guard as the request it belongs to, written only
    through ``append_outgoing_request``, which writes the request and its whole
    plan set in one statement so a half-related request cannot exist.
    """

    __tablename__ = "outgoing_request_plans"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_outgoing_request_plans_row"),
        UniqueConstraint(
            "project_id",
            "request_id",
            "follow_up_plan_id",
            name="uq_outgoing_request_plans_pair",
        ),
        ForeignKeyConstraint(
            ["project_id", "request_id"],
            ["outgoing_requests.project_id", "outgoing_requests.id"],
            name="fk_outgoing_request_plans_request",
        ),
        ForeignKeyConstraint(
            ["project_id", "follow_up_plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_outgoing_request_plans_plan",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    request_id: Mapped[int] = mapped_column(BigInteger, index=True)
    follow_up_plan_id: Mapped[int] = mapped_column(BigInteger, index=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OutgoingRequestResponse(Base):
    """One observation that a reply arrived, with the evidence for it (#652).

    Recording that a reply arrived stops one retained request's silence clock:
    the ``unanswered_request`` band fires only for a retained request whose
    boundary has passed *and* which has no recorded response as of the
    reading's cutoff.

    **It stops the clock and settles nothing else.**  The accepted #652
    contract is explicit, and it is the whole reason this row is a separate
    fact from the record question: "recording a response stops the no-response
    clock. It does **not** automatically resolve the Follow-up Plan or change
    an accepted project value. The question can remain open even though the
    recipient replied."  Nothing here writes a delta disposition, a plan
    lifecycle act, or a Project Record revision, and nothing may be added that
    does.

    **Each observation carries its evidence.**  One of four things is what a
    coordinator actually has: the incoming ``Document``, the ``SourceDelivery``
    that brought it, the exact ``SourceSegment`` inside it, or an attributable
    manual observation -- somebody heard it on the telephone and says so under
    their own name.  ``evidence_kind`` names which, a check constraint makes
    the other three unrepresentable in that row, and ``source_reference`` is
    the exact reference in every case, because "they replied" with nothing
    behind it is the assumption ADR-0090 retired ``STALE`` for.

    **Acknowledgement, partial and substantive are different observations**, so
    several rows may answer one request: an acknowledgement on Monday and the
    substance on Friday are two things that happened, and collapsing them would
    lose the first or misdescribe the second.  ``completeness`` says which this
    one is; it is not a status the row later moves between.

    Append-only and corrected the way the request is: a mistaken observation is
    superseded by a corrected one naming it, and the original stays.
    """

    __tablename__ = "outgoing_request_responses"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_outgoing_request_responses_row"
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_outgoing_request_responses_key",
        ),
        ForeignKeyConstraint(
            ["project_id", "request_id"],
            ["outgoing_requests.project_id", "outgoing_requests.id"],
            name="fk_outgoing_request_responses_request",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_outgoing_request_responses_document",
        ),
        ForeignKeyConstraint(
            ["source_delivery_id", "project_id"],
            ["source_deliveries.id", "source_deliveries.project_id"],
            name="fk_outgoing_request_responses_delivery",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_segment_id"],
            ["source_segments.project_id", "source_segments.id"],
            name="fk_outgoing_request_responses_segment",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_response_id"],
            [
                "outgoing_request_responses.project_id",
                "outgoing_request_responses.id",
            ],
            name="fk_outgoing_request_responses_supersedes",
        ),
        CheckConstraint(
            "length(btrim(recorded_by_principal)) > 0",
            name="ck_outgoing_request_responses_principal",
        ),
        CheckConstraint(
            "length(btrim(source_reference)) > 0",
            name="ck_outgoing_request_responses_reference",
        ),
        CheckConstraint(
            "completeness in ('acknowledgement', 'partial', 'substantive')",
            name="ck_outgoing_request_responses_completeness",
        ),
        CheckConstraint(
            "evidence_kind in ('document', 'source_delivery', 'source_segment', "
            "'manual_observation')",
            name="ck_outgoing_request_responses_evidence_kind",
        ),
        # The four kinds, made unrepresentable in each other's shape. A row
        # that names a document and a telephone call is not a row this schema
        # can hold, which is what stops "linked to its evidence" from becoming
        # "has an evidence column somebody filled in".
        CheckConstraint(
            "(evidence_kind = 'document' and document_id is not null"
            " and source_delivery_id is null and source_segment_id is null"
            " and observation is null and observed_by_principal is null)"
            " or (evidence_kind = 'source_delivery' and source_delivery_id is not null"
            " and document_id is null and source_segment_id is null"
            " and observation is null and observed_by_principal is null)"
            " or (evidence_kind = 'source_segment' and source_segment_id is not null"
            " and document_id is null and source_delivery_id is null"
            " and observation is null and observed_by_principal is null)"
            " or (evidence_kind = 'manual_observation' and observation is not null"
            " and observed_by_principal is not null"
            " and document_id is null and source_delivery_id is null"
            " and source_segment_id is null)",
            name="ck_outgoing_request_responses_evidence",
        ),
        CheckConstraint(
            "observation is null or length(btrim(observation)) > 0",
            name="ck_outgoing_request_responses_observation",
        ),
        CheckConstraint(
            "observed_by_principal is null "
            "or length(btrim(observed_by_principal)) > 0",
            name="ck_outgoing_request_responses_observer",
        ),
        CheckConstraint(
            "(supersedes_response_id is null) = (correction_reason is null)",
            name="ck_outgoing_request_responses_correction",
        ),
        Index(
            "uq_outgoing_request_responses_correction",
            "project_id",
            "supersedes_response_id",
            unique=True,
            postgresql_where=text("supersedes_response_id is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    request_id: Mapped[int] = mapped_column(BigInteger, index=True)
    received_on: Mapped[date] = mapped_column(Date)
    completeness: Mapped[str] = mapped_column(String(32))
    evidence_kind: Mapped[str] = mapped_column(String(32))
    document_id: Mapped[int | None] = mapped_column(BigInteger)
    source_delivery_id: Mapped[int | None] = mapped_column(BigInteger)
    source_segment_id: Mapped[int | None] = mapped_column(BigInteger)
    observation: Mapped[str | None] = mapped_column(Text)
    observed_by_principal: Mapped[str | None] = mapped_column(String(128))
    source_reference: Mapped[str] = mapped_column(String(255))
    recorded_by_principal: Mapped[str] = mapped_column(String(128))
    supersedes_response_id: Mapped[int | None] = mapped_column(BigInteger)
    correction_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaReviewPacketReceipt(Base):
    """The one receipt for one guided Review Packet act (#526, ADR-0085).

    A Review Packet is a derived presentation and never an authoritative
    record (ADR-0085), so nothing here stores a packet's membership as state
    a later reading must reconcile.  What this row preserves is the *act*: the
    grouping rule and key the coordinator was shown, the human principal, the
    accepted revision they had read, the optional one Project Record revision
    the act produced, and — through ``delta_review_packet_children`` — the
    exact ordered child set with one outcome and one decision, plan, or
    deferral identity each.

    ``revision_id`` is null exactly when every child was a dated Defer: a
    scheduling-only act writes no Project Record revision (ADR-0084,
    ADR-0085).
    """

    __tablename__ = "delta_review_packet_receipts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_delta_review_packet_receipts_project_id"
        ),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_delta_review_packet_receipts_key"
        ),
        CheckConstraint(
            f"grouping_key_kind in ({_PACKET_GROUPING_KEY_KINDS_SQL})",
            name="ck_delta_review_packet_receipts_key_kind",
        ),
        CheckConstraint(
            "length(btrim(grouping_rule_version)) > 0",
            name="ck_delta_review_packet_receipts_rule_version",
        ),
        CheckConstraint(
            "length(btrim(decided_by_principal)) > 0",
            name="ck_delta_review_packet_receipts_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id"), index=True
    )
    grouping_rule_version: Mapped[str] = mapped_column(String(64))
    grouping_key_kind: Mapped[str] = mapped_column(String(32))
    grouping_key: Mapped[str] = mapped_column(String(255))
    decided_by_principal: Mapped[str] = mapped_column(String(128))
    observed_accepted_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaReviewPacketChild(Base):
    """One child delta of one packet act, with its own retained identity (#526).

    ADR-0035 forbids one Save collapsing the identity of the distinct domain
    acts inside it, so the receipt does not summarize its children: each row
    names the exact Proposed Delta, the position it was shown in, the outcome
    the coordinator chose, and the one identity that outcome produced.
    """

    __tablename__ = "delta_review_packet_children"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id", "ordinal", name="uq_delta_review_packet_children_ordinal"
        ),
        UniqueConstraint(
            "receipt_id", "delta_id", name="uq_delta_review_packet_children_delta"
        ),
        ForeignKeyConstraint(
            ["project_id", "receipt_id"],
            [
                "delta_review_packet_receipts.project_id",
                "delta_review_packet_receipts.id",
            ],
            name="fk_delta_review_packet_children_receipt",
        ),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_review_packet_children_delta",
        ),
        ForeignKeyConstraint(
            ["project_id", "decision_id"],
            ["delta_record_decisions.project_id", "delta_record_decisions.id"],
            name="fk_delta_review_packet_children_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "follow_up_plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_delta_review_packet_children_plan",
        ),
        CheckConstraint(
            f"outcome in ({_PACKET_CHILD_OUTCOMES_SQL})",
            name="ck_delta_review_packet_children_outcome",
        ),
        CheckConstraint("ordinal > 0", name="ck_delta_review_packet_children_ordinal"),
        # One outcome, one identity: a semantic child names its Human Record
        # Decision, a Needs coordination child its Follow-up Plan, and a dated
        # Defer its scheduling receipt. No child names two, and none names none.
        CheckConstraint(
            "(case when decision_id is null then 0 else 1 end) "
            "+ (case when follow_up_plan_id is null then 0 else 1 end) "
            "+ (case when deferral_id is null then 0 else 1 end) = 1",
            name="ck_delta_review_packet_children_one_identity",
        ),
        CheckConstraint(
            "(outcome in ('apply', 'keep_current', 'edit_and_apply')) "
            "= (decision_id is not null)",
            name="ck_delta_review_packet_children_semantic",
        ),
        CheckConstraint(
            "(outcome = 'needs_coordination') = (follow_up_plan_id is not null)",
            name="ck_delta_review_packet_children_coordination",
        ),
        CheckConstraint(
            "(outcome = 'defer') = (deferral_id is not null)",
            name="ck_delta_review_packet_children_defer",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    outcome: Mapped[str] = mapped_column(String(32))
    observed_source_revision: Mapped[str] = mapped_column(String(128))
    decision_id: Mapped[int | None] = mapped_column(BigInteger)
    follow_up_plan_id: Mapped[int | None] = mapped_column(BigInteger)
    deferral_id: Mapped[int | None] = mapped_column(
        ForeignKey("delta_deferrals.id"), unique=True
    )


class DeltaReviewPacketSupport(Base):
    """One effective Support Assessment the whole packet act relied on (#526)."""

    __tablename__ = "delta_review_packet_supports"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            "support_assessment_id",
            name="uq_delta_review_packet_supports_member",
        ),
        UniqueConstraint(
            "receipt_id", "ordinal", name="uq_delta_review_packet_supports_ordinal"
        ),
        ForeignKeyConstraint(
            ["project_id", "receipt_id"],
            [
                "delta_review_packet_receipts.project_id",
                "delta_review_packet_receipts.id",
            ],
            name="fk_delta_review_packet_supports_receipt",
        ),
        ForeignKeyConstraint(
            ["project_id", "support_assessment_id"],
            ["support_assessments.project_id", "support_assessments.id"],
            name="fk_delta_review_packet_supports_assessment",
        ),
        CheckConstraint("ordinal > 0", name="ck_delta_review_packet_supports_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger)
    support_assessment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


class DeltaReviewPacketReversal(Base):
    """The compensating act for one guided packet Save (#526, ADR-0035).

    Undo never deletes and never cascades into later work.  It appends one
    compensating Project Record revision that restores each predecessor
    accepted decision the packet superseded, and names the receipt it
    compensates so the original act, its children, and its history all stay
    exactly as they were recorded.  A packet whose every child was a dated
    Defer has no revision to compensate; reversing it only releases those
    scheduling receipts, so ``revision_id`` is null.
    """

    __tablename__ = "delta_review_packet_reversals"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id", name="uq_delta_review_packet_reversals_receipt"
        ),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_delta_review_packet_reversals_key"
        ),
        ForeignKeyConstraint(
            ["project_id", "receipt_id"],
            [
                "delta_review_packet_receipts.project_id",
                "delta_review_packet_receipts.id",
            ],
            name="fk_delta_review_packet_reversals_receipt",
        ),
        CheckConstraint(
            "length(btrim(reversed_by_principal)) > 0",
            name="ck_delta_review_packet_reversals_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger)
    revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    reversed_by_principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    reversed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CaptureCorrectionRequest(Base):
    """One attributable report that a named capture is wrong about its source (#836).

    ADR-0100's ancillary action. It changes nothing in the accepted record and
    resolves no Proposed Delta, which is why it is a relation of its own rather
    than a column on either.

    The binding is the composite foreign keys, not the column names.
    ``fact_id`` is reached through ``(project_id, document_id, fact_id)``, so
    ``document_id`` is the capture's own document by construction; both segment
    columns are then reached through that same ``document_id``, so a selected
    passage from another file -- or another customer's -- is unrepresentable
    rather than merely refused in Python. ``fact_content_sha256`` is the
    capture's own identity digest, recorded so a reader can prove the id still
    names the capture that was challenged.

    ``source_segment_id`` is where the capture said it read the value and
    ``selected_source_segment_id`` is where the coordinator says it should have
    been read; they are different columns because reading the wrong cell is one
    of the defects reported here.

    Written only by ``report_capture_correction``, the record-decision role's
    command; a guard trigger refuses every other write, and a mistaken report is
    corrected by making another one.
    """

    __tablename__ = "capture_correction_requests"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_capture_correction_requests_project_id"
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_capture_correction_requests_key",
        ),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_capture_correction_requests_delta",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "fact_id"],
            ["facts.project_id", "facts.document_id", "facts.id"],
            name="fk_capture_correction_requests_fact",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_capture_correction_requests_cited_passage",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "selected_source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_capture_correction_requests_selected_passage",
        ),
        CheckConstraint(
            "fact_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_capture_correction_requests_digest",
        ),
        CheckConstraint(
            "length(btrim(expected_interpretation)) > 0 "
            "and length(expected_interpretation) <= 2000",
            name="ck_capture_correction_requests_interpretation",
        ),
        CheckConstraint(
            "length(btrim(reported_by_principal)) > 0",
            name="ck_capture_correction_requests_principal",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_capture_correction_requests_key_text",
        ),
        Index(
            "ix_capture_correction_requests_delta_id", "project_id", "delta_id"
        ),
        Index("ix_capture_correction_requests_fact_id", "project_id", "fact_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    delta_id: Mapped[int] = mapped_column(BigInteger)
    document_id: Mapped[int] = mapped_column(BigInteger)
    fact_id: Mapped[int] = mapped_column(BigInteger)
    fact_content_sha256: Mapped[str] = mapped_column(String(64))
    source_segment_id: Mapped[int | None] = mapped_column(BigInteger)
    selected_source_segment_id: Mapped[int] = mapped_column(BigInteger)
    expected_interpretation: Mapped[str] = mapped_column(Text)
    reported_by_principal: Mapped[str] = mapped_column(String(128))
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- ADR-0101's correction lifecycle (#836, #842) --------------------------

#: What one source-grounded correction investigation concluded. Spelled here
#: beside the relation so the Python and the command's own check constraint
#: cannot name different sets.
CAPTURE_CORRECTION_OUTCOMES = ("no_change", "still_differs", "inconclusive")


class CaptureCorrectionResult(Base):
    """What one source-grounded correction established, with its whole proof (#842).

    ADR-0101 is explicit that a corrected Source Fact alone does not prove "no
    difference": that conclusion also depends on which accepted revision was
    read and which comparison rule was applied. So this row carries the
    complete proof rather than a pair of foreign keys -- the request that
    caused the investigation, the exact challenged capture by immutable
    identity and digest, the corrected capture with the Support Assessment
    holding it to the retained source, the accepted revision, the comparison
    rule version, the conclusion, the replacement proposal where there is one,
    the responsible operations actor beside the identity that executed the
    work, and an idempotency identity.

    ``outcome`` is one of ``CAPTURE_CORRECTION_OUTCOMES``. ``inconclusive`` is
    a result and not a failure to record: ADR-0101 forbids claiming a
    successful correction from missing or ambiguous evidence, so that outcome
    carries no corrected capture, no accepted revision and no replacement, and
    retires nothing.

    Written only by ``record_capture_correction_result``, the record-decision
    role's command; a guard trigger refuses every other write.
    """

    __tablename__ = "capture_correction_results"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_capture_correction_results_project_id"
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_capture_correction_results_key",
        ),
        ForeignKeyConstraint(
            ["project_id", "request_id"],
            [
                "capture_correction_requests.project_id",
                "capture_correction_requests.id",
            ],
            name="fk_capture_correction_results_request",
        ),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_capture_correction_results_delta",
        ),
        ForeignKeyConstraint(
            ["project_id", "replacement_delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_capture_correction_results_replacement",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "challenged_fact_id"],
            ["facts.project_id", "facts.document_id", "facts.id"],
            name="fk_capture_correction_results_challenged_capture",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "corrected_fact_id"],
            ["facts.project_id", "facts.document_id", "facts.id"],
            name="fk_capture_correction_results_corrected_capture",
        ),
        ForeignKeyConstraint(
            ["project_id", "corrected_support_assessment_id"],
            ["support_assessments.project_id", "support_assessments.id"],
            name="fk_capture_correction_results_support",
        ),
        CheckConstraint(
            "challenged_fact_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_capture_correction_results_challenged_digest",
        ),
        CheckConstraint(
            "outcome in ('no_change', 'still_differs', 'inconclusive')",
            name="ck_capture_correction_results_outcome",
        ),
        CheckConstraint(
            "(outcome = 'no_change'"
            " and corrected_fact_id is not null"
            " and corrected_support_assessment_id is not null"
            " and accepted_revision_id is not null"
            " and replacement_delta_id is null)"
            " or (outcome = 'still_differs'"
            " and corrected_fact_id is not null"
            " and corrected_support_assessment_id is not null"
            " and replacement_delta_id is not null)"
            " or (outcome = 'inconclusive'"
            " and corrected_fact_id is null"
            " and corrected_support_assessment_id is null"
            " and accepted_revision_id is null"
            " and replacement_delta_id is null)",
            name="ck_capture_correction_results_outcome_shape",
        ),
        CheckConstraint(
            "length(btrim(comparison_rule_version)) > 0",
            name="ck_capture_correction_results_rule",
        ),
        CheckConstraint(
            "length(btrim(finding)) > 0 and length(finding) <= 2000",
            name="ck_capture_correction_results_finding",
        ),
        CheckConstraint(
            "length(btrim(authorized_by_principal)) > 0",
            name="ck_capture_correction_results_authorized_by",
        ),
        CheckConstraint(
            "length(btrim(executed_by)) > 0",
            name="ck_capture_correction_results_executed_by",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_capture_correction_results_key_text",
        ),
        Index(
            "ix_capture_correction_results_delta_id", "project_id", "delta_id"
        ),
        Index(
            "ix_capture_correction_results_request_id", "project_id", "request_id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    request_id: Mapped[int] = mapped_column(BigInteger)
    delta_id: Mapped[int] = mapped_column(BigInteger)
    document_id: Mapped[int] = mapped_column(BigInteger)
    challenged_fact_id: Mapped[int] = mapped_column(BigInteger)
    challenged_fact_sha256: Mapped[str] = mapped_column(String(64))
    corrected_fact_id: Mapped[int | None] = mapped_column(BigInteger)
    corrected_support_assessment_id: Mapped[int | None] = mapped_column(BigInteger)
    accepted_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    comparison_rule_version: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    replacement_delta_id: Mapped[int | None] = mapped_column(BigInteger)
    finding: Mapped[str] = mapped_column(Text)
    authorized_by_principal: Mapped[str] = mapped_column(String(128))
    executed_by: Mapped[str] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    written_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))


class DeltaCaptureCorrection(Base):
    """This proposal is no longer an actionable comparison; its capture was corrected.

    ADR-0101's ``DeltaCaptureCorrection``, and the whole of its assertion. It
    is not a decision about what the record should show, not a claim that a
    coordinator concluded anything, and not a newer source version arriving --
    which is why it is neither a ``DeltaDisposition`` nor a
    ``DeltaSupersession``, both of which would have been false entries.

    One row per Proposed Delta, held by ``uq_delta_capture_corrections_delta``:
    that is what makes an exact retry the same act rather than a second one,
    and it is the index two competing retirements serialise on. The row is
    append-only; a retirement recorded in error is answered by the
    recomparison that follows a further corrected capture, not by an edit.

    Standing stays derived, so this is read the way the other three terminal
    relationships are read: a row exists, or it does not.
    """

    __tablename__ = "delta_capture_corrections"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_delta_capture_corrections_project_id"
        ),
        UniqueConstraint("delta_id", name="uq_delta_capture_corrections_delta"),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_capture_corrections_delta",
        ),
        ForeignKeyConstraint(
            ["project_id", "result_id"],
            [
                "capture_correction_results.project_id",
                "capture_correction_results.id",
            ],
            name="fk_delta_capture_corrections_result",
        ),
        Index(
            "ix_delta_capture_corrections_result_id", "project_id", "result_id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    delta_id: Mapped[int] = mapped_column(BigInteger)
    result_id: Mapped[int] = mapped_column(BigInteger)
    retired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
