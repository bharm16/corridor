"""Released policy: what is approved, what it ran on, and what it admitted.

One approvals table and one runs table, per ADR-0028, rather than a pair per
policy family -- five near-identical pairs were what this replaced. The four
activation kinds are single-table inheritance over one ``policy_activations``
relation for the same reason. The admission outcomes and inclusion requests
beneath them are the ADR-0029-style automatic Record Inclusion the legacy path
still performs, recorded so that a mechanical admission is auditable.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base


__all__ = [
    "DependencyAdmissionOutcome",
    "EventAdmissionAcceptanceReceipt",
    "EventAdmissionActivation",
    "EventAdmissionOutcome",
    "OrganizationIdentityActivation",
    "POLICY_FAMILIES",
    "PolicyActivation",
    "PolicyApproval",
    "PolicyRun",
    "RecordInclusionRequest",
    "RevisionReconciliationRequest",
    "ScheduleLinkActivation",
    "UnreadableCellAdmissionActivation",
]


# The three families that write records or move support under an
# authorized policy (ADRs 0022, 0026, 0027). ADR-0028 joined their
# approval and run tables — the shapes were identical, and copies drift —
# while each family keeps its own outcome table, whose shape is the
# receipt.
POLICY_FAMILIES = (
    "automatic-carry-forward",
    "event-admission",
    "dependency-admission",
)

_POLICY_FAMILY_CHECK = (
    "family in ('automatic-carry-forward', 'event-admission', 'dependency-admission')"
)


class PolicyApproval(Base):
    """One immutable human authorization of a policy family's rules.

    ADR-0028: one table for every family's signatures, each row naming
    its family. The family-carrying unique keys are what let each outcome
    table keep its "my outcomes point only at my runs" rule in the
    schema rather than in code review.
    """

    __tablename__ = "policy_approvals"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_policy_approvals_project_id"),
        UniqueConstraint("family", "id", name="uq_policy_approvals_family_id"),
        UniqueConstraint(
            "project_id",
            "family",
            "id",
            name="uq_policy_approvals_project_family_id",
        ),
        CheckConstraint(_POLICY_FAMILY_CHECK, name="ck_policy_approvals_family"),
        CheckConstraint(
            "jsonb_typeof(policy_json) = 'object'",
            name="ck_policy_approvals_object",
        ),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_policy_approvals_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    family: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(Text)
    policy_json: Mapped[dict] = mapped_column(JSONB)
    policy_sha256: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PolicyRun(Base):
    """One immutable batch receipt for a policy pass.

    `applied_count` is the neutral name for what a family applied —
    carried support, admitted events, admitted dependencies. A deferred
    database trigger reconciles both counts against the family's own
    outcome table at commit, for every family: the check Carry-Forward
    alone used to carry (ADR-0028).
    """

    __tablename__ = "policy_runs"
    __table_args__ = (
        UniqueConstraint("family", "id", name="uq_policy_runs_family_id"),
        UniqueConstraint(
            "project_id",
            "family",
            "id",
            name="uq_policy_runs_project_family_id",
        ),
        CheckConstraint(_POLICY_FAMILY_CHECK, name="ck_policy_runs_family"),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_policy_runs_sha256",
        ),
        CheckConstraint(
            "applied_count >= 0 and abstained_count >= 0",
            name="ck_policy_runs_counts",
        ),
        ForeignKeyConstraint(
            ["project_id", "family", "policy_approval_id"],
            [
                "policy_approvals.project_id",
                "policy_approvals.family",
                "policy_approvals.id",
            ],
            name="fk_policy_runs_approval_project_family",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(String(32))
    # Null for normal Corridor-managed policies. Historical project-approved
    # Carry-Forward runs retain the approval they originally named.
    policy_approval_id: Mapped[int | None] = mapped_column(BigInteger)
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    abstention_reason_version: Mapped[str] = mapped_column(String(64))
    applied_count: Mapped[int] = mapped_column(Integer)
    abstained_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventAdmissionOutcome(Base):
    """One immutable event outcome within an event-admission receipt.

    An admitted outcome names the DependencyEvent it created; an
    abstention names the check that failed, under a stated reason
    vocabulary. Neither carries a model's opinion — no model verdict can
    appear in an admission path (ADR-0026).
    """

    __tablename__ = "event_admission_outcomes"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('admitted', 'abstained')",
            name="ck_event_admission_outcome_value",
        ),
        CheckConstraint(
            "("
            "outcome = 'admitted' and reason is null "
            "and dependency_event_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and dependency_event_id is null"
            ")",
            name="ck_event_admission_outcome_kind",
        ),
        # The family rule stays in the schema (ADR-0028): this outcome
        # can only ever point at an event-admission run.
        CheckConstraint(
            "family = 'event-admission'",
            name="ck_event_admission_outcome_family",
        ),
        ForeignKeyConstraint(
            ["family", "policy_run_id"],
            ["policy_runs.family", "policy_runs.id"],
            name="fk_event_admission_outcome_run_family",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    policy_run_id: Mapped[int] = mapped_column(BigInteger, index=True)
    family: Mapped[str] = mapped_column(String(32), server_default="event-admission")
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    outcome: Mapped[str] = mapped_column(String(9))
    reason: Mapped[str | None] = mapped_column(String(64))
    dependency_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_events.id")
    )
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    scope_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_event_scope_decisions.id")
    )
    candidate_disposition_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_dispositions.id")
    )
    audit_log_id: Mapped[int | None] = mapped_column(ForeignKey("audit_log.id"))
    eligibility_json: Mapped[dict | None] = mapped_column(JSONB)
    eligibility_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventAdmissionAcceptanceReceipt(Base):
    """Immutable real-state proof for one Event Admission activation attempt."""

    __tablename__ = "event_admission_acceptance_receipts"
    __table_args__ = (
        CheckConstraint(
            "status in ('passed', 'failed')",
            name="ck_event_admission_acceptance_status",
        ),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_event_admission_acceptance_policy_sha256",
        ),
        CheckConstraint(
            "receipt_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_event_admission_acceptance_receipt_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    source_revision: Mapped[str] = mapped_column(String(64))
    migration_head: Mapped[str] = mapped_column(String(64))
    predecessor_policy_version: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    reason_version: Mapped[str] = mapped_column(String(64))
    selection_rule: Mapped[str] = mapped_column(String(128))
    receipt_json: Mapped[dict] = mapped_column(JSONB)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PolicyActivation(Base):
    """ADR-0050's one activation ledger, keyed by policy family and fingerprint.

    Four automatic Record Inclusion expansions each grew their own activation
    table with the same seven columns: the automatic location link rule, the
    corroborated unreadable-cell admission class, the whole-row organization
    identity tiers, and the unknown-scope Event Admission class. Nothing about
    ADR-0050's gate is per-family — the pass is void when the rule's own
    fingerprint changes, a human suspension outranks it, and only an
    attributable act lifts one — so the history is one relation with the family
    as a column, and each family is a typed view onto it (single-table
    inheritance on ``family``). A query written for one family cannot read
    another family's rows, and an operator screen reads every family at once.

    Two columns are per-family shape rather than optional data, and the check
    constraints say which is which:

    - ``policy_sha256`` is the digest of the canonical policy the pass stands
      on. Three families bind their pass to it. The Event Admission family
      binds a pass to an immutable acceptance receipt instead, so it carries no
      digest here and the digest families cannot omit one.
    - ``acceptance_receipt_id`` is that receipt, and only the Event Admission
      family has one.

    Rows are append-only: a suspension is a new row, never an edit, and a lift
    is an ``activate`` row under the lifting person's own subject.
    """

    __tablename__ = "policy_activations"
    __table_args__ = (
        CheckConstraint(
            "action in ('activate', 'suspend')",
            name="ck_policy_activation_action",
        ),
        CheckConstraint(
            "length(trim(family)) > 0",
            name="ck_policy_activation_family",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_policy_activation_reason",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_policy_activation_actor",
        ),
        # A digest family carries a well-formed digest; the receipt-bound family
        # carries none. A `case`, not two `or`ed clauses: a null digest makes
        # ``policy_sha256 ~ '...'`` null, and a check that evaluates to null
        # passes, so the two-clause form admitted exactly the row it refuses.
        CheckConstraint(
            "case when family = 'event_admission' "
            "then policy_sha256 is null "
            "else policy_sha256 is not null "
            "and policy_sha256 ~ '^[0-9a-f]{64}$' end",
            name="ck_policy_activation_sha256",
        ),
        # Only the receipt-bound family names a receipt, and it always does.
        CheckConstraint(
            "(family = 'event_admission') = (acceptance_receipt_id is not null)",
            name="ck_policy_activation_receipt",
        ),
        # A suspension proves nothing, so it never carries a case count. An
        # activation carries the count its replay compared, except in the
        # receipt-bound family, where the immutable acceptance receipt holds the
        # population and the ledger row points at it.
        CheckConstraint(
            "action <> 'suspend' or replay_case_count is null",
            name="ck_policy_activation_case_count",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(String(48))
    action: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str | None] = mapped_column(String(64))
    # How many recorded human decisions the replay compared against. Null for a
    # suspension, which needs no proof.
    replay_case_count: Mapped[int | None] = mapped_column(Integer)
    # The immutable clone-based acceptance receipt an Event Admission pass is
    # bound to. Null for every fingerprint-bound family.
    acceptance_receipt_id: Mapped[int | None] = mapped_column(
        ForeignKey("event_admission_acceptance_receipts.id")
    )
    reason: Mapped[str] = mapped_column(String(160))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __mapper_args__ = {
        "polymorphic_on": "family",
        # The base is never a stored family; it exists so a reader can ask one
        # question of every family at once.
        "polymorphic_identity": "policy_activation",
    }


class EventAdmissionActivation(PolicyActivation):
    """Append-only activation or suspension of one proved policy version."""

    __mapper_args__ = {"polymorphic_identity": "event_admission"}


class OrganizationIdentityActivation(PolicyActivation):
    """Append-only ADR-0050 gate for whole-row automatic identity tiers."""

    __mapper_args__ = {"polymorphic_identity": "organization_identity"}


class ScheduleLinkActivation(PolicyActivation):
    """Append-only activation or suspension of the automatic location link rule.

    The exact location-link rule is a new automatic matching class, so ADR-0050
    governs it: it may not auto-write until a regression replay of the project's
    own recorded human link decisions passes with at least one real case and no
    contradiction. A brand-new rule with no history has no passing replay and so
    never auto-links until a person has linked by hand.
    """

    __mapper_args__ = {"polymorphic_identity": "schedule_link"}


class UnreadableCellAdmissionActivation(PolicyActivation):
    """Append-only activation/suspension of corroborated cross-document admission.

    Admitting a corroborated cell value across documents expands automatic
    Record Inclusion behavior, so ADR-0050 governs it exactly as it governs the
    location-link rule. Shipped inactive — a project with no passing replay
    never auto-admits.
    """

    __mapper_args__ = {"polymorphic_identity": "unreadable_cell_admission"}


class RecordInclusionRequest(Base):
    """One durable, coalescing watermark of a project's pending Record Inclusion.

    A completed Extraction Run (and, later, an approved identity/fact change)
    leaves the record needing reconciliation, but ``load_project`` appends a
    PolicyRun on every call — so calling it on every idle tick would grow the
    receipt log without bound. This row is the handoff that makes reconciliation
    conditional and recoverable: a producer bumps ``dirty_seq`` inside its own
    transaction, so a rolled-back producer leaves no work and a committed one
    survives process exit. Reconciliation is pending exactly while
    ``dirty_seq > reconciled_seq``; many bumps between reconciliations coalesce
    into one pending pass. Unlike the append-only receipt tables, this is
    mutable operational state (like a due-work occurrence), so it carries no
    immutability trigger.
    """

    __tablename__ = "record_inclusion_requests"
    __table_args__ = (
        CheckConstraint(
            "dirty_seq >= 0 and reconciled_seq >= 0",
            name="ck_record_inclusion_requests_non_negative",
        ),
        CheckConstraint(
            "reconciled_seq <= dirty_seq",
            name="ck_record_inclusion_requests_watermark_order",
        ),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), primary_key=True
    )
    dirty_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reconciled_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    last_reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RevisionReconciliationRequest(Base):
    """One durable, coalescing watermark of a project's pending revision work.

    A committed structural change — a registered Supersession edge or a changed
    Current Production Run — makes a project's Document Revision Processing and
    Automatic Support Update possibly stale, but re-running that pass on every
    idle scheduled tick would create a Revision Comparison nobody asked for and
    append a Carry-Forward PolicyRun without bound. This row is the handoff that
    makes the pass conditional and recoverable, exactly like
    ``record_inclusion_requests``: a producer bumps ``dirty_seq`` inside its own
    transaction, so a rolled-back producer leaves no revision work and a
    committed one survives process exit. Reconciliation is pending exactly while
    ``dirty_seq > reconciled_seq``; many bumps between reconciliations coalesce
    into one pending pass. Like the Record Inclusion watermark this is mutable
    operational state, not an append-only receipt, so it carries no immutability
    trigger.
    """

    __tablename__ = "revision_reconciliation_requests"
    __table_args__ = (
        CheckConstraint(
            "dirty_seq >= 0 and reconciled_seq >= 0",
            name="ck_revision_reconciliation_requests_non_negative",
        ),
        CheckConstraint(
            "reconciled_seq <= dirty_seq",
            name="ck_revision_reconciliation_requests_watermark_order",
        ),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), primary_key=True
    )
    dirty_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reconciled_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    last_reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DependencyAdmissionOutcome(Base):
    """One immutable candidate outcome within an admission receipt.

    `admitted` names the Dependency the primary candidate became;
    `merged` names the Dependency an identical sibling corroborates;
    `abstained` names the check that failed.
    """

    __tablename__ = "dependency_admission_outcomes"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('admitted', 'merged', 'abstained')",
            name="ck_dependency_admission_outcome_value",
        ),
        CheckConstraint(
            "("
            "outcome in ('admitted', 'merged') and reason is null "
            "and dependency_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and dependency_id is null"
            ")",
            name="ck_dependency_admission_outcome_kind",
        ),
        # The family rule stays in the schema (ADR-0028): this outcome
        # can only ever point at a dependency-admission run.
        CheckConstraint(
            "family = 'dependency-admission'",
            name="ck_dependency_admission_outcome_family",
        ),
        CheckConstraint(
            "eligibility_sha256 is null or eligibility_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_dependency_admission_outcome_eligibility_sha256",
        ),
        CheckConstraint(
            "(outcome = 'abstained' and "
            "((eligibility_json is null and eligibility_sha256 is null) or "
            "(eligibility_json is not null and eligibility_sha256 is not null))) "
            "or (outcome in ('admitted', 'merged') and "
            "eligibility_json is null and eligibility_sha256 is null)",
            name="ck_dependency_admission_outcome_eligibility_shape",
        ),
        ForeignKeyConstraint(
            ["family", "policy_run_id"],
            ["policy_runs.family", "policy_runs.id"],
            name="fk_dependency_admission_outcome_run_family",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    policy_run_id: Mapped[int] = mapped_column(BigInteger, index=True)
    family: Mapped[str] = mapped_column(
        String(32), server_default="dependency-admission"
    )
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    outcome: Mapped[str] = mapped_column(String(9))
    reason: Mapped[str | None] = mapped_column(String(64))
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    eligibility_json: Mapped[dict | None] = mapped_column(JSONB)
    eligibility_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
