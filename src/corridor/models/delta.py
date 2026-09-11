"""Proposed Deltas and their resolution: the difference, then the decision.

A Proposed Delta is a typed difference from the accepted record, and the record
is unchanged while it stays open. Resolve Delta turns one into a single atomic
Project Record Revision. The dispositions, deferrals, supersessions and Review
Packet children are all here because they are the same transaction's parts: an
earlier design resolved deltas one row at a time and could leave a packet half
applied, which #526 closed by making the packet one transaction.
"""

from datetime import date, datetime
from hashlib import sha256
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
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
    "CLOSURE_KINDS",
    "OutgoingRequest",
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
    """A retained outgoing request, and the response boundary it declared (#652).

    The chase list's ``unanswered_request`` band is the one place Corridor may
    say a specific thing has gone unanswered, and ADR-0090 retired the legacy
    ``STALE`` alert precisely because silence is not evidence: nobody sending a
    document does not mean anybody failed to answer.  A no-response fact
    therefore needs a *retained request* behind it, and until this table existed
    ``follow_up_bundles.read_retained_outgoing_requests`` truthfully returned
    nothing.  This is that record: what was asked, of which External
    Organization, covering which Utility Conflicts, its exact sent bytes or a
    digest of them, the declared expected-response boundary (silence before it
    is not a finding), the attributable sender and day, and the Follow-up Plan
    the request advances.

    It is Corridor-originated correspondence, not source-derived evidence and
    not an accepted-record decision, so it does not join the spine's append
    matrix.  It is written only through ``append_outgoing_request`` under the
    record-decision role, and it is append-only: a guard trigger refuses every
    update, delete, and truncate, and refuses an insert that does not arrive as
    that role, so not even the schema owner can write one raw (#492 idiom).

    ``sent_bytes`` is the exact request when Corridor kept it and null when it
    did not — whoever sent it, by whatever means, may not have retained the
    bytes — so ``content_sha256`` is the digest that stands on either footing,
    the same choice ``SourceDelivery`` makes for a delivery whose bytes were
    never kept.  ``expected_response_by`` is the resolved boundary the band
    reads; ``boundary_rule_version`` and ``boundary_interval_days`` record how
    it was derived when an interval and a rule produced it rather than a date
    stated outright.
    """

    __tablename__ = "outgoing_requests"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_outgoing_requests_project_id"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_outgoing_requests_key"
        ),
        ForeignKeyConstraint(
            ["project_id", "follow_up_plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_outgoing_requests_plan",
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
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_outgoing_requests_digest"
        ),
        CheckConstraint(
            "expected_response_by >= sent_on", name="ck_outgoing_requests_boundary"
        ),
        CheckConstraint(
            "jsonb_typeof(covered_subject_keys) = 'array'",
            name="ck_outgoing_requests_subjects",
        ),
        CheckConstraint(
            "sent_bytes is null or octet_length(sent_bytes) > 0",
            name="ck_outgoing_requests_bytes",
        ),
        CheckConstraint(
            "boundary_interval_days is null or boundary_interval_days >= 0",
            name="ck_outgoing_requests_interval",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    follow_up_plan_id: Mapped[int] = mapped_column(BigInteger, index=True)
    external_organization: Mapped[str] = mapped_column(String(255))
    responsible_role: Mapped[str | None] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text)
    covered_subject_keys: Mapped[Any] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    sent_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    sent_on: Mapped[date] = mapped_column(Date)
    sent_by_principal: Mapped[str] = mapped_column(String(128))
    expected_response_by: Mapped[date] = mapped_column(Date)
    boundary_rule_version: Mapped[str | None] = mapped_column(String(64))
    boundary_interval_days: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def digest_is_valid(self) -> bool:
        """Whether retained bytes still match the digest; true when none were kept."""
        if self.sent_bytes is None:
            return True
        return sha256(self.sent_bytes).hexdigest() == self.content_sha256


class OutgoingRequestResponse(Base):
    """A received response that stops one retained request's silence clock (#652).

    Recording that a reply arrived, at least enough to stop the no-response
    clock: the band fires only for a retained request whose boundary has passed
    *and* which has no recorded response as of the reading's cutoff.  Full
    receipt and delivery tracking is deliberately out of scope (#652); this row
    exists to make "they answered" a fact the chase list can read.  It is
    append-only and written only through ``append_outgoing_request_response``
    under the record-decision role, held by the same guard trigger as the
    request it answers.  One response per request stops the clock; the
    command converges a replay on the row it already wrote.
    """

    __tablename__ = "outgoing_request_responses"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "request_id", name="uq_outgoing_request_responses_request"
        ),
        ForeignKeyConstraint(
            ["project_id", "request_id"],
            ["outgoing_requests.project_id", "outgoing_requests.id"],
            name="fk_outgoing_request_responses_request",
        ),
        CheckConstraint(
            "length(btrim(recorded_by_principal)) > 0",
            name="ck_outgoing_request_responses_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    request_id: Mapped[int] = mapped_column(BigInteger, index=True)
    received_on: Mapped[date] = mapped_column(Date)
    recorded_by_principal: Mapped[str] = mapped_column(String(128))
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
