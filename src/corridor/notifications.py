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
  runtime's server-owned handler calls.  It rechecks that the assignment is
  still current (reassignment, revoked membership, changed contact) before every
  send, does its provider I/O holding no project mutation lock, and retains the
  provider result, bounded retry state, and an explicit uncertain outcome.  It
  uses a stable per-dispatch idempotency key but never claims exactly-once.

This module deliberately does not import the assignment writers (they import it),
the runtime, or the web layer; it reaches the current-assignment fact through the
same ``WorkDecision`` tail the writers project from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from corridor import access, audit
from corridor.models import (
    AssignmentNotification,
    AssignmentNotificationAttempt,
    AssignmentNotificationDispatch,
    AssignmentNotificationFeedback,
    CommitmentLineage,
    Dependency,
    DueWorkSchedule,
    PersonIdentity,
    ProjectRosterEntry,
    WorkDecision,
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

# Visible delivery limitations. Each keeps the assignment intact and only
# explains why a message could not be delivered as current.
LIMITATION_UNRESOLVED_CONTACT = "unresolved_contact"
LIMITATION_REVOKED_MEMBERSHIP = "revoked_membership"
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
        contact, limitation = _resolve_contact(session, recipient_principal)
        idempotency_key = _sha256(
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


def _resolve_contact(
    session: Session, recipient_principal: str
) -> tuple[str | None, str | None]:
    """The verified contact for a principal, or a typed delivery limitation.

    Contact resolves only through the verified-contact record for the roster
    identity's principal. There is no display-name match, no free-text assignee
    inference, and no guessed address: an unresolved mapping is a visible
    limitation, never a fabricated recipient.
    """
    identity = session.scalar(
        select(PersonIdentity).where(
            PersonIdentity.principal_subject == recipient_principal
        )
    )
    if identity is None:
        return None, LIMITATION_UNRESOLVED_CONTACT
    return identity.email_normalized, None


def _occurrence_key(
    *,
    subject_identity: tuple[str, int],
    assignment_decision_id: int,
    recipient_principal: str,
) -> str:
    return _sha256(
        {
            "category": CATEGORY_NEW_ASSIGNMENT,
            "subject_kind": subject_identity[0],
            "subject_id": subject_identity[1],
            "assignment_decision_id": assignment_decision_id,
            "recipient_principal": recipient_principal,
        }
    )


# --- Delivery adapter seam ------------------------------------------------


@dataclass(frozen=True)
class DeliveryRequest:
    """What a delivery adapter is handed; carries no cross-project secret."""

    channel: str
    recipient_contact: str
    idempotency_key: str
    subject_summary: dict[str, Any]


@dataclass(frozen=True)
class DeliveryOutcome:
    """A provider's typed result for one delivery attempt.

    ``status`` is ``completed`` only with provider acknowledgment, ``uncertain``
    when acknowledgment is unavailable, and ``failed`` on a definite rejection.
    """

    status: str
    provider_message_id: str | None = None
    provider_result: dict[str, Any] | None = None
    error_code: str | None = None
    retryable: bool = True


@runtime_checkable
class DeliveryAdapter(Protocol):
    """The replaceable provider seam; tests inject a non-sending capture."""

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome: ...


class DisabledDeliveryAdapter:
    """The default: it never sends, so completing the code enables no delivery.

    Real activation is a deployment concern — a human wires a real adapter after
    recording the gate-7 configuration. Until then every attempt fails visibly
    rather than silently mailing, so credentials or a schedule alone deliver
    nothing.
    """

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        return DeliveryOutcome(
            status="failed",
            error_code="delivery_adapter_not_configured",
            retryable=False,
            provider_result={"delivered": False, "reason": "adapter_not_configured"},
        )


class RecordingDeliveryAdapter:
    """In-memory capture for tests; holds requests without ever sending them."""

    def __init__(
        self,
        outcome: Callable[[DeliveryRequest], DeliveryOutcome] | DeliveryOutcome | None = None,
    ) -> None:
        self.sent: list[DeliveryRequest] = []
        self._outcome = outcome

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        self.sent.append(request)
        if callable(self._outcome):
            return self._outcome(request)
        if isinstance(self._outcome, DeliveryOutcome):
            return self._outcome
        return DeliveryOutcome(
            status="completed",
            provider_message_id=f"fake-{request.idempotency_key[:16]}",
            provider_result={"delivered": True},
        )

    def deliveries_for(self, idempotency_key: str) -> list[DeliveryRequest]:
        return [item for item in self.sent if item.idempotency_key == idempotency_key]


_DELIVERY_ADAPTERS: dict[str, DeliveryAdapter] = {}


def register_delivery_adapter(channel: str, adapter: DeliveryAdapter) -> None:
    """Wire a real (or, in tests, a recording) adapter for one channel."""

    _DELIVERY_ADAPTERS[channel] = adapter


def clear_delivery_adapters() -> None:
    """Reset the wired adapters; tests call this on teardown."""

    _DELIVERY_ADAPTERS.clear()


def resolve_delivery_adapter(channel: str) -> DeliveryAdapter:
    """The wired adapter for a channel, or the non-sending default."""

    return _DELIVERY_ADAPTERS.get(channel, DisabledDeliveryAdapter())


# --- Delivery sweep -------------------------------------------------------


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

    Each dispatch is processed in its own short transactions and the provider
    call holds no transaction at all, so provider I/O never spans a project
    mutation lock. Before every send the assignment is re-checked for currency;
    an obsolete assignment is never presented as current. Bounded by ``budget``.
    """
    now = _aware_utc(clock.now())
    with session_factory() as reading:
        dispatch_ids = list(
            reading.scalars(
                select(AssignmentNotificationDispatch.id)
                .where(
                    AssignmentNotificationDispatch.project_id == project_id,
                    AssignmentNotificationDispatch.channel == channel,
                    or_(
                        AssignmentNotificationDispatch.delivery_state == "queued",
                        (
                            (AssignmentNotificationDispatch.delivery_state == "retry_due")
                            & (
                                AssignmentNotificationDispatch.next_attempt_at
                                <= now
                            )
                        ),
                    ),
                )
                .order_by(AssignmentNotificationDispatch.id)
                .limit(budget)
            ).all()
        )

    counts = {
        "considered": 0,
        "completed": 0,
        "retry_due": 0,
        "failed": 0,
        "uncertain": 0,
        "skipped": 0,
    }
    for dispatch_id in dispatch_ids:
        counts["considered"] += 1
        with session_factory() as checking:
            with checking.begin():
                dispatch = checking.get(
                    AssignmentNotificationDispatch, dispatch_id, with_for_update=True
                )
                if dispatch is None or dispatch.channel != channel:
                    continue
                if not _is_deliverable(dispatch, now=now):
                    continue
                notification = checking.get(
                    AssignmentNotification, dispatch.notification_id
                )
                status, contact, limitation = _assignment_currency(checking, notification)
                summary = _subject_summary(checking, notification)
                idempotency_key = dispatch.idempotency_key
                attempt_from = dispatch.attempt_count
                if status != "current":
                    _finalize_limitation(
                        checking,
                        dispatch,
                        limitation=limitation,
                        benign=(status == "superseded"),
                        owner=owner,
                        now=_aware_utc(clock.now()),
                    )
                    counts["skipped"] += 1
                    continue

        if status != "current":
            continue

        try:
            outcome = adapter.deliver(
                DeliveryRequest(
                    channel=channel,
                    recipient_contact=contact,
                    idempotency_key=idempotency_key,
                    subject_summary=summary,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one dispatch must not abort the pass
            # A provider error is a retryable delivery failure for this one
            # dispatch, retained like any other; it never blocks the others and
            # never claims a send happened.
            outcome = DeliveryOutcome(
                status="failed",
                error_code="delivery_provider_error",
                retryable=True,
                provider_result={"delivered": False, "error": str(exc)[:200]},
            )

        with session_factory() as recording:
            with recording.begin():
                dispatch = recording.get(
                    AssignmentNotificationDispatch, dispatch_id, with_for_update=True
                )
                if dispatch is None or dispatch.attempt_count != attempt_from:
                    # Another recovery worker already finalized this attempt.
                    continue
                new_state = _record_outcome(
                    recording,
                    dispatch,
                    contact=contact,
                    outcome=outcome,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff_seconds,
                    owner=owner,
                    now=_aware_utc(clock.now()),
                )
        counts[new_state] = counts.get(new_state, 0) + 1

    delivery_enabled = _delivery_enabled(session_factory, project_id)
    return summarize_delivery_pass(
        counts,
        project_id=project_id,
        configuration_version=configuration_version,
        delivery_enabled=delivery_enabled,
        observed_at=_aware_utc(clock.now()),
    )


def summarize_delivery_pass(
    counts: dict[str, int],
    *,
    project_id: int,
    configuration_version: str,
    delivery_enabled: bool,
    observed_at: datetime,
) -> dict[str, Any]:
    """One bounded, credential-free receipt body for a delivery pass."""

    attention = counts.get("failed", 0) > 0 or counts.get("uncertain", 0) > 0
    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "project_id": project_id,
        "configuration_version": configuration_version,
        "observed_at": _iso(observed_at),
        "health": "delivery_attention_required" if attention else "healthy",
        "delivery_enabled": bool(delivery_enabled),
        "considered": int(counts.get("considered", 0)),
        "completed": int(counts.get("completed", 0)),
        "retry_due": int(counts.get("retry_due", 0)),
        "failed": int(counts.get("failed", 0)),
        "uncertain": int(counts.get("uncertain", 0)),
        "skipped": int(counts.get("skipped", 0)),
    }


def _is_deliverable(dispatch: AssignmentNotificationDispatch, *, now: datetime) -> bool:
    if dispatch.delivery_state == "queued":
        return True
    return (
        dispatch.delivery_state == "retry_due"
        and dispatch.next_attempt_at is not None
        and _aware_utc(dispatch.next_attempt_at) <= now
    )


def _assignment_currency(
    session: Session, notification: AssignmentNotification
) -> tuple[str, str | None, str | None]:
    """Re-check the assignment before dispatch; never present an obsolete one.

    Returns ``(status, contact, limitation)`` where status is ``current``,
    ``superseded`` (reassigned or undone), ``revoked`` (membership withdrawn), or
    ``unresolved`` (no verified contact).
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
        return "superseded", None, LIMITATION_REASSIGNED
    membership = access.resolve_membership(
        session, notification.recipient_principal_subject, notification.project_id
    )
    if membership is None:
        return "revoked", None, LIMITATION_REVOKED_MEMBERSHIP
    contact, limitation = _resolve_contact(
        session, notification.recipient_principal_subject
    )
    if contact is None:
        return "unresolved", None, limitation
    return "current", contact, None


def _record_outcome(
    session: Session,
    dispatch: AssignmentNotificationDispatch,
    *,
    contact: str,
    outcome: DeliveryOutcome,
    max_attempts: int,
    backoff_seconds: int,
    owner: str,
    now: datetime,
) -> str:
    attempt_number = dispatch.attempt_count + 1
    dispatch.attempt_count = attempt_number
    dispatch.recipient_contact = contact
    dispatch.delivery_limitation = None
    dispatch.provider_message_id = outcome.provider_message_id
    dispatch.provider_result_json = outcome.provider_result

    if outcome.status == "completed":
        dispatch.delivery_state = "completed"
        dispatch.last_error_code = None
        dispatch.next_attempt_at = None
        attempt_outcome = "completed"
    else:
        error_code = outcome.error_code or (
            "acknowledgment_unavailable"
            if outcome.status == "uncertain"
            else "delivery_failed"
        )
        dispatch.last_error_code = error_code
        attempt_outcome = "uncertain" if outcome.status == "uncertain" else "failed"
        if outcome.retryable and attempt_number < max_attempts:
            dispatch.delivery_state = "retry_due"
            dispatch.next_attempt_at = now + timedelta(
                seconds=backoff_seconds * (2 ** (attempt_number - 1))
            )
        else:
            dispatch.delivery_state = (
                "uncertain" if outcome.status == "uncertain" else "failed"
            )
            dispatch.next_attempt_at = None
    session.flush([dispatch])
    _append_attempt(
        session,
        dispatch,
        attempt_number=attempt_number,
        outcome=attempt_outcome,
        recipient_contact=contact,
        limitation=None,
        provider_message_id=outcome.provider_message_id,
        provider_result=outcome.provider_result,
        error_code=dispatch.last_error_code,
        owner=owner,
        now=now,
    )
    return dispatch.delivery_state


def _finalize_limitation(
    session: Session,
    dispatch: AssignmentNotificationDispatch,
    *,
    limitation: str | None,
    benign: bool,
    owner: str,
    now: datetime,
) -> None:
    """Terminally record why a re-checked assignment was not delivered."""

    attempt_number = dispatch.attempt_count + 1
    dispatch.attempt_count = attempt_number
    dispatch.delivery_state = "failed"
    dispatch.delivery_limitation = limitation
    dispatch.last_error_code = limitation
    dispatch.next_attempt_at = None
    session.flush([dispatch])
    _append_attempt(
        session,
        dispatch,
        attempt_number=attempt_number,
        outcome="skipped",
        recipient_contact=None,
        limitation=limitation,
        provider_message_id=None,
        provider_result=None,
        error_code=limitation,
        owner=owner,
        now=now,
    )


def _append_attempt(
    session: Session,
    dispatch: AssignmentNotificationDispatch,
    *,
    attempt_number: int,
    outcome: str,
    recipient_contact: str | None,
    limitation: str | None,
    provider_message_id: str | None,
    provider_result: dict[str, Any] | None,
    error_code: str | None,
    owner: str,
    now: datetime,
) -> AssignmentNotificationAttempt:
    attempt = AssignmentNotificationAttempt(
        public_id=f"assignment-attempt:{uuid4().hex[:24]}",
        dispatch_id=dispatch.id,
        project_id=dispatch.project_id,
        attempt_number=attempt_number,
        outcome=outcome,
        recipient_contact=recipient_contact,
        delivery_limitation=limitation,
        provider_message_id=provider_message_id,
        provider_result_json=provider_result,
        error_code=error_code,
        runtime_owner=owner,
        observed_at=now,
    )
    session.add(attempt)
    session.flush([attempt])
    return attempt


def _delivery_enabled(session_factory, project_id: int) -> bool:
    with session_factory() as reading:
        schedule = reading.scalar(
            select(DueWorkSchedule.id).where(
                DueWorkSchedule.project_id == project_id,
                DueWorkSchedule.handler_key == ASSIGNMENT_NOTIFICATION_HANDLER,
                DueWorkSchedule.disabled_at.is_(None),
            )
        )
    return schedule is not None


# --- Reads: recipient inbox and operations delivery view ------------------


def recipient_inbox(
    session: Session, *, project_id: int, principal_subject: str
) -> list[dict[str, Any]]:
    """One member's new-assignment notifications in this project, with standing.

    Scoped to the signed-in member and the one project, so no other project's
    records or another person's assignments are exposed.
    """
    rows = session.execute(
        select(AssignmentNotification, AssignmentNotificationDispatch)
        .join(
            AssignmentNotificationDispatch,
            AssignmentNotificationDispatch.notification_id
            == AssignmentNotification.id,
        )
        .where(
            AssignmentNotification.project_id == project_id,
            AssignmentNotification.recipient_principal_subject == principal_subject,
        )
        .order_by(AssignmentNotification.id.desc())
    ).all()
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
    rows = session.execute(
        select(AssignmentNotification, AssignmentNotificationDispatch)
        .join(
            AssignmentNotificationDispatch,
            AssignmentNotificationDispatch.notification_id
            == AssignmentNotification.id,
        )
        .where(AssignmentNotification.project_id == project_id)
        .order_by(AssignmentNotification.id.desc())
    ).all()
    counts = {state: 0 for state in ("queued", "completed", "retry_due", "failed", "uncertain")}
    deliveries = []
    for notification, dispatch in rows:
        counts[dispatch.delivery_state] = counts.get(dispatch.delivery_state, 0) + 1
        deliveries.append(_operations_row(session, notification, dispatch))
    enabled = (
        session.scalar(
            select(DueWorkSchedule.id).where(
                DueWorkSchedule.project_id == project_id,
                DueWorkSchedule.handler_key == ASSIGNMENT_NOTIFICATION_HANDLER,
                DueWorkSchedule.disabled_at.is_(None),
            )
        )
        is not None
    )
    return {
        "delivery_enabled": enabled,
        "counts": counts,
        "deliveries": deliveries,
    }


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
    if dispatch.delivery_state == "queued":
        if dispatch.delivery_limitation == LIMITATION_UNRESOLVED_CONTACT:
            return "queued (no verified contact yet)"
        return "queued for delivery"
    if dispatch.delivery_state == "completed":
        return "delivered"
    if dispatch.delivery_state == "retry_due":
        return "retry scheduled"
    if dispatch.delivery_state == "uncertain":
        return "delivery not acknowledged"
    if dispatch.delivery_limitation == LIMITATION_REASSIGNED:
        return "assignment changed before delivery"
    if dispatch.delivery_limitation == LIMITATION_REVOKED_MEMBERSHIP:
        return "recipient is no longer a project member"
    if dispatch.delivery_limitation == LIMITATION_UNRESOLVED_CONTACT:
        return "no verified contact for the recipient"
    return "delivery failed"


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


# --- Small shared helpers -------------------------------------------------


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise NotificationRefusal("a notification clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
