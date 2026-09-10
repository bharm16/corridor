"""New-assignment notifications and their reliable, recoverable delivery (#351).

A committed roster-backed assignment must reach the assigned person, and
ownership must not wait on that message.  A feature-local mail queue was
rejected: it would duplicate the leases, retries, clock, and crash recovery the
one supervised Due Work runtime (#332) already owns, and it would tempt a
delivery outcome to gate ownership.  So this module keeps two responsibilities
and nothing else:

- **Registration** — ``register_new_assignment_notification`` writes one
  immutable occurrence bound to the exact subject, assignment Work Decision, and
  selected roster identity, plus a queued dispatch, in the *same* transaction as
  the assignment.  A rolled-back save leaves nothing; a crash after commit keeps
  it.  Repeated triggers converge on one occurrence through its fingerprint.
  Registration never resolves a recipient from a display name, free text, or a
  guessed address: the contact comes only from the typed verified-contact record
  (``PersonIdentity``) for the roster identity's principal, and an unresolved
  legacy mapping becomes a visible delivery limitation without touching the
  assignment.

- **Delivery** — ``deliver_project_assignment_notifications`` is what the shared
  runtime's server-owned handler calls.  The queue, the budget, the retry
  arithmetic, the attempt receipts, and the pass receipt are
  :mod:`corridor.outgoing_dispatch`'s, because they were the same in all three
  notification families and drifted while they were copied; what stays here is
  what only an assignment can answer — whether the assignment is *still* current
  (reassignment, revoked membership, changed contact) before every send, and
  what the message says about its subject.

This module deliberately does not import the assignment writers (they import it),
the runtime, or the web layer; it reaches the current-assignment fact through the
same ``WorkDecision`` tail the writers project from.  The provider seam
(``DeliveryAdapter`` and the adapter registry) is re-exported from the dispatch
module so the runtime and the other families keep one import site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from corridor import access, audit, digests, outgoing_dispatch
from corridor.models import (
    AssignmentNotification,
    AssignmentNotificationAttempt,
    AssignmentNotificationDispatch,
    AssignmentNotificationFeedback,
    CommitmentLineage,
    Dependency,
    DueActionNotification,
    DueActionNotificationAttempt,
    DueActionNotificationDispatch,
    DueWorkSchedule,
    ProjectRosterEntry,
    WorkDecision,
)
from corridor.outgoing_dispatch import (
    Currency,
    DeliveryAdapter,
    DeliveryOutcome,
    DeliveryRequest,
    DisabledDeliveryAdapter,
    RecordingDeliveryAdapter,
    clear_delivery_adapters,
    register_delivery_adapter,
    resolve_delivery_adapter,
    LIMITATION_REVOKED_MEMBERSHIP,
    LIMITATION_UNRESOLVED_CONTACT,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.statement_lifecycle import current_work_decision_filter


# The Due Work handler key for assignment-notification delivery. Defined here,
# next to the domain record it reconciles, and imported by the runtime that
# registers the handler — the runtime depends on this module, never the reverse.
ASSIGNMENT_NOTIFICATION_HANDLER = "assignment_notification"

# The one interruption category this slice emits (#351). Reminders, escalation,
# and change/lost-support notices are separately scoped successors.
CATEGORY_NEW_ASSIGNMENT = "new_assignment"

# The single delivery channel this slice targets.
CHANNEL_EMAIL = "email"

# The Work Decision field the assignment writes. Mirrors
# ``corridor.work_decisions.INTERNAL_OWNER``, which cannot be imported here
# without cycling the assignment writer back through its own notifier.
_INTERNAL_OWNER_FIELD = "internal_owner"

# The visible delivery limitation this category owns. It keeps the assignment
# intact and only explains why a message could not be delivered as current; the
# recipient-side limitations every family shares live with the dispatch machine
# and are re-exported above.
LIMITATION_REASSIGNED = "reassigned_superseded"

_RESULT_SCHEMA_VERSION = "assignment-notification-result-v1"


class NotificationRefusal(ValueError):
    """A notification act was refused; nothing was written."""


class NotificationAccessRefusal(NotificationRefusal):
    """The acting person is not entitled to this notification act."""


# --- Registration ---------------------------------------------------------


def register_new_assignment_notification(
    session: Session,
    *,
    assignment_decision: WorkDecision,
    roster_entry: ProjectRosterEntry,
    principal: HumanPrincipal,
) -> AssignmentNotification:
    """Register exactly one occurrence for a committed new assignment (#351).

    Called from every roster-backed assignment entry point in the same
    transaction as the assignment, so registration is atomic with it. Idempotent
    on the occurrence fingerprint: a repeated identical assignment, or a
    competing writer, converges on one occurrence and one queued dispatch.
    """
    recorder = require_human_principal(principal)
    if assignment_decision.field != _INTERNAL_OWNER_FIELD:
        raise NotificationRefusal(
            "a new-assignment notification tracks an Internal Owner decision"
        )
    if assignment_decision.dependency_id is not None:
        subject_kind = "constraint"
        dependency_id: int | None = assignment_decision.dependency_id
        commitment_lineage_id: int | None = None
        subject_identity = ("constraint", assignment_decision.dependency_id)
    elif assignment_decision.commitment_lineage_id is not None:
        subject_kind = "statement"
        dependency_id = None
        commitment_lineage_id = assignment_decision.commitment_lineage_id
        subject_identity = ("statement", assignment_decision.commitment_lineage_id)
    else:
        raise NotificationRefusal("the assignment decision names no subject")

    if not isinstance(roster_entry, ProjectRosterEntry):
        raise NotificationRefusal("a recipient must be a typed project roster entry")
    project_id = roster_entry.project_id
    recipient_principal = roster_entry.principal_subject

    occurrence_key = _occurrence_key(
        subject_identity=subject_identity,
        assignment_decision_id=assignment_decision.id,
        recipient_principal=recipient_principal,
    )
    public_id = f"assignment-notification:{occurrence_key[:24]}"
    session.execute(
        insert(AssignmentNotification)
        .values(
            public_id=public_id,
            project_id=project_id,
            category=CATEGORY_NEW_ASSIGNMENT,
            subject_kind=subject_kind,
            dependency_id=dependency_id,
            commitment_lineage_id=commitment_lineage_id,
            assignment_decision_id=assignment_decision.id,
            recipient_roster_entry_id=roster_entry.id,
            recipient_principal_subject=recipient_principal,
            occurrence_key=occurrence_key,
            registered_by=recorder.subject,
        )
        .on_conflict_do_nothing(index_elements=["occurrence_key"])
    )
    notification = session.scalar(
        select(AssignmentNotification).where(
            AssignmentNotification.occurrence_key == occurrence_key
        )
    )
    if notification is None:  # pragma: no cover - insert just guaranteed a row
        raise NotificationRefusal("the notification occurrence could not be registered")

    existing = session.scalar(
        select(AssignmentNotificationDispatch).where(
            AssignmentNotificationDispatch.notification_id == notification.id
        )
    )
    if existing is None:
        contact, limitation = outgoing_dispatch.resolve_contact(
            session, recipient_principal
        )
        idempotency_key = digests.canonical_sha256(
            {"occurrence_key": occurrence_key, "channel": CHANNEL_EMAIL}
        )
        session.execute(
            insert(AssignmentNotificationDispatch)
            .values(
                public_id=f"assignment-dispatch:{occurrence_key[:24]}",
                notification_id=notification.id,
                project_id=project_id,
                channel=CHANNEL_EMAIL,
                delivery_state="queued",
                recipient_contact=contact,
                delivery_limitation=limitation,
                attempt_count=0,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=["notification_id"])
        )
        session.flush()
    return notification


def _occurrence_key(
    *,
    subject_identity: tuple[str, int],
    assignment_decision_id: int,
    recipient_principal: str,
) -> str:
    return digests.canonical_sha256(
        {
            "category": CATEGORY_NEW_ASSIGNMENT,
            "subject_kind": subject_identity[0],
            "subject_id": subject_identity[1],
            "assignment_decision_id": assignment_decision_id,
            "recipient_principal": recipient_principal,
        }
    )


# --- Delivery sweep -------------------------------------------------------


def _assignment_category() -> outgoing_dispatch.DispatchCategory:
    """What the new-assignment family contributes to the shared machine."""
    return outgoing_dispatch.DispatchCategory(
        name="assignment",
        handler_key=ASSIGNMENT_NOTIFICATION_HANDLER,
        notification_model=AssignmentNotification,
        dispatch_model=AssignmentNotificationDispatch,
        attempt_model=AssignmentNotificationAttempt,
        result_schema_version=_RESULT_SCHEMA_VERSION,
        currency=_assignment_currency,
        subject_summary=_subject_summary,
    )


def deliver_project_assignment_notifications(
    session_factory,
    *,
    project_id: int,
    configuration_version: str,
    channel: str,
    adapter: DeliveryAdapter,
    clock,
    max_attempts: int,
    backoff_seconds: int,
    budget: int,
    owner: str,
) -> dict[str, Any]:
    """Deliver this project's due assignment notifications through the adapter.

    The shared dispatch machine owns the pass: each dispatch is processed in its
    own short transactions, the provider call holds no transaction at all, and
    the pass is bounded by ``budget``. What this category adds is
    ``_assignment_currency`` — before every send the assignment is re-checked, so
    an obsolete assignment is never presented as current.
    """
    return outgoing_dispatch.deliver_pass(
        session_factory,
        _assignment_category(),
        project_id=project_id,
        configuration_version=configuration_version,
        channel=channel,
        adapter=adapter,
        clock=clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=budget,
        owner=owner,
    )


def _assignment_currency(
    session: Session, notification: AssignmentNotification
) -> Currency:
    """Re-check the assignment before dispatch; never present an obsolete one.

    A reassigned or undone assignment is withheld as a benign skip, because its
    replacement carries its own occurrence. A revoked membership or an unresolved
    contact is a limitation the operator should see.
    """
    successor = aliased(WorkDecision)
    live = session.scalar(
        select(WorkDecision.id).where(
            WorkDecision.id == notification.assignment_decision_id,
            current_work_decision_filter(WorkDecision.id),
            ~select(successor.id)
            .where(
                successor.predecessor_decision_id
                == notification.assignment_decision_id
            )
            .exists(),
        )
    )
    if live is None:
        return Currency.withheld("superseded", LIMITATION_REASSIGNED, benign=True)
    withheld = outgoing_dispatch.withheld_recipient(
        session,
        project_id=notification.project_id,
        principal_subject=notification.recipient_principal_subject,
    )
    if withheld is not None:
        return withheld
    contact, unreachable = outgoing_dispatch.contact_currency(
        session, notification.recipient_principal_subject
    )
    if unreachable is not None:
        return unreachable
    return Currency.current(contact)


# --- Reads: recipient inbox and operations delivery view ------------------


def recipient_inbox(
    session: Session, *, project_id: int, principal_subject: str
) -> list[dict[str, Any]]:
    """One member's new-assignment notifications in this project, with standing.

    Scoped to the signed-in member and the one project, so no other project's
    records or another person's assignments are exposed.
    """
    rows = outgoing_dispatch.paired_rows(
        session,
        AssignmentNotification,
        AssignmentNotificationDispatch,
        project_id=project_id,
        principal_subject=principal_subject,
    )
    flagged = _flagged_notification_ids(
        session, project_id=project_id, flagged_by=principal_subject
    )
    return [
        _inbox_row(session, notification, dispatch, notification.id in flagged)
        for notification, dispatch in rows
    ]


def operations_delivery_view(
    session: Session, *, project_id: int
) -> dict[str, Any]:
    """Every assignment-notification delivery in one project, for the operator.

    Distinguishes queued, completed, retry-due, failed, and uncertain deliveries
    with subject context and any delivery limitation, and whether real delivery
    is enabled by a recorded gate-7 configuration. Scoped to one project.
    """
    rows = outgoing_dispatch.paired_rows(
        session,
        AssignmentNotification,
        AssignmentNotificationDispatch,
        project_id=project_id,
    )
    enabled = outgoing_dispatch.enabled_schedule(
        session, project_id=project_id, handler_key=ASSIGNMENT_NOTIFICATION_HANDLER
    )
    return outgoing_dispatch.operations_projection(
        rows,
        delivery_enabled=enabled is not None,
        row=lambda notification, dispatch: _operations_row(
            session, notification, dispatch
        ),
    )


def _inbox_row(
    session: Session,
    notification: AssignmentNotification,
    dispatch: AssignmentNotificationDispatch,
    flagged: bool,
) -> dict[str, Any]:
    summary = _subject_summary(session, notification)
    return {
        "notification_id": notification.id,
        "public_id": notification.public_id,
        "subject_kind": notification.subject_kind,
        "subject_label": summary["subject_label"],
        "delivery_state": dispatch.delivery_state,
        "delivery_status": _human_delivery_status(dispatch),
        "delivery_limitation": dispatch.delivery_limitation,
        "flagged_incorrect": flagged,
    }


def _operations_row(
    session: Session,
    notification: AssignmentNotification,
    dispatch: AssignmentNotificationDispatch,
) -> dict[str, Any]:
    summary = _subject_summary(session, notification)
    return {
        "notification_id": notification.id,
        "public_id": notification.public_id,
        "subject_kind": notification.subject_kind,
        "subject_label": summary["subject_label"],
        "recipient": notification.recipient_principal_subject,
        "delivery_state": dispatch.delivery_state,
        "delivery_status": _human_delivery_status(dispatch),
        "delivery_limitation": dispatch.delivery_limitation,
        "attempt_count": dispatch.attempt_count,
        "last_error_code": dispatch.last_error_code,
    }


def _human_delivery_status(dispatch: AssignmentNotificationDispatch) -> str:
    return outgoing_dispatch.human_delivery_status(
        dispatch,
        {LIMITATION_REASSIGNED: "assignment changed before delivery"},
    )


def _subject_summary(
    session: Session, notification: AssignmentNotification
) -> dict[str, Any]:
    if notification.subject_kind == "constraint":
        dependency = session.get(Dependency, notification.dependency_id)
        label = dependency.ref_code if dependency is not None else "constraint"
        return {"subject_label": f"Constraint {label}", "subject_ref": label}
    lineage = session.get(CommitmentLineage, notification.commitment_lineage_id)
    label = (
        f"Commitment {lineage.id}" if lineage is not None else "statement commitment"
    )
    return {"subject_label": label, "subject_ref": str(notification.commitment_lineage_id)}


# --- Wrong-assignment feedback --------------------------------------------


def flag_incorrect_assignment(
    session: Session,
    *,
    notification_id: int,
    principal: HumanPrincipal,
    note: str | None = None,
) -> AssignmentNotificationFeedback:
    """Let the assigned person flag an assignment without changing it (#351).

    The signed marker is attributable and append-only; the assignment and its
    Work Decision history stand until an authorized person changes them. Only the
    assigned person, and only while an active member, may flag.
    """
    recorder = require_human_principal(principal)
    notification = session.get(AssignmentNotification, notification_id)
    if notification is None:
        raise NotificationRefusal("no such assignment notification")
    membership = access.resolve_membership(
        session, recorder.subject, notification.project_id
    )
    if membership is None:
        raise NotificationAccessRefusal("not a member of this project")
    if recorder.subject != notification.recipient_principal_subject:
        raise NotificationAccessRefusal(
            "only the assigned person may flag this assignment"
        )
    existing = session.scalar(
        select(AssignmentNotificationFeedback).where(
            AssignmentNotificationFeedback.notification_id == notification.id,
            AssignmentNotificationFeedback.flagged_by == recorder.subject,
        )
    )
    if existing is not None:
        return existing

    if notification.subject_kind == "constraint":
        entity_type = audit.DEPENDENCY
        entity_id = notification.dependency_id
    else:
        entity_type = audit.COMMITMENT_LINEAGE
        entity_id = notification.commitment_lineage_id
    audit_entry = audit.record(
        session,
        principal=recorder,
        action=audit.FLAG_INCORRECT_ASSIGNMENT,
        entity_type=entity_type,
        entity_id=int(entity_id),
        after={
            "assignment_notification_id": notification.id,
            "assignment_decision_id": notification.assignment_decision_id,
            "recipient_principal_subject": notification.recipient_principal_subject,
            "note": (note or "").strip() or None,
        },
    )
    feedback = AssignmentNotificationFeedback(
        public_id=f"assignment-feedback:{uuid4().hex[:24]}",
        notification_id=notification.id,
        project_id=notification.project_id,
        flagged_by=recorder.subject,
        feedback_kind="incorrect_assignment",
        note=(note or "").strip() or None,
        audit_log_id=audit_entry.id,
    )
    session.add(feedback)
    session.flush([feedback])
    return feedback


def _flagged_notification_ids(
    session: Session, *, project_id: int, flagged_by: str
) -> set[int]:
    return set(
        session.scalars(
            select(AssignmentNotificationFeedback.notification_id).where(
                AssignmentNotificationFeedback.project_id == project_id,
                AssignmentNotificationFeedback.flagged_by == flagged_by,
            )
        ).all()
    )


# ==========================================================================
# Due-action notifications (#352): reminders, escalation, and daily summaries
# ==========================================================================
#
# The #351 machinery above delivers one immutable occurrence that a committed
# assignment registered.  This slice adds the three *derived* categories of
# ADR-0034 decisions 39 and 48.  Their conditions are not a single committed
# event: a Next Action becomes soon-due or past-due as the clock advances, and a
# daily digest rolls up standing information.  So each supervised tick re-derives
# the current conditions for the *complete* subject population from the subject's
# current authoritative plan and the *existing* check semantics, and converges on
# one durable occurrence per condition rather than emitting a new event on every
# poll.  Delivery reuses the same adapter seam, dispatch state machine, and
# append-only attempt receipts; the occurrence identity and the currency re-check
# are what differ.
#
# Boundaries this section keeps (the ticket's acceptance criteria and the ADRs):
# - An unknown Action Due Date is never invented into a date or an overdue
#   finding, and an External Organization's Promised For / Required By timing
#   never substitutes for the project's own Action Due Date (ADR-0038).  The
#   constraint predicate is exactly the existing exception engine's
#   ACTION_DUE_SOON/ACTION_OVERDUE; the statement predicate applies the same
#   thresholds to the statement plan, and both fire only on a present due date.
# - Immediate reminders reach only the assigned person; urgent overdue also
#   reaches one configured escalation contact, deduplicated when they coincide,
#   never the whole team (decision 48).  Missing escalation configuration leaves
#   escalation disabled and visible — no invented urgency rule or substitute
#   contact.
# - Daily summaries are one durable non-interrupting occurrence per recipient and
#   observation window; they are not a fifth immediate category and never replay
#   history.
# - The derivation writes nothing to the plan: a failed or uncertain delivery
#   cannot undo a plan, alter ownership, complete an action, or claim an External
#   Organization completed work.

# The Due Work handler key for due-action delivery.  Defined here beside the
# domain record it reconciles and imported by the runtime; the runtime depends on
# this module, never the reverse.
DUE_ACTION_NOTIFICATION_HANDLER = "due_action_notification"

# The three derived categories (all within ADR-0034 decision 39's four approved
# immediate categories plus the non-interrupting daily summary — never a fifth
# interruption).
CATEGORY_NEXT_ACTION_DUE = "next_action_due"
CATEGORY_NEXT_ACTION_ESCALATION = "next_action_escalation"
CATEGORY_DAILY_SUMMARY = "daily_summary"

ROLE_ASSIGNEE = "assignee"
ROLE_ESCALATION = "escalation"
ROLE_SUMMARY = "summary"

URGENCY_SOON = "soon"
URGENCY_OVERDUE = "overdue"
URGENCY_URGENT_OVERDUE = "urgent_overdue"

# Visible withheld-delivery limitations specific to the derived conditions.  Each
# keeps the plan and history intact and only explains why a reminder that once
# matched is no longer current (ADR-0032).
LIMITATION_PLAN_SUPERSEDED = "plan_superseded"
LIMITATION_ACTION_REASSIGNED = "action_reassigned"
LIMITATION_DEFERRED = "deferred"
LIMITATION_CONDITION_CLEARED = "condition_cleared"
LIMITATION_ESCALATION_DEDUPLICATED = "escalation_deduplicated"

_DUE_ACTION_RESULT_SCHEMA_VERSION = "due-action-notification-result-v1"

_NEXT_ACTION_FIELD = "next_action"


@dataclass(frozen=True)
class _DueActionRecipient:
    """One typed recipient identity: a roster row and its principal subject."""

    roster_entry_id: int
    principal_subject: str


@dataclass(frozen=True)
class DueActionReminder:
    """One derived soon-due or past-due Next Action reminder for the assignee."""

    subject_kind: str
    dependency_id: int | None
    commitment_lineage_id: int | None
    plan_decision_id: int
    urgency: str
    action_due_date: date
    days: int
    urgent: bool
    assignee: _DueActionRecipient


@dataclass(frozen=True)
class DueActionEscalation:
    """One derived urgent-overdue escalation to the configured escalation contact."""

    subject_kind: str
    dependency_id: int | None
    commitment_lineage_id: int | None
    plan_decision_id: int
    action_due_date: date
    days: int
    escalation: _DueActionRecipient


@dataclass(frozen=True)
class DueActionSummary:
    """One recipient's non-interrupting digest counts for an observation window."""

    recipient: _DueActionRecipient
    counts: dict[str, int]


@dataclass(frozen=True)
class DueActionDerivation:
    """The complete current derivation for one project as of one date."""

    project_id: int
    today: date
    check_identity: str
    reminders: tuple[DueActionReminder, ...]
    escalations: tuple[DueActionEscalation, ...]
    escalation_enabled: bool
    escalation_disabled_reason: str | None


# --- Derivation over the complete subject population -----------------------


def derive_due_action_conditions(
    session: Session,
    *,
    project_id: int,
    today: date,
    urgent_overdue_days: int,
    escalation_roster_entry_id: int | None,
) -> DueActionDerivation:
    """Derive the current soon/past-due conditions for the whole population.

    Both subject kinds are read from their *current authoritative plans* and the
    *applicable existing check semantics*: constraints reuse the exception
    engine's ACTION_DUE_SOON / ACTION_OVERDUE facts (same live gating, same
    per-project thresholds, same ruleset), and statements apply the same
    thresholds to the accepted Commitment Lineage's plan.  The population is
    complete — every dependency and every coordinating lineage in the project,
    not the immediate or paginated Work List.  A subject with an unknown Action
    Due Date, a deferred item not yet returned, a closed External Organization
    fact, or a plan marked for review contributes nothing.
    """
    # Imported here rather than at module load: exceptions -> disputes ->
    # work_decisions -> notifications, so a module-level import would cycle.
    from corridor.check_configuration import effective_configuration
    from corridor.dependency_events import closed_party_commitment_lineages
    from corridor.exceptions import evaluate_project
    from corridor.statement_lifecycle import observe_current_statements

    effective = effective_configuration(session, project_id)
    thresholds = effective.thresholds
    check_identity = (
        f"{effective.ruleset_version}:"
        f"{effective.configuration_id if effective.configuration_id is not None else 'default'}"
    )

    escalation_recipient, escalation_disabled_reason = _resolve_escalation_recipient(
        session, project_id=project_id, roster_entry_id=escalation_roster_entry_id
    )

    reminders: list[DueActionReminder] = []
    escalations: list[DueActionEscalation] = []

    def _emit(
        *,
        subject_kind: str,
        dependency_id: int | None,
        commitment_lineage_id: int | None,
        plan_decision_id: int,
        urgency: str,
        action_due_date: date,
        days: int,
    ) -> None:
        assignee = _current_assignee(
            session,
            dependency_id=dependency_id,
            commitment_lineage_id=commitment_lineage_id,
        )
        if assignee is None:
            # No typed roster identity is accountable, so there is no authorized
            # recipient — a reminder is never sent to an invented address.
            return
        urgent = urgency == URGENCY_OVERDUE and days >= urgent_overdue_days
        reminders.append(
            DueActionReminder(
                subject_kind=subject_kind,
                dependency_id=dependency_id,
                commitment_lineage_id=commitment_lineage_id,
                plan_decision_id=plan_decision_id,
                urgency=urgency,
                action_due_date=action_due_date,
                days=days,
                urgent=urgent,
                assignee=assignee,
            )
        )
        if (
            urgent
            and escalation_recipient is not None
            and escalation_recipient.principal_subject != assignee.principal_subject
        ):
            escalations.append(
                DueActionEscalation(
                    subject_kind=subject_kind,
                    dependency_id=dependency_id,
                    commitment_lineage_id=commitment_lineage_id,
                    plan_decision_id=plan_decision_id,
                    action_due_date=action_due_date,
                    days=days,
                    escalation=escalation_recipient,
                )
            )

    # --- Constraint subjects: the existing exception check semantics ---
    evaluation = evaluate_project(
        session, project_id, today=today, thresholds=thresholds
    )
    for exception in evaluation.found:
        urgency = _URGENCY_BY_RULE.get(exception.rule)
        if urgency is None:
            continue
        dependency = session.get(Dependency, exception.dependency_id)
        if dependency is None or dependency.action_due_date is None:
            continue
        # A deferred item is not immediate work until its stated return date
        # (ADR-0034 decisions 47/78); the exception ledger still lists it, but
        # the immediate reminder is withheld while it is deferred.
        if _has_live_deferral(dependency, today):
            continue
        plan_decision = _current_plan_decision(
            session, dependency_id=dependency.id
        )
        if plan_decision is None or plan_decision.after_value is None:
            continue
        _emit(
            subject_kind="constraint",
            dependency_id=dependency.id,
            commitment_lineage_id=None,
            plan_decision_id=plan_decision.id,
            urgency=urgency,
            action_due_date=dependency.action_due_date,
            days=exception.quantity_days or 0,
        )

    # --- Statement subjects: the same thresholds over the lineage plan ---
    lineages = session.scalars(
        select(CommitmentLineage)
        .where(CommitmentLineage.project_id == project_id)
        .order_by(CommitmentLineage.id)
    ).all()
    if lineages:
        observations = observe_current_statements(
            session, [lineage.id for lineage in lineages]
        )
        closed = closed_party_commitment_lineages(session, project_id)
        for lineage in lineages:
            if lineage.id not in observations:
                # Not a currently coordinating accepted Commitment or Change.
                continue
            if lineage.id in closed:
                # The External Organization fact is closed; not live work.
                continue
            if lineage.plan_needs_review:
                # A material successor marked the plan for review (ADR-0038);
                # Corridor must not silently reminder-drive a plan whose fact
                # may have changed.
                continue
            if _has_live_deferral(lineage, today):
                continue
            if not lineage.next_action or lineage.action_due_date is None:
                continue
            delta = (lineage.action_due_date - today).days
            if delta < 0:
                urgency, days = URGENCY_OVERDUE, -delta
            elif delta <= thresholds.action_due_soon_days:
                urgency, days = URGENCY_SOON, delta
            else:
                continue
            plan_decision = _current_plan_decision(
                session, commitment_lineage_id=lineage.id
            )
            if plan_decision is None or plan_decision.after_value is None:
                continue
            _emit(
                subject_kind="statement",
                dependency_id=None,
                commitment_lineage_id=lineage.id,
                plan_decision_id=plan_decision.id,
                urgency=urgency,
                action_due_date=lineage.action_due_date,
                days=days,
            )

    return DueActionDerivation(
        project_id=project_id,
        today=today,
        check_identity=check_identity,
        reminders=tuple(reminders),
        escalations=tuple(escalations),
        escalation_enabled=escalation_recipient is not None,
        escalation_disabled_reason=escalation_disabled_reason,
    )


_URGENCY_BY_RULE = {
    "ACTION_DUE_SOON": URGENCY_SOON,
    "ACTION_OVERDUE": URGENCY_OVERDUE,
}


def _has_live_deferral(projection: Any, today: date) -> bool:
    return (
        projection.deferral_return_date is not None
        and projection.deferral_return_date > today
    )


def _current_decision_tail(
    session: Session,
    *,
    field: str,
    dependency_id: int | None = None,
    commitment_lineage_id: int | None = None,
) -> WorkDecision | None:
    """The current, non-superseded Work Decision tail for one subject and field.

    Mirrors ``work_decisions._tail`` without importing that module (which imports
    this one), so the derivation reads the authoritative chain tail directly.
    """
    successor = aliased(WorkDecision)
    clause = (
        WorkDecision.dependency_id == dependency_id
        if dependency_id is not None
        else WorkDecision.commitment_lineage_id == commitment_lineage_id
    )
    return session.scalar(
        select(WorkDecision)
        .where(
            clause,
            WorkDecision.field == field,
            current_work_decision_filter(WorkDecision.id),
            ~select(successor.id)
            .where(successor.predecessor_decision_id == WorkDecision.id)
            .exists(),
        )
        .order_by(WorkDecision.id)
    )


def _current_plan_decision(
    session: Session,
    *,
    dependency_id: int | None = None,
    commitment_lineage_id: int | None = None,
) -> WorkDecision | None:
    return _current_decision_tail(
        session,
        field=_NEXT_ACTION_FIELD,
        dependency_id=dependency_id,
        commitment_lineage_id=commitment_lineage_id,
    )


def _current_assignee(
    session: Session,
    *,
    dependency_id: int | None = None,
    commitment_lineage_id: int | None = None,
) -> _DueActionRecipient | None:
    """The typed roster identity currently accountable for this subject.

    The reminder recipient reuses #351's typed assignment binding: the current
    Internal Owner Work Decision tail resolves the new-assignment occurrence that
    captured the exact roster identity when the person was assigned.  There is no
    display-name match and no invented recipient; an owner assigned outside the
    roster-backed path simply has no reminder recipient here.
    """
    owner_decision = _current_decision_tail(
        session,
        field=_INTERNAL_OWNER_FIELD,
        dependency_id=dependency_id,
        commitment_lineage_id=commitment_lineage_id,
    )
    if owner_decision is None or owner_decision.after_value is None:
        return None
    notification = session.scalar(
        select(AssignmentNotification).where(
            AssignmentNotification.assignment_decision_id == owner_decision.id
        )
    )
    if notification is None:
        return None
    roster = session.get(ProjectRosterEntry, notification.recipient_roster_entry_id)
    if roster is None or not roster.active:
        return None
    return _DueActionRecipient(
        roster_entry_id=roster.id,
        principal_subject=notification.recipient_principal_subject,
    )


def _resolve_escalation_recipient(
    session: Session,
    *,
    project_id: int,
    roster_entry_id: int | None,
) -> tuple[_DueActionRecipient | None, str | None]:
    """The one configured escalation contact, or a visible disabled reason.

    Escalation stays disabled — never an invented urgency rule or a substituted
    contact — until an operator declares one active project roster identity as the
    escalation contact (ADR-0034 decision 48).
    """
    if roster_entry_id is None:
        return None, "no escalation contact configured"
    roster = session.get(ProjectRosterEntry, roster_entry_id)
    if roster is None or roster.project_id != project_id:
        return None, "configured escalation contact is not a project roster entry"
    if not roster.active:
        return None, "configured escalation contact is no longer an active member"
    return (
        _DueActionRecipient(
            roster_entry_id=roster.id, principal_subject=roster.principal_subject
        ),
        None,
    )


# --- Registration: converge one durable occurrence per condition -----------


def register_due_action_notifications(
    session: Session,
    *,
    project_id: int,
    configuration_version: str,
    today: date,
    urgent_overdue_days: int,
    escalation_roster_entry_id: int | None,
    channel: str,
    owner: str,
    register_summary: bool,
    summary_window_start: date | None = None,
    summary_window_end: date | None = None,
) -> DueActionDerivation:
    """Idempotently register the current derived occurrences and their dispatches.

    Called inside a committed transaction on each supervised tick.  Every
    occurrence is upserted on its fingerprint, so an unchanged condition on a
    later poll converges on the same row and dispatch rather than emitting a new
    event; a changed plan, a soon->overdue crossing, a new check configuration, or
    a new summary window is a new occurrence.  Registration never withdraws an
    earlier occurrence — a condition that has since cleared is withheld at
    dispatch, preserving the delivery history (ADR-0032).
    """
    derivation = derive_due_action_conditions(
        session,
        project_id=project_id,
        today=today,
        urgent_overdue_days=urgent_overdue_days,
        escalation_roster_entry_id=escalation_roster_entry_id,
    )

    for reminder in derivation.reminders:
        subject_identity = (
            ("constraint", reminder.dependency_id)
            if reminder.subject_kind == "constraint"
            else ("statement", reminder.commitment_lineage_id)
        )
        occurrence_key = digests.canonical_sha256(
            {
                "category": CATEGORY_NEXT_ACTION_DUE,
                "subject_kind": subject_identity[0],
                "subject_id": subject_identity[1],
                "plan_decision_id": reminder.plan_decision_id,
                "urgency": reminder.urgency,
                "recipient_role": ROLE_ASSIGNEE,
                "recipient_principal": reminder.assignee.principal_subject,
                "check_identity": derivation.check_identity,
            }
        )
        _upsert_due_action_occurrence(
            session,
            occurrence_key=occurrence_key,
            values={
                "project_id": project_id,
                "category": CATEGORY_NEXT_ACTION_DUE,
                "subject_kind": reminder.subject_kind,
                "dependency_id": reminder.dependency_id,
                "commitment_lineage_id": reminder.commitment_lineage_id,
                "plan_decision_id": reminder.plan_decision_id,
                "urgency": reminder.urgency,
                "action_due_date": reminder.action_due_date,
                "check_identity": derivation.check_identity,
                "observation_start": today,
                "observation_end": today,
                "recipient_role": ROLE_ASSIGNEE,
                "recipient_roster_entry_id": reminder.assignee.roster_entry_id,
                "recipient_principal_subject": reminder.assignee.principal_subject,
                "configuration_version": configuration_version,
            },
            channel=channel,
            owner=owner,
        )

    for escalation in derivation.escalations:
        subject_identity = (
            ("constraint", escalation.dependency_id)
            if escalation.subject_kind == "constraint"
            else ("statement", escalation.commitment_lineage_id)
        )
        occurrence_key = digests.canonical_sha256(
            {
                "category": CATEGORY_NEXT_ACTION_ESCALATION,
                "subject_kind": subject_identity[0],
                "subject_id": subject_identity[1],
                "plan_decision_id": escalation.plan_decision_id,
                "urgency": URGENCY_URGENT_OVERDUE,
                "recipient_role": ROLE_ESCALATION,
                "recipient_principal": escalation.escalation.principal_subject,
                "check_identity": derivation.check_identity,
            }
        )
        _upsert_due_action_occurrence(
            session,
            occurrence_key=occurrence_key,
            values={
                "project_id": project_id,
                "category": CATEGORY_NEXT_ACTION_ESCALATION,
                "subject_kind": escalation.subject_kind,
                "dependency_id": escalation.dependency_id,
                "commitment_lineage_id": escalation.commitment_lineage_id,
                "plan_decision_id": escalation.plan_decision_id,
                "urgency": URGENCY_URGENT_OVERDUE,
                "action_due_date": escalation.action_due_date,
                "check_identity": derivation.check_identity,
                "observation_start": today,
                "observation_end": today,
                "recipient_role": ROLE_ESCALATION,
                "recipient_roster_entry_id": escalation.escalation.roster_entry_id,
                "recipient_principal_subject": escalation.escalation.principal_subject,
                "configuration_version": configuration_version,
            },
            channel=channel,
            owner=owner,
        )

    if register_summary and summary_window_start is not None and summary_window_end is not None:
        for summary in _derive_summaries(
            session,
            project_id=project_id,
            derivation=derivation,
            window_start=summary_window_start,
            window_end=summary_window_end,
        ):
            occurrence_key = digests.canonical_sha256(
                {
                    "category": CATEGORY_DAILY_SUMMARY,
                    "recipient_principal": summary.recipient.principal_subject,
                    "window_start": summary_window_start.isoformat(),
                    "window_end": summary_window_end.isoformat(),
                    "check_identity": derivation.check_identity,
                }
            )
            _upsert_due_action_occurrence(
                session,
                occurrence_key=occurrence_key,
                values={
                    "project_id": project_id,
                    "category": CATEGORY_DAILY_SUMMARY,
                    "subject_kind": None,
                    "dependency_id": None,
                    "commitment_lineage_id": None,
                    "plan_decision_id": None,
                    "urgency": None,
                    "action_due_date": None,
                    "check_identity": derivation.check_identity,
                    "observation_start": summary_window_start,
                    "observation_end": summary_window_end,
                    "summary_json": {
                        "window_start": summary_window_start.isoformat(),
                        "window_end": summary_window_end.isoformat(),
                        "counts": summary.counts,
                    },
                    "recipient_role": ROLE_SUMMARY,
                    "recipient_roster_entry_id": summary.recipient.roster_entry_id,
                    "recipient_principal_subject": summary.recipient.principal_subject,
                    "configuration_version": configuration_version,
                },
                channel=channel,
                owner=owner,
            )

    return derivation


def _derive_summaries(
    session: Session,
    *,
    project_id: int,
    derivation: DueActionDerivation,
    window_start: date,
    window_end: date,
) -> list[DueActionSummary]:
    """One non-interrupting digest per recipient with eligible info in the window.

    The digest rolls up the recipient's own current soon/overdue standing and the
    deferrals that return within the exposed window.  It is bounded, current, and
    never a replay of every historical condition.  A recipient with nothing
    eligible gets no occurrence.
    """
    counts: dict[str, dict[str, int]] = {}
    recipients: dict[str, _DueActionRecipient] = {}

    def _bucket(recipient: _DueActionRecipient) -> dict[str, int]:
        recipients.setdefault(recipient.principal_subject, recipient)
        return counts.setdefault(
            recipient.principal_subject,
            {"soon": 0, "overdue": 0, "urgent_overdue": 0, "returning_deferrals": 0},
        )

    for reminder in derivation.reminders:
        bucket = _bucket(reminder.assignee)
        bucket[reminder.urgency] += 1
        if reminder.urgent:
            bucket["urgent_overdue"] += 1

    for recipient, count in _returning_deferrals(
        session,
        project_id=project_id,
        window_start=window_start,
        window_end=window_end,
    ).items():
        bucket = _bucket(recipient)
        bucket["returning_deferrals"] += count

    return [
        DueActionSummary(recipient=recipients[principal], counts=counts[principal])
        for principal in sorted(recipients)
        if any(value > 0 for value in counts[principal].values())
    ]


def _returning_deferrals(
    session: Session,
    *,
    project_id: int,
    window_start: date,
    window_end: date,
) -> dict[_DueActionRecipient, int]:
    """Count, per accountable recipient, deferrals returning within the window."""
    tallies: dict[str, tuple[_DueActionRecipient, int]] = {}

    def _tally(recipient: _DueActionRecipient | None) -> None:
        if recipient is None:
            return
        existing = tallies.get(recipient.principal_subject)
        tallies[recipient.principal_subject] = (
            recipient,
            (existing[1] if existing else 0) + 1,
        )

    dependencies = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project_id,
            Dependency.dismissed_at.is_(None),
            Dependency.deferral_return_date.is_not(None),
            Dependency.deferral_return_date >= window_start,
            Dependency.deferral_return_date <= window_end,
        )
    ).all()
    for dependency in dependencies:
        _tally(_current_assignee(session, dependency_id=dependency.id))

    lineages = session.scalars(
        select(CommitmentLineage).where(
            CommitmentLineage.project_id == project_id,
            CommitmentLineage.deferral_return_date.is_not(None),
            CommitmentLineage.deferral_return_date >= window_start,
            CommitmentLineage.deferral_return_date <= window_end,
        )
    ).all()
    for lineage in lineages:
        _tally(_current_assignee(session, commitment_lineage_id=lineage.id))

    return {recipient: count for recipient, count in tallies.values()}


def _upsert_due_action_occurrence(
    session: Session,
    *,
    occurrence_key: str,
    values: dict[str, Any],
    channel: str,
    owner: str,
) -> DueActionNotification:
    """Upsert one occurrence and its queued dispatch on the occurrence fingerprint."""
    session.execute(
        insert(DueActionNotification)
        .values(
            public_id=f"due-action-notification:{occurrence_key[:24]}",
            occurrence_key=occurrence_key,
            registered_by=owner,
            **values,
        )
        .on_conflict_do_nothing(index_elements=["occurrence_key"])
    )
    notification = session.scalar(
        select(DueActionNotification).where(
            DueActionNotification.occurrence_key == occurrence_key
        )
    )
    if notification is None:  # pragma: no cover - the insert just guaranteed a row
        raise NotificationRefusal("a due-action occurrence could not be registered")
    existing = session.scalar(
        select(DueActionNotificationDispatch).where(
            DueActionNotificationDispatch.notification_id == notification.id
        )
    )
    if existing is None:
        contact, limitation = outgoing_dispatch.resolve_contact(
            session, notification.recipient_principal_subject
        )
        idempotency_key = digests.canonical_sha256(
            {"occurrence_key": occurrence_key, "channel": channel}
        )
        session.execute(
            insert(DueActionNotificationDispatch)
            .values(
                public_id=f"due-action-dispatch:{occurrence_key[:24]}",
                notification_id=notification.id,
                project_id=notification.project_id,
                channel=channel,
                delivery_state="queued",
                recipient_contact=contact,
                delivery_limitation=limitation,
                attempt_count=0,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=["notification_id"])
        )
        session.flush()
    return notification


# --- Delivery sweep: the category's contribution to the shared machine ------

# A benignly stale condition — a withdrawn or superseded plan, a reassignment, a
# deferral, a cleared finding, or an escalation contact that now coincides with
# the assignee — is a withheld reminder the operator need not act on.
_DUE_ACTION_BENIGN_LIMITATIONS = frozenset(
    {
        LIMITATION_PLAN_SUPERSEDED,
        LIMITATION_ACTION_REASSIGNED,
        LIMITATION_DEFERRED,
        LIMITATION_CONDITION_CLEARED,
        LIMITATION_ESCALATION_DEDUPLICATED,
    }
)


def deliver_project_due_action_notifications(
    session_factory,
    *,
    project_id: int,
    configuration_version: str,
    channel: str,
    adapter: DeliveryAdapter,
    clock,
    max_attempts: int,
    backoff_seconds: int,
    budget: int,
    urgent_overdue_days: int,
    escalation_roster_entry_id: int | None,
    owner: str,
) -> dict[str, Any]:
    """Deliver this project's due-action notifications through the adapter.

    The shared dispatch machine owns the pass; this category contributes the
    currency re-check that makes a *derived* condition safe to send. Before every
    send the derived condition is re-read — completion, cancellation, a successor
    action, deferral, reassignment, an external closure, or a coincident
    escalation contact all withhold a now-stale reminder without deleting its
    delivery history. The re-check is bound to this pass's date and escalation
    configuration, because those are what the condition is derived against.
    """
    today = outgoing_dispatch.aware_utc(clock.now()).date()

    def currency(session: Session, notification: DueActionNotification) -> Currency:
        return _due_action_currency(
            session,
            notification,
            today=today,
            urgent_overdue_days=urgent_overdue_days,
            escalation_roster_entry_id=escalation_roster_entry_id,
        )

    category = outgoing_dispatch.DispatchCategory(
        name="due-action",
        handler_key=DUE_ACTION_NOTIFICATION_HANDLER,
        notification_model=DueActionNotification,
        dispatch_model=DueActionNotificationDispatch,
        attempt_model=DueActionNotificationAttempt,
        result_schema_version=_DUE_ACTION_RESULT_SCHEMA_VERSION,
        currency=currency,
        subject_summary=_due_action_delivery_summary,
    )
    return outgoing_dispatch.deliver_pass(
        session_factory,
        category,
        project_id=project_id,
        configuration_version=configuration_version,
        channel=channel,
        adapter=adapter,
        clock=clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=budget,
        owner=owner,
    )


def _due_action_delivery_summary(
    session: Session, notification: DueActionNotification
) -> dict[str, Any]:
    """What the recipient is told: a frozen digest, or one subject's reminder."""
    if notification.category == CATEGORY_DAILY_SUMMARY:
        return _due_action_summary_payload(notification)
    return _due_action_subject_summary(session, notification)


def _withheld_due_action(status: str, limitation: str) -> Currency:
    """One withheld outcome, classified benign from the limitation itself."""
    return Currency.withheld(
        status, limitation, benign=limitation in _DUE_ACTION_BENIGN_LIMITATIONS
    )


def _due_action_currency(
    session: Session,
    notification: DueActionNotification,
    *,
    today: date,
    urgent_overdue_days: int,
    escalation_roster_entry_id: int | None,
) -> Currency:
    """Re-read the derived condition before dispatch; never present a stale one.

    The occurrence is ``current`` only when the condition still holds for the
    bound recipient; every other answer withholds the message and keeps the typed
    limitation that explains it.
    """
    withheld = outgoing_dispatch.withheld_recipient(
        session,
        project_id=notification.project_id,
        principal_subject=notification.recipient_principal_subject,
    )
    if withheld is not None:
        return withheld

    if notification.category != CATEGORY_DAILY_SUMMARY:
        # A daily summary is not subject-bound: its frozen digest stays current
        # as long as the recipient is still a member with a resolvable contact.
        stale = _stale_due_action_condition(
            session,
            notification,
            today=today,
            urgent_overdue_days=urgent_overdue_days,
            escalation_roster_entry_id=escalation_roster_entry_id,
        )
        if stale is not None:
            return stale

    contact, unreachable = outgoing_dispatch.contact_currency(
        session, notification.recipient_principal_subject
    )
    if unreachable is not None:
        return unreachable
    return Currency.current(contact)


def _stale_due_action_condition(
    session: Session,
    notification: DueActionNotification,
    *,
    today: date,
    urgent_overdue_days: int,
    escalation_roster_entry_id: int | None,
) -> Currency | None:
    """Why this reminder or escalation is no longer current, or ``None``.

    The bound plan decision must still be the current Next Action tail —
    completion, cancellation, or a successor action all replace it, and a due-date
    change appends a new decision with a new id.
    """
    plan_tail = _current_plan_decision(
        session,
        dependency_id=notification.dependency_id,
        commitment_lineage_id=notification.commitment_lineage_id,
    )
    if (
        plan_tail is None
        or plan_tail.after_value is None
        or plan_tail.id != notification.plan_decision_id
    ):
        return _withheld_due_action("superseded", LIMITATION_PLAN_SUPERSEDED)

    projection = _due_action_subject_projection(session, notification)
    if projection is None:
        return _withheld_due_action("superseded", LIMITATION_PLAN_SUPERSEDED)
    if _has_live_deferral(projection, today):
        return _withheld_due_action("deferred", LIMITATION_DEFERRED)
    if notification.subject_kind == "statement" and not _statement_still_live(
        session, notification.commitment_lineage_id
    ):
        return _withheld_due_action("superseded", LIMITATION_CONDITION_CLEARED)
    if projection.action_due_date is None:
        return _withheld_due_action("cleared", LIMITATION_CONDITION_CLEARED)

    band, days = _urgency_band(projection.action_due_date, today)
    current_assignee = _current_assignee(
        session,
        dependency_id=notification.dependency_id,
        commitment_lineage_id=notification.commitment_lineage_id,
    )
    if current_assignee is None:
        return _withheld_due_action("superseded", LIMITATION_PLAN_SUPERSEDED)

    if notification.category == CATEGORY_NEXT_ACTION_ESCALATION:
        # The escalation must still be urgent, still routed to the configured
        # contact, and still a distinct person from the current assignee.
        escalation, _ = _resolve_escalation_recipient(
            session,
            project_id=notification.project_id,
            roster_entry_id=escalation_roster_entry_id,
        )
        if (
            escalation is None
            or escalation.principal_subject != notification.recipient_principal_subject
        ):
            return _withheld_due_action("cleared", LIMITATION_CONDITION_CLEARED)
        if band != URGENCY_OVERDUE or days < urgent_overdue_days:
            return _withheld_due_action("cleared", LIMITATION_CONDITION_CLEARED)
        if escalation.principal_subject == current_assignee.principal_subject:
            return _withheld_due_action(
                "deduplicated", LIMITATION_ESCALATION_DEDUPLICATED
            )
        return None

    # An ordinary reminder must still be routed to the current assignee and match
    # the urgency band it was raised for.
    if current_assignee.principal_subject != notification.recipient_principal_subject:
        return _withheld_due_action("reassigned", LIMITATION_ACTION_REASSIGNED)
    if band != notification.urgency:
        return _withheld_due_action("cleared", LIMITATION_CONDITION_CLEARED)
    return None


def _urgency_band(action_due_date: date, today: date) -> tuple[str | None, int]:
    delta = (action_due_date - today).days
    if delta < 0:
        return URGENCY_OVERDUE, -delta
    return URGENCY_SOON, delta


def _due_action_subject_projection(
    session: Session, notification: DueActionNotification
) -> Any | None:
    if notification.subject_kind == "constraint":
        dependency = session.get(Dependency, notification.dependency_id)
        if dependency is None or dependency.dismissed_at is not None:
            return None
        return dependency
    return session.get(CommitmentLineage, notification.commitment_lineage_id)


def _statement_still_live(session: Session, commitment_lineage_id: int | None) -> bool:
    from corridor.dependency_events import closed_party_commitment_lineages
    from corridor.statement_lifecycle import observe_current_statement

    if commitment_lineage_id is None:
        return False
    lineage = session.get(CommitmentLineage, commitment_lineage_id)
    if lineage is None or lineage.plan_needs_review:
        return False
    if observe_current_statement(session, commitment_lineage_id) is None:
        return False
    closed = closed_party_commitment_lineages(session, lineage.project_id)
    return commitment_lineage_id not in closed


# --- Reads: recipient inbox and operations delivery view -------------------


def due_action_inbox(
    session: Session, *, project_id: int, principal_subject: str
) -> list[dict[str, Any]]:
    """One member's due-action notifications in this project, with standing.

    Scoped to the signed-in member and the one project, so no other project's
    records or another person's reminders are exposed.
    """
    rows = outgoing_dispatch.paired_rows(
        session,
        DueActionNotification,
        DueActionNotificationDispatch,
        project_id=project_id,
        principal_subject=principal_subject,
    )
    return [
        {
            "notification_id": notification.id,
            "public_id": notification.public_id,
            "category": notification.category,
            "urgency": notification.urgency,
            "recipient_role": notification.recipient_role,
            "subject_label": _due_action_subject_summary(session, notification)[
                "subject_label"
            ],
            "action_due_date": (
                notification.action_due_date.isoformat()
                if notification.action_due_date is not None
                else None
            ),
            "observation_window": _observation_window(notification),
            "delivery_state": dispatch.delivery_state,
            "delivery_status": _human_due_action_status(dispatch),
            "delivery_limitation": dispatch.delivery_limitation,
        }
        for notification, dispatch in rows
    ]


def due_action_operations_view(
    session: Session, *, project_id: int
) -> dict[str, Any]:
    """Every due-action delivery in one project, plus escalation configuration.

    Distinguishes delivery standing with subject context and any limitation,
    reports whether real delivery is enabled by a recorded gate-7 configuration,
    and makes the escalation configuration visible — including, when it is
    missing, the reason escalation is disabled (ADR-0034 decision 48).
    """
    rows = outgoing_dispatch.paired_rows(
        session,
        DueActionNotification,
        DueActionNotificationDispatch,
        project_id=project_id,
    )
    schedule = outgoing_dispatch.enabled_schedule(
        session, project_id=project_id, handler_key=DUE_ACTION_NOTIFICATION_HANDLER
    )
    view = outgoing_dispatch.operations_projection(
        rows,
        delivery_enabled=schedule is not None,
        row=lambda notification, dispatch: {
            "notification_id": notification.id,
            "public_id": notification.public_id,
            "category": notification.category,
            "urgency": notification.urgency,
            "recipient_role": notification.recipient_role,
            "recipient": notification.recipient_principal_subject,
            "subject_label": _due_action_subject_summary(session, notification)[
                "subject_label"
            ],
            "delivery_state": dispatch.delivery_state,
            "delivery_status": _human_due_action_status(dispatch),
            "delivery_limitation": dispatch.delivery_limitation,
            "attempt_count": dispatch.attempt_count,
            "last_error_code": dispatch.last_error_code,
        },
    )
    view["escalation"] = _escalation_configuration(session, schedule)
    return view


def _escalation_configuration(
    session: Session, schedule: DueWorkSchedule | None
) -> dict[str, Any]:
    """Whether escalation is enabled for this project, or why it is disabled."""
    if schedule is None:
        return {"enabled": False, "reason": "no due-action schedule is enabled"}
    roster_entry_id = schedule.scope_json.get("escalation_roster_entry_id")
    recipient, reason = _resolve_escalation_recipient(
        session,
        project_id=schedule.project_id,
        roster_entry_id=roster_entry_id,
    )
    if recipient is None:
        return {"enabled": False, "reason": reason}
    return {
        "enabled": True,
        "reason": None,
        "escalation_roster_entry_id": recipient.roster_entry_id,
        "urgent_overdue_days": schedule.scope_json.get("urgent_overdue_days"),
    }


def _observation_window(notification: DueActionNotification) -> dict[str, str | None]:
    return {
        "start": (
            notification.observation_start.isoformat()
            if notification.observation_start is not None
            else None
        ),
        "end": (
            notification.observation_end.isoformat()
            if notification.observation_end is not None
            else None
        ),
    }


def _human_due_action_status(dispatch: DueActionNotificationDispatch) -> str:
    return outgoing_dispatch.human_delivery_status(
        dispatch,
        {
            LIMITATION_PLAN_SUPERSEDED: "the plan changed before delivery",
            LIMITATION_ACTION_REASSIGNED: "the action was reassigned before delivery",
            LIMITATION_DEFERRED: "the item was deferred before delivery",
            LIMITATION_CONDITION_CLEARED: "the condition cleared before delivery",
            LIMITATION_ESCALATION_DEDUPLICATED: (
                "the escalation contact already holds the action"
            ),
        },
    )


def _due_action_summary_payload(
    notification: DueActionNotification,
) -> dict[str, Any]:
    payload = dict(notification.summary_json or {})
    payload["subject_label"] = "Daily summary"
    payload["category"] = notification.category
    payload["recipient_role"] = notification.recipient_role
    return payload


def _due_action_subject_summary(
    session: Session, notification: DueActionNotification
) -> dict[str, Any]:
    if notification.category == CATEGORY_DAILY_SUMMARY:
        return {"subject_label": "Daily summary", "subject_ref": None}
    if notification.subject_kind == "constraint":
        dependency = session.get(Dependency, notification.dependency_id)
        label = dependency.ref_code if dependency is not None else "constraint"
        return {
            "subject_label": f"Constraint {label}",
            "subject_ref": label,
            "category": notification.category,
            "urgency": notification.urgency,
            "action_due_date": (
                notification.action_due_date.isoformat()
                if notification.action_due_date is not None
                else None
            ),
            "recipient_role": notification.recipient_role,
        }
    lineage = session.get(CommitmentLineage, notification.commitment_lineage_id)
    label = f"Commitment {lineage.id}" if lineage is not None else "statement commitment"
    return {
        "subject_label": label,
        "subject_ref": str(notification.commitment_lineage_id),
        "category": notification.category,
        "urgency": notification.urgency,
        "action_due_date": (
            notification.action_due_date.isoformat()
            if notification.action_due_date is not None
            else None
        ),
        "recipient_role": notification.recipient_role,
    }
