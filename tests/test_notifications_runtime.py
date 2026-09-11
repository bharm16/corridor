"""End-to-end assignment-notification delivery over the shared runtime (#351).

These tests use the harness-owned disposable database so delivery runs through
real committed transactions, a controlled clock, and a fake (non-sending)
adapter. They cover both subject kinds, completion, provider uncertainty,
bounded retry and exhaustion, the re-checked delivery limitations (reassignment,
revoked membership, unresolved contact), durability across a crash, competing
workers, and the gate-7 boundary that keeps real delivery disabled.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor import notifications
from corridor.due_work import (
    AssignmentNotificationDeclaration,
    DueWorkRefusal,
    configure_due_work,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.external_statements import (
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import (
    AssignmentNotification,
    AssignmentNotificationAttempt,
    AssignmentNotificationDispatch,
    Dependency,
    DueWorkSchedule,
    ExternalOrg,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
)
from corridor.notifications import DeliveryOutcome, RecordingDeliveryAdapter
from corridor.principals import HumanPrincipal
from corridor.work_decisions import CoordinationSubject, assign_internal_owner

RECORDER = HumanPrincipal("local:runtime-coordinator")
CHANNEL = "email"


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _assignee(index: int = 1) -> HumanPrincipal:
    return HumanPrincipal(f"local:runtime-assignee-{index}")


def _committed_constraint(
    factory,
    *,
    index: int = 1,
    email: str | None = "dana@example.com",
    active: bool = True,
):
    assignee = _assignee(index)
    with factory() as s:
        project = Project(
            slug=f"notif-{uuid4().hex}", name="Notif Runtime", is_synthetic=True
        )
        s.add(project)
        s.flush([project])
        dependency = Dependency(
            project_id=project.id,
            ref_code=f"NR-{index}",
            dep_type="utility_relocation",
            title="Water main",
        )
        s.add(dependency)
        s.flush([dependency])
        roster = ProjectRosterEntry(
            project_id=project.id,
            principal_subject=assignee.subject,
            display_name=f"Assignee {index}",
            active=active,
            can_coordinate=True,
        )
        s.add(roster)
        s.flush([roster])
        if email is not None:
            s.add(
                PersonIdentity(
                    email_normalized=email, principal_subject=assignee.subject
                )
            )
            s.flush()
        decision = assign_internal_owner(
            s,
            CoordinationSubject.dependency(dependency.id),
            roster.display_name,
            principal=RECORDER,
        )
        notification = notifications.register_new_assignment_notification(
            s, assignment_decision=decision, roster_entry=roster, principal=RECORDER
        )
        result = {
            "project_id": project.id,
            "dependency_id": dependency.id,
            "roster_id": roster.id,
            "decision_id": decision.id,
            "notification_id": notification.id,
            "assignee": assignee,
        }
        s.commit()
    return result


def _sweep(factory, project_id, adapter, *, now, max_attempts=3, backoff_seconds=60):
    return notifications.deliver_project_assignment_notifications(
        factory,
        project_id=project_id,
        configuration_version="assignment-notification-v1",
        channel=CHANNEL,
        adapter=adapter,
        clock=ControlledClock(now),
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=500,
        owner="runtime:test-worker",
    )


def _dispatch(factory, notification_id):
    with factory() as s:
        return s.scalar(
            select(AssignmentNotificationDispatch).where(
                AssignmentNotificationDispatch.notification_id == notification_id
            )
        )


def _attempts(factory, notification_id):
    with factory() as s:
        dispatch_id = s.scalar(
            select(AssignmentNotificationDispatch.id).where(
                AssignmentNotificationDispatch.notification_id == notification_id
            )
        )
        return list(
            s.scalars(
                select(AssignmentNotificationAttempt)
                .where(AssignmentNotificationAttempt.dispatch_id == dispatch_id)
                .order_by(AssignmentNotificationAttempt.attempt_number)
            ).all()
        )


# --- Delivery outcomes ----------------------------------------------------


def test_delivery_completes_and_retains_provider_evidence(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    adapter = RecordingDeliveryAdapter()

    summary = _sweep(factory, ctx["project_id"], adapter, now=now)

    assert summary["completed"] == 1
    assert summary["health"] == "healthy"
    assert len(adapter.sent) == 1
    assert adapter.sent[0].recipient_contact == "dana@example.com"

    dispatch = _dispatch(factory, ctx["notification_id"])
    assert dispatch.delivery_state == "completed"
    assert dispatch.provider_message_id is not None
    assert dispatch.provider_result_json == {"delivered": True}
    attempts = _attempts(factory, ctx["notification_id"])
    assert [a.outcome for a in attempts] == ["completed"]


def test_statement_subject_delivers(runtime_database):
    factory = runtime_database.session_factory
    assignee = _assignee(9)
    with factory() as s:
        project = Project(
            slug=f"notif-stmt-{uuid4().hex}", name="Stmt", is_synthetic=True
        )
        s.add(project)
        s.flush([project])
        dependency = Dependency(
            project_id=project.id,
            ref_code="NS-1",
            dep_type="utility_relocation",
            title="Statement subject",
        )
        s.add(dependency)
        s.flush([dependency])
        party = ExternalOrg(name="Runtime Statement Party")
        s.add(party)
        s.flush([party])
        dependency.external_org_id = party.id
        s.flush()
        roster = ProjectRosterEntry(
            project_id=project.id,
            principal_subject=assignee.subject,
            display_name="Statement Owner",
            active=True,
            can_coordinate=True,
        )
        s.add(roster)
        s.flush([roster])
        s.add(
            PersonIdentity(
                email_normalized="stmt@example.com",
                principal_subject=assignee.subject,
            )
        )
        s.flush()
        event = record_external_party_statement(
            s,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="verbal",
            event_date=date(2026, 8, 12),
            description="Party will provide the relocation schedule.",
            new_timing=StatementTiming.day("August 20, 2026", date(2026, 8, 20)),
            scope=StatementScope.selected((dependency.id,)),
            created_by="local:statement-coordinator",
        )
        decision = assign_internal_owner(
            s,
            CoordinationSubject.statement(event.commitment_lineage_id),
            roster.display_name,
            principal=RECORDER,
        )
        notification = notifications.register_new_assignment_notification(
            s, assignment_decision=decision, roster_entry=roster, principal=RECORDER
        )
        project_id = project.id
        notification_id = notification.id
        assert notification.subject_kind == "statement"
        s.commit()

    adapter = RecordingDeliveryAdapter()
    _sweep(
        factory,
        project_id,
        adapter,
        now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
    )
    dispatch = _dispatch(factory, notification_id)
    assert dispatch.delivery_state == "completed"
    assert adapter.sent[0].subject_summary["subject_label"].startswith("Commitment")


def test_uncertain_outcome_is_retained_explicitly(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)
    adapter = RecordingDeliveryAdapter(
        DeliveryOutcome(
            status="uncertain",
            provider_result={"acknowledged": False},
            retryable=False,
        )
    )
    summary = _sweep(
        factory,
        ctx["project_id"],
        adapter,
        now=datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc),
    )
    assert summary["uncertain"] == 1
    assert summary["health"] == "delivery_attention_required"
    dispatch = _dispatch(factory, ctx["notification_id"])
    assert dispatch.delivery_state == "uncertain"
    assert dispatch.last_error_code == "acknowledgment_unavailable"
    attempts = _attempts(factory, ctx["notification_id"])
    assert attempts[-1].outcome == "uncertain"


def test_failure_retries_with_backoff_then_terminal_failure(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)
    adapter = RecordingDeliveryAdapter(
        DeliveryOutcome(status="failed", error_code="smtp_550", retryable=True)
    )
    start = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)

    first = _sweep(factory, ctx["project_id"], adapter, now=start)
    assert first["retry_due"] == 1
    dispatch = _dispatch(factory, ctx["notification_id"])
    assert dispatch.delivery_state == "retry_due"
    assert dispatch.next_attempt_at is not None

    second = _sweep(
        factory, ctx["project_id"], adapter, now=start + timedelta(seconds=120)
    )
    assert second["retry_due"] == 1

    third = _sweep(
        factory, ctx["project_id"], adapter, now=start + timedelta(seconds=600)
    )
    assert third["failed"] == 1
    dispatch = _dispatch(factory, ctx["notification_id"])
    assert dispatch.delivery_state == "failed"
    assert dispatch.next_attempt_at is None
    attempts = _attempts(factory, ctx["notification_id"])
    assert len(attempts) == 3
    assert all(a.outcome == "failed" for a in attempts)


def test_stable_idempotency_key_across_attempts(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)

    class FlakyThenGood:
        def __init__(self):
            self.calls = 0
            self.sent = []

        def deliver(self, request):
            self.sent.append(request)
            self.calls += 1
            if self.calls == 1:
                return DeliveryOutcome(status="failed", retryable=True)
            return DeliveryOutcome(
                status="completed", provider_message_id="ok", provider_result={"delivered": True}
            )

    adapter = FlakyThenGood()
    start = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)
    _sweep(factory, ctx["project_id"], adapter, now=start)
    _sweep(factory, ctx["project_id"], adapter, now=start + timedelta(seconds=120))

    keys = {request.idempotency_key for request in adapter.sent}
    assert len(adapter.sent) == 2
    assert len(keys) == 1  # the same idempotency key across the retry
    assert _dispatch(factory, ctx["notification_id"]).delivery_state == "completed"


# --- Re-checked limitations before dispatch -------------------------------


def test_reassignment_supersedes_the_stale_dispatch(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, index=1)
    # Reassign to a different roster identity, registering a fresh occurrence.
    second = _assignee(2)
    with factory() as s:
        roster = ProjectRosterEntry(
            project_id=ctx["project_id"],
            principal_subject=second.subject,
            display_name="Assignee 2",
            active=True,
            can_coordinate=True,
        )
        s.add(roster)
        s.flush([roster])
        s.add(
            PersonIdentity(
                email_normalized="sam@example.com", principal_subject=second.subject
            )
        )
        s.flush()
        decision = assign_internal_owner(
            s,
            CoordinationSubject.dependency(ctx["dependency_id"]),
            roster.display_name,
            principal=RECORDER,
        )
        new_notification = notifications.register_new_assignment_notification(
            s, assignment_decision=decision, roster_entry=roster, principal=RECORDER
        )
        new_notification_id = new_notification.id
        s.commit()

    adapter = RecordingDeliveryAdapter()
    _sweep(
        factory,
        ctx["project_id"],
        adapter,
        now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
    )
    stale = _dispatch(factory, ctx["notification_id"])
    assert stale.delivery_state == "failed"
    assert stale.delivery_limitation == notifications.LIMITATION_REASSIGNED
    current = _dispatch(factory, new_notification_id)
    assert current.delivery_state == "completed"
    # Only the current assignment was ever handed to the provider.
    assert [r.recipient_contact for r in adapter.sent] == ["sam@example.com"]


def test_revoked_membership_is_a_visible_delivery_limitation(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)
    with factory() as s:
        entry = s.get(ProjectRosterEntry, ctx["roster_id"])
        entry.active = False
        s.commit()
    adapter = RecordingDeliveryAdapter()
    _sweep(
        factory,
        ctx["project_id"],
        adapter,
        now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
    )
    dispatch = _dispatch(factory, ctx["notification_id"])
    assert dispatch.delivery_state == "failed"
    assert dispatch.delivery_limitation == notifications.LIMITATION_REVOKED_MEMBERSHIP
    assert adapter.sent == []


def test_unresolved_contact_never_invents_a_recipient(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, email=None)
    adapter = RecordingDeliveryAdapter()
    _sweep(
        factory,
        ctx["project_id"],
        adapter,
        now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
    )
    dispatch = _dispatch(factory, ctx["notification_id"])
    assert dispatch.delivery_state == "failed"
    assert dispatch.delivery_limitation == notifications.LIMITATION_UNRESOLVED_CONTACT
    assert adapter.sent == []


# --- Durability, the runtime, competing workers, and the gate --------------


def test_crash_after_commit_preserves_the_notification(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)
    # A completely fresh session (a "restarted" worker) still finds the durable
    # occurrence and dispatch, and can deliver it.
    with factory() as fresh:
        assert (
            fresh.scalar(
                select(func.count()).select_from(AssignmentNotification).where(
                    AssignmentNotification.id == ctx["notification_id"]
                )
            )
            == 1
        )
    adapter = RecordingDeliveryAdapter()
    _sweep(
        factory,
        ctx["project_id"],
        adapter,
        now=datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc),
    )
    assert _dispatch(factory, ctx["notification_id"]).delivery_state == "completed"


def test_committed_assignment_delivers_through_the_supervised_runtime(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    with factory() as s:
        configure_due_work(
            s,
            AssignmentNotificationDeclaration.released_hourly(
                project_id=ctx["project_id"],
                configuration_version="assignment-notification-v1",
                starts_at=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
            ),
            now=now,
        )
        s.commit()

    adapter = RecordingDeliveryAdapter()
    notifications.register_delivery_adapter(CHANNEL, adapter)
    try:
        with factory() as ticking:
            [occurrence] = enqueue_due_work(ticking, now=now)
            ticking.commit()
        result = run_due_work_once(
            factory, clock=ControlledClock(now), owner="runtime:notify-worker"
        )
    finally:
        notifications.clear_delivery_adapters()

    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_result["completed"] == 1
    assert len(adapter.sent) == 1
    assert _dispatch(factory, ctx["notification_id"]).delivery_state == "completed"
    with factory() as verification:
        status = due_work_status(verification, project_id=ctx["project_id"])
        assert status["receipts"][0]["handler"] == "assignment_notification"
        assert status["receipts"][0]["execution_outcome"] == "completed"


def test_competing_workers_deliver_a_notification_exactly_once(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    with factory() as s:
        configure_due_work(
            s,
            AssignmentNotificationDeclaration.released_hourly(
                project_id=ctx["project_id"],
                configuration_version="assignment-notification-v1",
                starts_at=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
            ),
            now=now,
        )
        s.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    adapter = RecordingDeliveryAdapter()
    notifications.register_delivery_adapter(CHANNEL, adapter)
    ready = Barrier(2)

    def compete(owner):
        ready.wait(timeout=2)
        return run_due_work_once(factory, clock=ControlledClock(now), owner=owner)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(compete, ("runtime:worker-a", "runtime:worker-b"))
            )
    finally:
        notifications.clear_delivery_adapters()

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1
    assert len(adapter.sent) == 1
    assert _dispatch(factory, ctx["notification_id"]).delivery_state == "completed"


def test_gate7_missing_or_invalid_config_keeps_delivery_disabled(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)

    # With no gate-7 schedule, the runtime enqueues nothing and the dispatch
    # stays queued: credentials or code completion alone deliver nothing.
    with factory() as ticking:
        assert enqueue_due_work(ticking, now=now) == ()
        ticking.commit()
    assert _dispatch(factory, ctx["notification_id"]).delivery_state == "queued"

    # An invalid declaration (a silent-zero request budget) is refused and
    # writes no schedule, so delivery remains disabled.
    with factory() as s:
        with pytest.raises(DueWorkRefusal):
            configure_due_work(
                s,
                AssignmentNotificationDeclaration.released_hourly(
                    project_id=ctx["project_id"],
                    configuration_version="assignment-notification-v1",
                    starts_at=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
                    notification_budget=0,
                ),
                now=now,
            )
        assert (
            s.scalar(
                select(func.count()).select_from(DueWorkSchedule).where(
                    DueWorkSchedule.project_id == ctx["project_id"]
                )
            )
            == 0
        )
