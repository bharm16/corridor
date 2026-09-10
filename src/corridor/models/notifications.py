"""Notifications: one row for what was owed, then dispatch and attempt rows.

Three families -- new assignment (#351), due action, and new document -- each
split into the notification, the dispatch that addressed it to one recipient,
and the attempts that dispatch made. They are not one table because the
addressing rules differ per family and a single table needed nullable columns
that no constraint could hold; they share the split because a delivery failure
must not lose the fact that a person was owed the notice.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base


__all__ = [
    "ASSIGNMENT_DELIVERY_STATES",
    "ASSIGNMENT_NOTIFICATION_SUBJECT_KINDS",
    "AssignmentNotification",
    "AssignmentNotificationAttempt",
    "AssignmentNotificationDispatch",
    "AssignmentNotificationFeedback",
    "DocumentNotification",
    "DocumentNotificationAttempt",
    "DocumentNotificationDispatch",
    "DueActionNotification",
    "DueActionNotificationAttempt",
    "DueActionNotificationDispatch",
    "NEW_ASSIGNMENT_NOTIFICATION_CATEGORY",
]


# --- New-assignment notifications (#351) ---------------------------------
#
# A committed new roster-backed assignment produces exactly one immutable
# notification occurrence bound to its exact subject, assignment decision, and
# selected roster identity.  The occurrence is registered in the same
# transaction as the assignment, so a rolled-back save leaves no notification
# and a crash after commit cannot lose it (ADR-0032).  Delivery rides the one
# supervised Due Work runtime (#332) through a server-owned handler; the
# occurrence and its dispatch are the durable domain record, never a second
# scheduler.  Only the ``new_assignment`` interruption category exists here;
# reminders, escalation, and change notices are separately scoped successors.

# The one category this slice emits.  Kept as a check-constrained value rather
# than a free string so a later category cannot silently ride this table.
NEW_ASSIGNMENT_NOTIFICATION_CATEGORY = "new_assignment"
ASSIGNMENT_NOTIFICATION_SUBJECT_KINDS = ("constraint", "statement")
ASSIGNMENT_DELIVERY_STATES = (
    "queued",
    "completed",
    "retry_due",
    "failed",
    "uncertain",
)


class AssignmentNotification(Base):
    """One immutable new-assignment notification occurrence (#351, ADR-0032).

    Bound to the exact Coordination Subject, the exact assignment Work Decision
    that made the person accountable, and the selected roster identity.  The
    ``occurrence_key`` fingerprint makes repeated triggers and competing writers
    converge on one row; the occurrence never gates ownership, which takes
    effect on the assignment's own commit regardless of any delivery outcome.
    """

    __tablename__ = "assignment_notifications"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_assignment_notification_key"),
        CheckConstraint(
            "category = 'new_assignment'",
            name="ck_assignment_notification_category",
        ),
        CheckConstraint(
            "subject_kind in ('constraint', 'statement')",
            name="ck_assignment_notification_subject_kind",
        ),
        CheckConstraint(
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)",
            name="ck_assignment_notification_subject_shape",
        ),
        CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_assignment_notification_key_hex",
        ),
        CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_assignment_notification_actor",
        ),
        CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_assignment_notification_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    subject_kind: Mapped[str] = mapped_column(String(16))
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    assignment_decision_id: Mapped[int] = mapped_column(
        ForeignKey("work_decisions.id"), index=True
    )
    recipient_roster_entry_id: Mapped[int] = mapped_column(
        ForeignKey("project_roster_entries.id")
    )
    recipient_principal_subject: Mapped[str] = mapped_column(String(128))
    occurrence_key: Mapped[str] = mapped_column(String(64))
    registered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AssignmentNotificationDispatch(Base):
    """Mutable delivery standing for one notification occurrence (#351).

    The occurrence is immutable; this row carries what the shared runtime and
    the recipient inbox read: the queued / completed / retry-due / failed /
    uncertain state, the resolved verified contact (or a visible delivery
    limitation when the roster identity has no typed contact), retained provider
    result and idempotency evidence, and bounded retry state.  Its identity is
    immutable; only the delivery standing changes.
    """

    __tablename__ = "assignment_notification_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", name="uq_assignment_dispatch_notification"
        ),
        CheckConstraint("channel = 'email'", name="ck_assignment_dispatch_channel"),
        CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_assignment_dispatch_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_assignment_dispatch_attempt_count"
        ),
        CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_assignment_dispatch_retry_shape",
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_assignment_dispatch_idempotency_hex",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("assignment_notifications.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    delivery_state: Mapped[str] = mapped_column(String(16))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AssignmentNotificationAttempt(Base):
    """Append-only record of one notification delivery attempt (#351, ADR-0032).

    Every sweep of a dispatch appends one row: what was attempted, the retained
    provider result and idempotency evidence, and the explicit outcome —
    including an ``uncertain`` outcome when an acknowledgment is unavailable and
    a ``skipped`` outcome when a re-checked assignment is no longer current or a
    typed contact could not be resolved.  Nothing here is ever mutated.
    """

    __tablename__ = "assignment_notification_attempts"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_assignment_attempt_number"
        ),
        CheckConstraint(
            "outcome in "
            "('completed', 'retry_due', 'failed', 'uncertain', 'skipped')",
            name="ck_assignment_attempt_outcome",
        ),
        CheckConstraint(
            "attempt_number > 0", name="ck_assignment_attempt_positive"
        ),
        CheckConstraint(
            "length(trim(runtime_owner)) > 0",
            name="ck_assignment_attempt_owner",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("assignment_notification_dispatches.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    runtime_owner: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AssignmentNotificationFeedback(Base):
    """Append-only attributable feedback that an assignment looks incorrect (#351).

    The assigned person can flag a notification's assignment through this path.
    It preserves the existing assignment and its Work Decision history until an
    authorized person changes it — flagging records a signed marker, never a
    mutation of the assignment (ADR-0035).
    """

    __tablename__ = "assignment_notification_feedback"
    __table_args__ = (
        UniqueConstraint(
            "notification_id",
            "flagged_by",
            name="uq_assignment_feedback_person",
        ),
        CheckConstraint(
            "feedback_kind = 'incorrect_assignment'",
            name="ck_assignment_feedback_kind",
        ),
        CheckConstraint(
            "length(trim(flagged_by)) > 0",
            name="ck_assignment_feedback_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("assignment_notifications.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    flagged_by: Mapped[str] = mapped_column(String(128))
    feedback_kind: Mapped[str] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text)
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DueActionNotification(Base):
    """One immutable due-action notification occurrence (#352, ADR-0032/0038).

    This extends the #351 new-assignment occurrence to the three derived
    categories of ADR-0034 decisions 39/48: a soon-due or past-due Next Action
    reminder (``next_action_due``), an urgent-overdue escalation
    (``next_action_escalation``), and a non-interrupting per-recipient daily
    summary (``daily_summary``).  Unlike a new assignment, these conditions are
    *derived* on each supervised tick from the subject's current authoritative
    plan and the applicable existing check semantics, so the occurrence binds the
    exact subject, current Next Action Work Decision identity, applicable
    check/configuration identity, observation window, urgency band, and typed
    recipient role.  The ``occurrence_key`` fingerprint deliberately excludes the
    poll time, so an unchanged condition converges on one row rather than
    becoming a new event on every poll; a genuinely new condition (a new plan
    decision, a soon->overdue crossing, a new check configuration, or a new
    summary window) is a new occurrence.  A daily summary carries no subject.
    """

    __tablename__ = "due_action_notifications"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_due_action_notification_key"),
        CheckConstraint(
            "category in "
            "('next_action_due', 'next_action_escalation', 'daily_summary')",
            name="ck_due_action_notification_category",
        ),
        CheckConstraint(
            "recipient_role in ('assignee', 'escalation', 'summary')",
            name="ck_due_action_notification_role",
        ),
        CheckConstraint(
            "urgency is null or urgency in ('soon', 'overdue', 'urgent_overdue')",
            name="ck_due_action_notification_urgency",
        ),
        # The one shape rule that couples category to its bound fields.  A daily
        # summary names a recipient and a window but no subject, plan, urgency or
        # due date; a reminder or escalation names exactly one subject, its
        # current Next Action decision, and an urgency band.
        CheckConstraint(
            "("
            "category = 'daily_summary' and subject_kind is null "
            "and dependency_id is null and commitment_lineage_id is null "
            "and plan_decision_id is null and urgency is null "
            "and action_due_date is null and recipient_role = 'summary' "
            "and observation_start is not null and observation_end is not null"
            ") or ("
            "category in ('next_action_due', 'next_action_escalation') "
            "and subject_kind in ('constraint', 'statement') "
            "and plan_decision_id is not null and urgency is not null "
            "and recipient_role in ('assignee', 'escalation') "
            "and ("
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)"
            ")"
            ")",
            name="ck_due_action_notification_shape",
        ),
        CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_due_action_notification_key_hex",
        ),
        CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_due_action_notification_actor",
        ),
        CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_due_action_notification_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    subject_kind: Mapped[str | None] = mapped_column(String(16))
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    # The current Next Action Work Decision the finding was derived against — the
    # plan identity that makes an unchanged condition converge and a changed plan
    # a new occurrence (ADR-0038's independent Next Action chain tail).
    plan_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id")
    )
    urgency: Mapped[str | None] = mapped_column(String(16))
    action_due_date: Mapped[date | None] = mapped_column(Date)
    # The applicable check/configuration identity (ruleset version and the
    # effective threshold configuration) the finding was derived under.
    check_identity: Mapped[str | None] = mapped_column(String(128))
    # The observation window the occurrence covers.  For a reminder or escalation
    # it is the single observation date; for a daily summary it is the exposed
    # window the digest rolls up.
    observation_start: Mapped[date | None] = mapped_column(Date)
    observation_end: Mapped[date | None] = mapped_column(Date)
    # The frozen, non-interrupting digest a daily summary exposes: its window and
    # the eligible bounded counts for the recipient, never a replay of history.
    # Null for a subject-bound reminder or escalation.
    summary_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    recipient_role: Mapped[str] = mapped_column(String(16))
    recipient_roster_entry_id: Mapped[int] = mapped_column(
        ForeignKey("project_roster_entries.id")
    )
    recipient_principal_subject: Mapped[str] = mapped_column(String(128))
    configuration_version: Mapped[str] = mapped_column(String(64))
    occurrence_key: Mapped[str] = mapped_column(String(64))
    registered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DueActionNotificationDispatch(Base):
    """Mutable delivery standing for one due-action occurrence (#352).

    Identical delivery machinery to the #351 assignment dispatch: the occurrence
    is immutable and this row carries the queued / completed / retry-due / failed
    / uncertain state, the resolved verified contact (or a visible delivery
    limitation), retained provider evidence, and bounded retry state.  Its
    identity is immutable; only the delivery standing changes.
    """

    __tablename__ = "due_action_notification_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", name="uq_due_action_dispatch_notification"
        ),
        CheckConstraint("channel = 'email'", name="ck_due_action_dispatch_channel"),
        CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_due_action_dispatch_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_due_action_dispatch_attempt_count"
        ),
        CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_due_action_dispatch_retry_shape",
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_due_action_dispatch_idempotency_hex",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("due_action_notifications.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    delivery_state: Mapped[str] = mapped_column(String(16))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DueActionNotificationAttempt(Base):
    """Append-only record of one due-action delivery attempt (#352, ADR-0032).

    Every sweep of a dispatch appends one row: what was attempted, the retained
    provider result and idempotency evidence, and the explicit outcome —
    including an ``uncertain`` outcome when an acknowledgment is unavailable and a
    ``skipped`` outcome when the re-derived condition is no longer current (the
    action completed, was cancelled or deferred, the plan changed, membership was
    revoked, or a typed contact could not be resolved).  Nothing here is mutated.
    """

    __tablename__ = "due_action_notification_attempts"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_due_action_attempt_number"
        ),
        CheckConstraint(
            "outcome in "
            "('completed', 'retry_due', 'failed', 'uncertain', 'skipped')",
            name="ck_due_action_attempt_outcome",
        ),
        CheckConstraint(
            "attempt_number > 0", name="ck_due_action_attempt_positive"
        ),
        CheckConstraint(
            "length(trim(runtime_owner)) > 0",
            name="ck_due_action_attempt_owner",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("due_action_notification_dispatches.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    runtime_owner: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentNotification(Base):
    """One immutable document-related interruption occurrence (#353, ADR-0037).

    This carries the two remaining #196 immediate-notification categories
    (ADR-0034 decision 39): a previously affirmative Documentation Review whose
    applicable current support lapsed (``documentation_loss``), and an authentic
    registered source transition that affects a current Commitment or a
    relocation/removal/abandonment Constraint (``document_change``).  It shares
    the delivery adapter seam and the single supervised runtime with the
    new-assignment occurrence (#351) but never reuses its assignment-shaped row:
    a loss preserves the earlier review *and its author*, and one of its
    recipients (the original reviewer) may hold no current roster entry.

    The occurrence is bound to exact identities — the affirmative review, the
    reviewed supporting citation, and the proven source transition — so a
    persistent condition or a repeated processing pass converges on one row and
    never resends the same event.  Registration is a derived system act: Corridor
    stops showing the requirement as met and records visible high-priority work
    (ADR-0037); the earlier human judgment is preserved, never reversed here.
    """

    __tablename__ = "document_notifications"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_document_notification_key"),
        CheckConstraint(
            "category in ('documentation_loss', 'document_change')",
            name="ck_document_notification_category",
        ),
        CheckConstraint(
            "subject_kind in ('constraint', 'statement')",
            name="ck_document_notification_subject_kind",
        ),
        CheckConstraint(
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)",
            name="ck_document_notification_subject_shape",
        ),
        CheckConstraint(
            "recipient_role in "
            "('current_assignee', 'original_reviewer', "
            "'current_assignee_and_original_reviewer')",
            name="ck_document_notification_recipient_role",
        ),
        # A loss names the affirmative review it preserves; a change names an
        # authentic registered source transition (a proven revision comparison
        # or a superseding statement event).  Neither is inferred from a
        # filename, a date, or an unproven replacement.
        CheckConstraint(
            "(category = 'documentation_loss' and review_confirmation_id is not null) "
            "or (category = 'document_change' and "
            "(comparison_id is not null or statement_event_id is not null))",
            name="ck_document_notification_authentic_source",
        ),
        CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_document_notification_key_hex",
        ),
        CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_document_notification_actor",
        ),
        CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_document_notification_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    subject_kind: Mapped[str] = mapped_column(String(16))
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    recipient_principal_subject: Mapped[str] = mapped_column(String(128))
    recipient_role: Mapped[str] = mapped_column(String(48))
    # Category A (documentation_loss): the earlier affirmative Documentation
    # Review, its stated requirement, and the exact reviewed supporting citation.
    review_confirmation_id: Mapped[int | None] = mapped_column(
        ForeignKey("documentation_field_confirmations.id")
    )
    requirement_field: Mapped[str | None] = mapped_column(String(64))
    reviewed_evidence_link_id: Mapped[int | None] = mapped_column(BigInteger)
    original_reviewer_subject: Mapped[str | None] = mapped_column(String(128))
    # The proven source transition (both categories where one applies): the
    # superseded and superseding documents, the immutable Revision Comparison
    # and finding, or the superseding statement event.
    predecessor_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id")
    )
    successor_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id")
    )
    comparison_id: Mapped[int | None] = mapped_column(BigInteger)
    finding_id: Mapped[int | None] = mapped_column(BigInteger)
    statement_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_events.id")
    )
    # A stable code for the current supported reason a review lost support, or
    # the change class a document transition produced; plain project language is
    # rendered from it, never shown as a raw code.
    reason_code: Mapped[str] = mapped_column(String(64))
    # An ambiguous or otherwise uncertain correspondence is retained honestly:
    # the message preserves the uncertainty and never presents it as proved.
    change_uncertain: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Exact source context the message links (requirement label, reviewed
    # citation, before/after, document names, page references).  Display only;
    # it never becomes a project decision and never leaves the project.
    source_context_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    occurrence_key: Mapped[str] = mapped_column(String(64))
    registered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentNotificationDispatch(Base):
    """Mutable delivery standing for one document-notification occurrence (#353).

    The occurrence is immutable; this row carries the queued / completed /
    retry-due / failed / uncertain state, the resolved verified contact (or a
    visible delivery limitation when the recipient has no current membership or
    typed contact, or the underlying condition resolved before dispatch),
    retained provider result and idempotency evidence, and bounded retry state.
    Its identity is immutable; only the delivery standing changes.
    """

    __tablename__ = "document_notification_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", name="uq_document_dispatch_notification"
        ),
        CheckConstraint("channel = 'email'", name="ck_document_dispatch_channel"),
        CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_document_dispatch_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_document_dispatch_attempt_count"
        ),
        CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_document_dispatch_retry_shape",
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_document_dispatch_idempotency_hex",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("document_notifications.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    delivery_state: Mapped[str] = mapped_column(String(16))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DocumentNotificationAttempt(Base):
    """Append-only record of one document-notification delivery attempt (#353).

    Every sweep of a dispatch appends one row: what was attempted, the retained
    provider result and idempotency evidence, and the explicit outcome —
    including an ``uncertain`` outcome when an acknowledgment is unavailable and
    a ``skipped`` outcome when a re-checked membership, contact, or underlying
    condition made the interruption no longer current.  Nothing here is mutated.
    """

    __tablename__ = "document_notification_attempts"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_document_attempt_number"
        ),
        CheckConstraint(
            "outcome in "
            "('completed', 'retry_due', 'failed', 'uncertain', 'skipped')",
            name="ck_document_attempt_outcome",
        ),
        CheckConstraint(
            "attempt_number > 0", name="ck_document_attempt_positive"
        ),
        CheckConstraint(
            "length(trim(runtime_owner)) > 0",
            name="ck_document_attempt_owner",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("document_notification_dispatches.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    runtime_owner: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
