"""One outbound-dispatch state machine for every notification family.

Three copies of this machine were built and they drifted. #351 wrote it for
new-assignment notifications; #352 appended the due-action families to the same
file and reused about half of it by adding ``attempt_model`` and
``public_prefix`` parameters to three private helpers; #353 needed the same
machine over a third table triple, could not reach those private helpers from
another module, and copied them. What ended up duplicated was not incidental
glue but the delivery contract itself: the ``queued OR (retry_due AND
next_attempt_at <= now)`` queue selection with its ``order_by(id).limit(budget)``
budget, the short-transaction discipline that keeps provider I/O outside every
project mutation lock, the ``backoff_seconds * 2 ** (n - 1)`` retry arithmetic,
the terminal-limitation finalization, the six-key counts dict, and the receipt
body. Two of the copies had already reached different answers for one
append-only attempt identity (see ``attempt_public_id``), which is the exact
defect ADR-0089 names: "two definitions of one identity is the defect regardless
of which is currently right".

So this module owns the machine once, and a notification family contributes only
what is genuinely its own. The per-category interface is one
:class:`DispatchCategory`: the three ORM classes of its table triple, its
handler key and receipt schema version, a **currency re-check** that returns one
common :class:`Currency` shape, and a **subject summary** for the recipient.
Discovery, registration, and the category's own limitation vocabulary stay with
the family, because those are what the families actually differ in.

The replaceable provider seam lives here too, unchanged: :class:`DeliveryAdapter`
with :class:`DisabledDeliveryAdapter` as the default, so completing this code
enables no real delivery. A feature-local mail queue remains rejected for the
reason #351 gave — it would duplicate the leases, retries, clock, and crash
recovery the one supervised Due Work runtime (#332) already owns.

This module imports no notification family and no runtime: the families and the
runtime depend on it, never the reverse.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from corridor import access, digests
from corridor.models import DueWorkSchedule, PersonIdentity


# The one status that sends. Every other status a category returns withholds the
# message and records why.
CURRENT = "current"

# The delivery limitations that belong to the recipient rather than to any one
# category's condition, so every family reports them in the same words.
LIMITATION_UNRESOLVED_CONTACT = "unresolved_contact"
LIMITATION_REVOKED_MEMBERSHIP = "revoked_membership"

# The persisted dispatch states, in the order an operations reader counts them.
DELIVERY_STATES = ("queued", "completed", "retry_due", "failed", "uncertain")


class DispatchRefusal(ValueError):
    """An outbound-dispatch act was refused; nothing was written."""


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
        outcome: Callable[[DeliveryRequest], DeliveryOutcome]
        | DeliveryOutcome
        | None = None,
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


# --- The per-category interface -------------------------------------------


@dataclass(frozen=True)
class Currency:
    """One shape for every category's pre-send re-check.

    Either the occurrence is still ``current`` and carries the verified
    ``contact`` it may be sent to, or it is withheld under the category's own
    ``status`` with the typed ``limitation`` that explains it. ``benign`` says a
    withheld outcome is an expected non-send — a superseded plan, a
    reassignment, a resolved condition — counted as skipped rather than as an
    operator-visible failure. The three copies expressed this as a 3-tuple, a
    4-tuple, and a 3-tuple with a separate benign-limitation frozenset.
    """

    status: str
    contact: str | None = None
    limitation: str | None = None
    benign: bool = False

    @classmethod
    def current(cls, contact: str) -> Currency:
        """The occurrence still holds and may be sent to this contact."""
        return cls(CURRENT, contact=contact)

    @classmethod
    def withheld(cls, status: str, limitation: str, *, benign: bool = False) -> Currency:
        """The occurrence is no longer current; nothing is sent and why is kept."""
        return cls(status, limitation=limitation, benign=benign)

    @property
    def is_current(self) -> bool:
        return self.status == CURRENT


@dataclass(frozen=True)
class DispatchCategory:
    """Everything one notification family contributes to the shared machine.

    ``name`` is the family's own short name; it derives the attempt ``public_id``
    prefix so the identity has one derivation rather than a repeated literal.
    """

    name: str
    handler_key: str
    notification_model: type
    dispatch_model: type
    attempt_model: type
    result_schema_version: str
    currency: Callable[[Session, Any], Currency]
    subject_summary: Callable[[Session, Any], dict[str, Any]]

    @property
    def attempt_public_prefix(self) -> str:
        return f"{self.name}-attempt"


# --- Recipient resolution, shared by every category -----------------------


def resolve_contact(
    session: Session, recipient_principal: str
) -> tuple[str | None, str | None]:
    """The verified contact for a principal, or a typed delivery limitation.

    Contact resolves only through the typed verified-contact record
    (``PersonIdentity``). There is no display-name match, no free-text assignee
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


def withheld_recipient(
    session: Session, *, project_id: int, principal_subject: str
) -> Currency | None:
    """The withheld outcome for a recipient who may not be mailed, or ``None``.

    A person who left the project is never mailed, whatever their role on the
    occurrence was.
    """
    membership = access.resolve_membership(session, principal_subject, project_id)
    if membership is None:
        return Currency.withheld("revoked", LIMITATION_REVOKED_MEMBERSHIP)
    return None


def contact_currency(
    session: Session, principal_subject: str
) -> tuple[str | None, Currency | None]:
    """``(contact, None)``, or ``(None, withheld)`` when no contact is verified."""
    contact, limitation = resolve_contact(session, principal_subject)
    if contact is None:
        return None, Currency.withheld("unresolved", limitation or LIMITATION_UNRESOLVED_CONTACT)
    return contact, None


# --- Delivery sweep -------------------------------------------------------


def deliver_pass(
    session_factory,
    category: DispatchCategory,
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
    """Deliver one project's due dispatches for one category through the adapter.

    Each dispatch is processed in its own short transactions and the provider
    call holds no transaction at all, so provider I/O never spans a project
    mutation lock. Before every send the category re-checks that its occurrence
    is still current; an obsolete occurrence is never presented as current, and
    its earlier delivery history is never deleted (ADR-0032). A stable
    per-dispatch idempotency key is passed to the provider, but nothing here
    claims exactly-once. Bounded by ``budget``.
    """
    now = aware_utc(clock.now())
    dispatch_model = category.dispatch_model
    with session_factory() as reading:
        dispatch_ids = list(
            reading.scalars(
                select(dispatch_model.id)
                .where(
                    dispatch_model.project_id == project_id,
                    dispatch_model.channel == channel,
                    or_(
                        dispatch_model.delivery_state == "queued",
                        (
                            (dispatch_model.delivery_state == "retry_due")
                            & (dispatch_model.next_attempt_at <= now)
                        ),
                    ),
                )
                .order_by(dispatch_model.id)
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
        sending: tuple[str, dict[str, Any], str, int] | None = None
        with session_factory() as checking:
            with checking.begin():
                dispatch = checking.get(
                    dispatch_model, dispatch_id, with_for_update=True
                )
                if dispatch is None or dispatch.channel != channel:
                    continue
                if not is_deliverable(dispatch, now=now):
                    continue
                notification = checking.get(
                    category.notification_model, dispatch.notification_id
                )
                currency = category.currency(checking, notification)
                if currency.is_current:
                    sending = (
                        currency.contact,
                        category.subject_summary(checking, notification),
                        dispatch.idempotency_key,
                        dispatch.attempt_count,
                    )
                else:
                    finalize_limitation(
                        checking,
                        dispatch,
                        category=category,
                        limitation=currency.limitation,
                        owner=owner,
                        now=aware_utc(clock.now()),
                    )
                    # A benignly stale occurrence is a skip the operator need not
                    # act on; a revoked membership or an unresolved contact is a
                    # limitation they should see.
                    counts["skipped" if currency.benign else "failed"] += 1
        if sending is None:
            continue
        contact, summary, idempotency_key, attempt_from = sending

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
                    dispatch_model, dispatch_id, with_for_update=True
                )
                if dispatch is None or dispatch.attempt_count != attempt_from:
                    # Another recovery worker already finalized this attempt.
                    continue
                new_state = record_outcome(
                    recording,
                    dispatch,
                    category=category,
                    contact=contact,
                    outcome=outcome,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff_seconds,
                    owner=owner,
                    now=aware_utc(clock.now()),
                )
        counts[new_state] = counts.get(new_state, 0) + 1

    return summarize_pass(
        counts,
        schema_version=category.result_schema_version,
        project_id=project_id,
        configuration_version=configuration_version,
        delivery_enabled=delivery_enabled(
            session_factory, project_id=project_id, handler_key=category.handler_key
        ),
        observed_at=aware_utc(clock.now()),
    )


def summarize_pass(
    counts: Mapping[str, int],
    *,
    schema_version: str,
    project_id: int,
    configuration_version: str,
    delivery_enabled: bool,
    observed_at: datetime,
) -> dict[str, Any]:
    """One bounded, credential-free receipt body for a delivery pass."""

    attention = counts.get("failed", 0) > 0 or counts.get("uncertain", 0) > 0
    return {
        "schema_version": schema_version,
        "project_id": project_id,
        "configuration_version": configuration_version,
        "observed_at": iso(observed_at),
        "health": "delivery_attention_required" if attention else "healthy",
        "delivery_enabled": bool(delivery_enabled),
        "considered": int(counts.get("considered", 0)),
        "completed": int(counts.get("completed", 0)),
        "retry_due": int(counts.get("retry_due", 0)),
        "failed": int(counts.get("failed", 0)),
        "uncertain": int(counts.get("uncertain", 0)),
        "skipped": int(counts.get("skipped", 0)),
    }


def is_deliverable(dispatch: Any, *, now: datetime) -> bool:
    """Whether this dispatch is due right now: queued, or a retry that has come."""
    if dispatch.delivery_state == "queued":
        return True
    return (
        dispatch.delivery_state == "retry_due"
        and dispatch.next_attempt_at is not None
        and aware_utc(dispatch.next_attempt_at) <= now
    )


def record_outcome(
    session: Session,
    dispatch: Any,
    *,
    category: DispatchCategory,
    contact: str,
    outcome: DeliveryOutcome,
    max_attempts: int,
    backoff_seconds: int,
    owner: str,
    now: datetime,
) -> str:
    """Record one provider result and return the dispatch's new delivery state.

    A retryable failure inside the attempt budget schedules an exponentially
    backed-off retry; exhaustion or a non-retryable rejection is terminal, and an
    unacknowledged send stays explicitly ``uncertain`` rather than being called
    either delivered or failed.
    """
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
    append_attempt(
        session,
        dispatch,
        category=category,
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


def finalize_limitation(
    session: Session,
    dispatch: Any,
    *,
    category: DispatchCategory,
    limitation: str | None,
    owner: str,
    now: datetime,
) -> None:
    """Terminally record why a re-checked occurrence was not delivered.

    The dispatch reaches a terminal ``failed`` state carrying the typed
    limitation, and the append-only attempt records a ``skipped`` outcome:
    nothing was sent, so the delivery is never marked completed. The delivery
    pass decides from :class:`Currency` whether this is a benign withheld
    message counted as skipped or an operator-visible problem. The earlier
    delivery history is never deleted (ADR-0032).
    """
    attempt_number = dispatch.attempt_count + 1
    dispatch.attempt_count = attempt_number
    dispatch.delivery_state = "failed"
    dispatch.delivery_limitation = limitation
    dispatch.last_error_code = limitation
    dispatch.next_attempt_at = None
    session.flush([dispatch])
    append_attempt(
        session,
        dispatch,
        category=category,
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


def append_attempt(
    session: Session,
    dispatch: Any,
    *,
    category: DispatchCategory,
    attempt_number: int,
    outcome: str,
    recipient_contact: str | None,
    limitation: str | None,
    provider_message_id: str | None,
    provider_result: dict[str, Any] | None,
    error_code: str | None,
    owner: str,
    now: datetime,
) -> Any:
    """Append one immutable per-attempt receipt to the category's attempt table."""
    attempt = category.attempt_model(
        public_id=attempt_public_id(
            category, dispatch_id=dispatch.id, attempt_number=attempt_number
        ),
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


def attempt_public_id(
    category: DispatchCategory, *, dispatch_id: int, attempt_number: int
) -> str:
    """The one derivation of an attempt's public identity.

    There were two. #351 minted ``uuid4().hex[:24]``; #353's copy digested
    ``{'dispatch', 'n'}``. Both named the same append-only fact, which is the
    defect ADR-0089 states plainly: two definitions of one identity is the
    defect regardless of which is currently right. The digest is the one that
    survives, for the reason that ADR gives for preferring an identity a
    database re-derives from the row's own columns — every attempt table
    already constrains ``(dispatch_id, attempt_number)`` uniquely, so deriving
    the public identity from exactly those two columns means one attempt has one
    public id, a competing worker cannot mint a second identity for it, and the
    identity can be recomputed from the row rather than looked up. Existing
    stored ids stay valid: a ``public_id`` is an opaque string, and nothing
    recomputes a historical one to compare it.
    """
    return (
        f"{category.attempt_public_prefix}:"
        f"{digests.canonical_sha256({'dispatch_id': dispatch_id, 'attempt_number': attempt_number})[:24]}"
    )


# --- Schedule and projection shapes shared by the reads -------------------


def enabled_schedule(
    session: Session, *, project_id: int, handler_key: str
) -> DueWorkSchedule | None:
    """This project's enabled Due Work schedule for one handler, if any.

    An enabled schedule is what makes real delivery possible at all, so every
    operations view reports its presence as ``delivery_enabled``.
    """
    return session.scalar(
        select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == project_id,
            DueWorkSchedule.handler_key == handler_key,
            DueWorkSchedule.disabled_at.is_(None),
        )
    )


def delivery_enabled(session_factory, *, project_id: int, handler_key: str) -> bool:
    """Whether a recorded gate-7 configuration enables real delivery."""
    with session_factory() as reading:
        return (
            enabled_schedule(reading, project_id=project_id, handler_key=handler_key)
            is not None
        )


def paired_rows(
    session: Session,
    notification_model: type,
    dispatch_model: type,
    *,
    project_id: int,
    principal_subject: str | None = None,
) -> list[tuple[Any, Any]]:
    """Every occurrence in one project with its dispatch, newest first.

    Scoped to the one project, and to one member when ``principal_subject`` is
    given, so no other project's records or another person's notifications are
    exposed.
    """
    query = (
        select(notification_model, dispatch_model)
        .join(
            dispatch_model,
            dispatch_model.notification_id == notification_model.id,
        )
        .where(notification_model.project_id == project_id)
        .order_by(notification_model.id.desc())
    )
    if principal_subject is not None:
        query = query.where(
            notification_model.recipient_principal_subject == principal_subject
        )
    return list(session.execute(query).all())


def operations_projection(
    pairs: Iterable[tuple[Any, Any]],
    *,
    delivery_enabled: bool,
    row: Callable[[Any, Any], dict[str, Any]],
) -> dict[str, Any]:
    """The operator's shape: whether delivery is enabled, state counts, and rows."""
    counts = {state: 0 for state in DELIVERY_STATES}
    deliveries = []
    for notification, dispatch in pairs:
        counts[dispatch.delivery_state] = counts.get(dispatch.delivery_state, 0) + 1
        deliveries.append(row(notification, dispatch))
    return {
        "delivery_enabled": bool(delivery_enabled),
        "counts": counts,
        "deliveries": deliveries,
    }


_SHARED_LIMITATION_PHRASES: Mapping[str, str] = {
    LIMITATION_REVOKED_MEMBERSHIP: "recipient is no longer a project member",
    LIMITATION_UNRESOLVED_CONTACT: "no verified contact for the recipient",
}


def human_delivery_status(
    dispatch: Any, limitation_phrases: Mapping[str, str] | None = None
) -> str:
    """Delivery standing in plain words, including why a message was withheld."""
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
    phrases = {**_SHARED_LIMITATION_PHRASES, **(limitation_phrases or {})}
    return phrases.get(dispatch.delivery_limitation, "delivery failed")


# --- Small shared helpers -------------------------------------------------


def aware_utc(value: datetime) -> datetime:
    """The value as an aware UTC datetime, refusing a naive or non-datetime one."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DispatchRefusal("a notification clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    """The aware UTC ISO-8601 rendering a receipt carries."""
    return aware_utc(value).isoformat()
