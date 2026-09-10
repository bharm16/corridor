"""The one outbound-dispatch state machine, proved once over a fake category.

Three notification families delivered themselves through three copies of this
machine, so each family's runtime suite re-proved the same queue selection,
retry arithmetic, and terminal transitions. Those belong to
``corridor.outgoing_dispatch`` now and are proved here against a category that
exists only in this file: it borrows the assignment table triple for storage but
supplies its own currency re-check and subject summary, so nothing here depends
on what a real family decides is current. Each family's own suite keeps an
end-to-end delivery test and its category-specific assertions.

The pass runs its short transactions through a factory that hands out the
rollback-scoped session, with ``begin`` mapped onto a savepoint, so the
machine's per-dispatch commit discipline is exercised without leaving rows
behind. Crash durability and competing workers, which need genuinely
independent committed transactions, stay in
``tests/test_notifications_runtime.py``.
"""

from datetime import datetime, timedelta, timezone

import pytest

from corridor import digests, outgoing_dispatch
from corridor.due_work import (
    AssignmentNotificationDeclaration,
    configure_assignment_notification,
)
from corridor.models import (
    AssignmentNotification,
    AssignmentNotificationAttempt,
    AssignmentNotificationDispatch,
    Dependency,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
)
from corridor.outgoing_dispatch import (
    Currency,
    DeliveryOutcome,
    DispatchCategory,
    RecordingDeliveryAdapter,
)
from corridor.principals import HumanPrincipal
from corridor.work_decisions import CoordinationSubject, assign_internal_owner

RECORDER = HumanPrincipal("local:dispatch-coordinator")
RECIPIENT = HumanPrincipal("local:dispatch-recipient")
CHANNEL = "email"
OWNER = "local:dispatch-worker"
CONTACT = "dispatch@example.com"

_SCHEMA_VERSION = "fake-category-result-v1"
_HANDLER = "assignment_notification"
_BENIGN_LIMITATION = "condition_cleared"
_VISIBLE_LIMITATION = outgoing_dispatch.LIMITATION_UNRESOLVED_CONTACT


class _NestedSession:
    """The rollback-scoped session, with ``begin`` mapped onto a savepoint."""

    def __init__(self, session):
        self._session = session

    def begin(self):
        return self._session.begin_nested()

    def __getattr__(self, name):
        return getattr(self._session, name)


class _SessionFactory:
    """Hands the same rollback-scoped session to every short transaction."""

    def __init__(self, session):
        self._nested = _NestedSession(session)

    def __call__(self):
        return self

    def __enter__(self):
        return self._nested

    def __exit__(self, *exception):
        return False


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = self.value + timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return ControlledClock(datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def factory(session):
    return _SessionFactory(session)


@pytest.fixture
def project(session):
    project = Project(
        slug="dispatch-machine", name="Dispatch Machine", is_synthetic=True
    )
    session.add(project)
    session.flush()
    session.add(
        PersonIdentity(email_normalized=CONTACT, principal_subject=RECIPIENT.subject)
    )
    session.flush()
    return project


def _category(
    currency=lambda session, notification: Currency.current(CONTACT),
    *,
    summary=lambda session, notification: {"subject_label": "Fake subject"},
) -> DispatchCategory:
    """A category that exists only for this test: its own re-check, borrowed tables."""
    return DispatchCategory(
        name="fake",
        handler_key=_HANDLER,
        notification_model=AssignmentNotification,
        dispatch_model=AssignmentNotificationDispatch,
        attempt_model=AssignmentNotificationAttempt,
        result_schema_version=_SCHEMA_VERSION,
        currency=currency,
        subject_summary=summary,
    )


@pytest.fixture
def roster(session, project):
    entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject=RECIPIENT.subject,
        display_name="Dana Recipient",
        active=True,
        can_coordinate=True,
    )
    session.add(entry)
    session.flush()
    return entry


def _occurrence(
    session, project, roster, *, index: int
) -> AssignmentNotificationDispatch:
    """One immutable occurrence and its queued dispatch, written directly."""
    dependency = Dependency(
        project_id=project.id,
        ref_code=f"OD-{index}",
        dep_type="utility_relocation",
        title=f"Subject {index}",
    )
    session.add(dependency)
    session.flush()
    decision = assign_internal_owner(
        session,
        CoordinationSubject.dependency(dependency.id),
        roster.display_name,
        principal=RECORDER,
    )
    key = digests.canonical_sha256({"fake_occurrence": index, "project": project.id})
    notification = AssignmentNotification(
        public_id=f"fake-notification:{key[:24]}",
        project_id=project.id,
        category="new_assignment",
        subject_kind="constraint",
        dependency_id=dependency.id,
        assignment_decision_id=decision.id,
        recipient_roster_entry_id=roster.id,
        recipient_principal_subject=RECIPIENT.subject,
        occurrence_key=key,
        registered_by=RECORDER.subject,
    )
    session.add(notification)
    session.flush()
    dispatch = AssignmentNotificationDispatch(
        public_id=f"fake-dispatch:{key[:24]}",
        notification_id=notification.id,
        project_id=project.id,
        channel=CHANNEL,
        delivery_state="queued",
        recipient_contact=CONTACT,
        attempt_count=0,
        idempotency_key=digests.canonical_sha256({"dispatch": key}),
    )
    session.add(dispatch)
    session.flush()
    return dispatch


def _deliver(
    factory,
    project,
    clock,
    *,
    adapter,
    category=None,
    max_attempts: int = 3,
    backoff_seconds: int = 10,
    budget: int = 10,
):
    return outgoing_dispatch.deliver_pass(
        factory,
        category if category is not None else _category(),
        project_id=project.id,
        configuration_version="fake-config-v1",
        channel=CHANNEL,
        adapter=adapter,
        clock=clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=budget,
        owner=OWNER,
    )


def _attempts(session, dispatch):
    return sorted(
        session.query(AssignmentNotificationAttempt)
        .filter_by(dispatch_id=dispatch.id)
        .all(),
        key=lambda attempt: attempt.attempt_number,
    )


def test_the_queue_takes_due_dispatches_in_id_order_within_the_budget(
    session, factory, project, roster, clock
):
    first = _occurrence(session, project, roster, index=1)
    second = _occurrence(session, project, roster, index=2)
    third = _occurrence(session, project, roster, index=3)
    adapter = RecordingDeliveryAdapter()

    result = _deliver(factory, project, clock, adapter=adapter, budget=2)

    assert result["considered"] == 2
    assert result["completed"] == 2
    assert [first.delivery_state, second.delivery_state] == ["completed", "completed"]
    assert third.delivery_state == "queued"
    assert len(adapter.sent) == 2


def test_a_scheduled_retry_is_not_deliverable_before_its_time(
    session, factory, project, roster, clock
):
    dispatch = _occurrence(session, project, roster, index=1)
    dispatch.delivery_state = "retry_due"
    dispatch.next_attempt_at = clock.now() + timedelta(seconds=30)
    session.flush()
    adapter = RecordingDeliveryAdapter()

    withheld = _deliver(factory, project, clock, adapter=adapter)
    assert withheld["considered"] == 0
    assert withheld["completed"] == 0
    assert adapter.sent == []
    # The queue selection and the in-transaction re-check enforce one rule, so
    # the second layer refuses the same dispatch the first did not select.
    assert outgoing_dispatch.is_deliverable(dispatch, now=clock.now()) is False

    clock.advance(30)
    assert outgoing_dispatch.is_deliverable(dispatch, now=clock.now()) is True
    due = _deliver(factory, project, clock, adapter=adapter)
    assert due["completed"] == 1
    assert len(adapter.sent) == 1


def test_a_retryable_failure_backs_off_exponentially_then_fails_terminally(
    session, factory, project, roster, clock
):
    dispatch = _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter(
        DeliveryOutcome(status="failed", error_code="provider_busy", retryable=True)
    )
    first_attempt_at = clock.now()

    first = _deliver(factory, project, clock, adapter=adapter)
    assert first["retry_due"] == 1
    assert dispatch.delivery_state == "retry_due"
    assert dispatch.attempt_count == 1
    assert dispatch.last_error_code == "provider_busy"
    assert dispatch.next_attempt_at == first_attempt_at + timedelta(seconds=10)

    clock.advance(10)
    second_attempt_at = clock.now()
    second = _deliver(factory, project, clock, adapter=adapter)
    assert second["retry_due"] == 1
    assert dispatch.attempt_count == 2
    # 10 * 2 ** (2 - 1): the delay doubles with the attempt, from the attempt.
    assert dispatch.next_attempt_at == second_attempt_at + timedelta(seconds=20)

    clock.advance(20)
    third = _deliver(factory, project, clock, adapter=adapter)
    assert third["failed"] == 1
    assert dispatch.delivery_state == "failed"
    assert dispatch.attempt_count == 3
    assert dispatch.next_attempt_at is None
    assert [attempt.outcome for attempt in _attempts(session, dispatch)] == [
        "failed",
        "failed",
        "failed",
    ]


def test_a_non_retryable_rejection_is_terminal_on_the_first_attempt(
    session, factory, project, roster, clock
):
    dispatch = _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter(
        DeliveryOutcome(
            status="failed", error_code="address_rejected", retryable=False
        )
    )

    result = _deliver(factory, project, clock, adapter=adapter)

    assert result["failed"] == 1
    assert dispatch.delivery_state == "failed"
    assert dispatch.attempt_count == 1
    assert dispatch.next_attempt_at is None


def test_an_unacknowledged_send_is_retained_as_uncertain_not_as_delivered(
    session, factory, project, roster, clock
):
    dispatch = _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter(
        DeliveryOutcome(status="uncertain", retryable=False)
    )

    result = _deliver(factory, project, clock, adapter=adapter)

    assert result["uncertain"] == 1
    assert result["completed"] == 0
    assert result["health"] == "delivery_attention_required"
    assert dispatch.delivery_state == "uncertain"
    assert dispatch.last_error_code == "acknowledgment_unavailable"
    assert [attempt.outcome for attempt in _attempts(session, dispatch)] == [
        "uncertain"
    ]


def test_a_provider_error_is_a_retained_retryable_failure_not_a_lost_pass(
    session, factory, project, roster, clock
):
    first = _occurrence(session, project, roster, index=1)
    second = _occurrence(session, project, roster, index=2)

    def _raise_for_the_first(request):
        if request.idempotency_key == first.idempotency_key:
            raise RuntimeError("provider unreachable")
        return DeliveryOutcome(status="completed", provider_message_id="ok")

    adapter = RecordingDeliveryAdapter(_raise_for_the_first)

    result = _deliver(factory, project, clock, adapter=adapter)

    assert result["retry_due"] == 1
    assert result["completed"] == 1
    assert first.delivery_state == "retry_due"
    assert first.last_error_code == "delivery_provider_error"
    assert first.provider_result_json["delivered"] is False
    assert second.delivery_state == "completed"


def test_a_benign_withheld_occurrence_is_a_skip_and_sends_nothing(
    session, factory, project, roster, clock
):
    dispatch = _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter()
    category = _category(
        lambda session, notification: Currency.withheld(
            "cleared", _BENIGN_LIMITATION, benign=True
        )
    )

    result = _deliver(factory, project, clock, adapter=adapter, category=category)

    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["health"] == "healthy"
    assert adapter.sent == []
    assert dispatch.delivery_state == "failed"
    assert dispatch.delivery_limitation == _BENIGN_LIMITATION
    assert dispatch.next_attempt_at is None
    attempt = _attempts(session, dispatch)[0]
    assert attempt.outcome == "skipped"
    assert attempt.recipient_contact is None
    assert attempt.delivery_limitation == _BENIGN_LIMITATION


def test_a_visible_withheld_occurrence_is_counted_as_a_failure(
    session, factory, project, roster, clock
):
    dispatch = _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter()
    category = _category(
        lambda session, notification: Currency.withheld(
            "unresolved", _VISIBLE_LIMITATION
        )
    )

    result = _deliver(factory, project, clock, adapter=adapter, category=category)

    assert result["failed"] == 1
    assert result["skipped"] == 0
    assert result["health"] == "delivery_attention_required"
    assert adapter.sent == []
    assert dispatch.delivery_limitation == _VISIBLE_LIMITATION


def test_every_attempt_public_id_has_one_derivation_from_its_own_columns(
    session, factory, project, roster, clock
):
    dispatch = _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter(
        DeliveryOutcome(status="failed", error_code="provider_busy", retryable=True)
    )
    category = _category()

    _deliver(factory, project, clock, adapter=adapter, category=category)
    clock.advance(10)
    _deliver(factory, project, clock, adapter=adapter, category=category)

    attempts = _attempts(session, dispatch)
    assert [attempt.public_id for attempt in attempts] == [
        outgoing_dispatch.attempt_public_id(
            category, dispatch_id=dispatch.id, attempt_number=number
        )
        for number in (1, 2)
    ]
    assert len({attempt.public_id for attempt in attempts}) == 2
    assert all(attempt.public_id.startswith("fake-attempt:") for attempt in attempts)


def test_the_subject_summary_reaches_the_adapter_with_the_idempotency_key(
    session, factory, project, roster, clock
):
    dispatch = _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter()
    category = _category(summary=lambda session, notification: {"body": "one message"})

    _deliver(factory, project, clock, adapter=adapter, category=category)

    request = adapter.deliveries_for(dispatch.idempotency_key)[0]
    assert request.channel == CHANNEL
    assert request.recipient_contact == CONTACT
    assert request.subject_summary == {"body": "one message"}


def test_the_receipt_reports_the_pass_and_whether_delivery_is_enabled(
    session, factory, project, roster, clock
):
    _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter()

    disabled = _deliver(factory, project, clock, adapter=adapter)
    assert disabled == {
        "schema_version": _SCHEMA_VERSION,
        "project_id": project.id,
        "configuration_version": "fake-config-v1",
        "observed_at": clock.now().isoformat(),
        "health": "healthy",
        "delivery_enabled": False,
        "considered": 1,
        "completed": 1,
        "retry_due": 0,
        "failed": 0,
        "uncertain": 0,
        "skipped": 0,
    }

    configure_assignment_notification(
        session,
        AssignmentNotificationDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="fake-config-v1",
            starts_at=clock.now(),
        ),
        now=clock.now(),
    )
    _occurrence(session, project, roster, index=2)

    enabled = _deliver(factory, project, clock, adapter=adapter)
    assert enabled["delivery_enabled"] is True


def test_a_naive_clock_is_refused_before_anything_is_written(
    session, factory, project, roster, clock
):
    _occurrence(session, project, roster, index=1)
    adapter = RecordingDeliveryAdapter()

    with pytest.raises(outgoing_dispatch.DispatchRefusal):
        _deliver(
            factory,
            project,
            ControlledClock(datetime(2026, 5, 4, 12, 0)),
            adapter=adapter,
        )

    assert adapter.sent == []
