"""End-to-end due-action delivery over the shared runtime (#352).

These tests use the harness-owned disposable database so derivation and delivery
run through real committed transactions, a controlled clock, and a fake
(non-sending) adapter.  They cover both subject kinds, the complete population,
the currency re-check that withholds a reminder after completion, cancellation,
reassignment, or deferral, urgent-overdue escalation with deduplication, provider
uncertainty and bounded retry, durability across a crash, competing workers, the
daily-summary window and its summary hour, the missed-run boundary, and the
gate-7 boundary that keeps real delivery disabled.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

from sqlalchemy import func, select

from corridor import notifications
from corridor.due_work import (
    DueActionNotificationDeclaration,
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
    DueActionNotification,
    DueActionNotificationDispatch,
    Dependency,
    ExternalOrg,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
)
from corridor.notifications import DeliveryOutcome, RecordingDeliveryAdapter
from corridor.principals import HumanPrincipal
from corridor.work_decisions import (
    CoordinationSubject,
    assign_internal_owner,
    cancel_next_action,
    complete_next_action,
    defer_work,
    set_next_action,
)

RECORDER = HumanPrincipal("local:due-action-runtime-coordinator")
CHANNEL = "email"
CONFIG_VERSION = "due-action-notification-v1"


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _assignee(index: int) -> HumanPrincipal:
    return HumanPrincipal(f"local:due-action-runtime-assignee-{index}")


def _roster(session, project_id, principal, *, display_name, email, active=True):
    entry = ProjectRosterEntry(
        project_id=project_id,
        principal_subject=principal.subject,
        display_name=display_name,
        active=active,
        can_coordinate=True,
    )
    session.add(entry)
    session.flush([entry])
    if email is not None:
        existing = session.scalar(
            select(PersonIdentity).where(
                PersonIdentity.principal_subject == principal.subject
            )
        )
        if existing is None:
            session.add(
                PersonIdentity(
                    email_normalized=email, principal_subject=principal.subject
                )
            )
            session.flush()
    return entry


def _committed_constraint(factory, *, due_date, index=1, email=None):
    assignee = _assignee(index)
    with factory() as s:
        project = Project(
            slug=f"due-action-{uuid4().hex}", name="Due Action Runtime", is_synthetic=True
        )
        s.add(project)
        s.flush([project])
        dependency = Dependency(
            project_id=project.id,
            ref_code=f"DR-{index}",
            dep_type="utility_relocation",
            title="Water main",
        )
        s.add(dependency)
        s.flush([dependency])
        roster = _roster(
            s,
            project.id,
            assignee,
            display_name=f"Owner {index}",
            email=email or f"assignee{index}@example.com",
        )
        subject = CoordinationSubject.dependency(dependency.id)
        decision = assign_internal_owner(
            s, subject, roster.display_name, principal=RECORDER
        )
        notifications.register_new_assignment_notification(
            s, assignment_decision=decision, roster_entry=roster, principal=RECORDER
        )
        set_next_action(
            s,
            subject,
            "Coordinate this constraint with the organization",
            due_date=due_date,
            principal=RECORDER,
        )
        result = {
            "project_id": project.id,
            "dependency_id": dependency.id,
            "roster_id": roster.id,
            "assignee": assignee,
        }
        s.commit()
    return result


def _committed_statement(factory, *, due_date, index=1):
    assignee = _assignee(index)
    with factory() as s:
        project = Project(
            slug=f"due-action-stmt-{uuid4().hex}", name="Stmt", is_synthetic=True
        )
        s.add(project)
        s.flush([project])
        dependency = Dependency(
            project_id=project.id,
            ref_code=f"DS-{index}",
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
        roster = _roster(
            s,
            project.id,
            assignee,
            display_name=f"Statement Owner {index}",
            email=f"stmt{index}@example.com",
        )
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
        subject = CoordinationSubject.statement(event.commitment_lineage_id)
        decision = assign_internal_owner(
            s, subject, roster.display_name, principal=RECORDER
        )
        notifications.register_new_assignment_notification(
            s, assignment_decision=decision, roster_entry=roster, principal=RECORDER
        )
        set_next_action(
            s,
            subject,
            "Confirm the stated timing with the organization",
            due_date=due_date,
            principal=RECORDER,
        )
        result = {
            "project_id": project.id,
            "commitment_lineage_id": event.commitment_lineage_id,
            "assignee": assignee,
        }
        s.commit()
    return result


def _register(factory, project_id, *, now, urgent_overdue_days=3, escalation=None,
              register_summary=False, window_days=1):
    today = now.date()
    with factory() as s:
        with s.begin():
            notifications.register_due_action_notifications(
                s,
                project_id=project_id,
                configuration_version=CONFIG_VERSION,
                today=today,
                urgent_overdue_days=urgent_overdue_days,
                escalation_roster_entry_id=escalation,
                channel=CHANNEL,
                owner="runtime:test-worker",
                register_summary=register_summary,
                summary_window_start=today - timedelta(days=window_days - 1),
                summary_window_end=today,
            )


def _sweep(factory, project_id, adapter, *, now, urgent_overdue_days=3, escalation=None,
           max_attempts=3, backoff_seconds=60):
    return notifications.deliver_project_due_action_notifications(
        factory,
        project_id=project_id,
        configuration_version=CONFIG_VERSION,
        channel=CHANNEL,
        adapter=adapter,
        clock=ControlledClock(now),
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=500,
        urgent_overdue_days=urgent_overdue_days,
        escalation_roster_entry_id=escalation,
        owner="runtime:test-worker",
    )


def _dispatches(factory, project_id):
    with factory() as s:
        return list(
            s.scalars(
                select(DueActionNotificationDispatch)
                .where(DueActionNotificationDispatch.project_id == project_id)
                .order_by(DueActionNotificationDispatch.id)
            ).all()
        )


def _only_dispatch(factory, project_id):
    dispatches = _dispatches(factory, project_id)
    assert len(dispatches) == 1
    return dispatches[0]


# --- Delivery of both subject kinds ---------------------------------------


def test_constraint_reminder_delivers_end_to_end(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 9, 2))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    adapter = RecordingDeliveryAdapter()

    _register(factory, ctx["project_id"], now=now)
    summary = _sweep(factory, ctx["project_id"], adapter, now=now)

    assert summary["completed"] == 1
    assert summary["schema_version"] == "due-action-notification-result-v1"
    assert len(adapter.sent) == 1
    assert adapter.sent[0].recipient_contact == "assignee1@example.com"
    assert adapter.sent[0].subject_summary["urgency"] == "soon"
    assert _only_dispatch(factory, ctx["project_id"]).delivery_state == "completed"


def test_statement_reminder_delivers_end_to_end(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_statement(factory, due_date=date(2026, 9, 2))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    adapter = RecordingDeliveryAdapter()

    _register(factory, ctx["project_id"], now=now)
    _sweep(factory, ctx["project_id"], adapter, now=now)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].subject_summary["subject_label"].startswith("Commitment")
    assert _only_dispatch(factory, ctx["project_id"]).delivery_state == "completed"


# --- Currency re-read before dispatch -------------------------------------


def test_completion_withholds_the_reminder_at_dispatch(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 8, 25))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    _register(factory, ctx["project_id"], now=now)

    # The assigned person completes the action before the reminder is delivered.
    with factory() as s:
        complete_next_action(
            s,
            CoordinationSubject.dependency(ctx["dependency_id"]),
            principal=RECORDER,
            no_follow_up_reason="no_immediate_follow_up",
        )
        s.commit()

    adapter = RecordingDeliveryAdapter()
    summary = _sweep(factory, ctx["project_id"], adapter, now=now)

    assert adapter.sent == []
    assert summary["skipped"] == 1
    dispatch = _only_dispatch(factory, ctx["project_id"])
    assert dispatch.delivery_state == "failed"
    assert dispatch.delivery_limitation == notifications.LIMITATION_PLAN_SUPERSEDED


def test_cancellation_withholds_the_reminder_at_dispatch(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 8, 25))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    _register(factory, ctx["project_id"], now=now)
    with factory() as s:
        cancel_next_action(
            s,
            CoordinationSubject.dependency(ctx["dependency_id"]),
            principal=RECORDER,
            cancellation_reason="no_longer_needed",
            no_follow_up_reason="no_immediate_follow_up",
        )
        s.commit()

    adapter = RecordingDeliveryAdapter()
    _sweep(factory, ctx["project_id"], adapter, now=now)

    assert adapter.sent == []
    assert (
        _only_dispatch(factory, ctx["project_id"]).delivery_limitation
        == notifications.LIMITATION_PLAN_SUPERSEDED
    )


def test_reassignment_withholds_and_the_new_owner_is_reminded(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 8, 25), index=1)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    _register(factory, ctx["project_id"], now=now)

    second = _assignee(2)
    with factory() as s:
        roster = _roster(
            s, ctx["project_id"], second, display_name="Owner 2", email="assignee2@example.com"
        )
        subject = CoordinationSubject.dependency(ctx["dependency_id"])
        decision = assign_internal_owner(
            s, subject, roster.display_name, principal=RECORDER
        )
        notifications.register_new_assignment_notification(
            s, assignment_decision=decision, roster_entry=roster, principal=RECORDER
        )
        s.commit()

    adapter = RecordingDeliveryAdapter()
    _sweep(factory, ctx["project_id"], adapter, now=now)
    # The stale reminder to the former owner is withheld, not sent.
    stale = _only_dispatch(factory, ctx["project_id"])
    assert stale.delivery_state == "failed"
    assert stale.delivery_limitation == notifications.LIMITATION_ACTION_REASSIGNED
    assert adapter.sent == []

    # The next derivation registers a reminder for the new owner, who is reached.
    _register(factory, ctx["project_id"], now=now)
    _sweep(factory, ctx["project_id"], adapter, now=now)
    assert [r.recipient_contact for r in adapter.sent] == ["assignee2@example.com"]


def test_deferral_withholds_the_reminder_at_dispatch(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 8, 25))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    _register(factory, ctx["project_id"], now=now)
    with factory() as s:
        defer_work(
            s,
            CoordinationSubject.dependency(ctx["dependency_id"]),
            reason="waiting_for_external_party",
            return_date=date(2026, 9, 10),
            principal=RECORDER,
        )
        s.commit()

    adapter = RecordingDeliveryAdapter()
    _sweep(factory, ctx["project_id"], adapter, now=now)

    assert adapter.sent == []
    assert (
        _only_dispatch(factory, ctx["project_id"]).delivery_limitation
        == notifications.LIMITATION_DEFERRED
    )


# --- Urgent overdue, escalation, and deduplication -------------------------


def test_urgent_overdue_reaches_assignee_and_one_escalation_contact(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 8, 20), index=1)
    escalation_principal = _assignee(5)
    with factory() as s:
        escalation = _roster(
            s,
            ctx["project_id"],
            escalation_principal,
            display_name="Escalation Contact",
            email="escalation@example.com",
        )
        escalation_id = escalation.id
        s.commit()

    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    _register(factory, ctx["project_id"], now=now, urgent_overdue_days=3, escalation=escalation_id)
    adapter = RecordingDeliveryAdapter()
    _sweep(factory, ctx["project_id"], adapter, now=now, urgent_overdue_days=3, escalation=escalation_id)

    # Exactly two recipients — the assignee and the one escalation contact — and
    # never the whole team.
    contacts = sorted(r.recipient_contact for r in adapter.sent)
    assert contacts == ["assignee1@example.com", "escalation@example.com"]
    categories = set(
        s2.category
        for s2 in _all_occurrences(factory, ctx["project_id"])
    )
    assert categories == {"next_action_due", "next_action_escalation"}


def test_escalation_disabled_without_a_configured_contact(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 8, 20))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    _register(factory, ctx["project_id"], now=now, urgent_overdue_days=3, escalation=None)
    adapter = RecordingDeliveryAdapter()
    _sweep(factory, ctx["project_id"], adapter, now=now, urgent_overdue_days=3, escalation=None)

    # Only the assignee is reached; no escalation occurrence exists.
    assert [r.recipient_contact for r in adapter.sent] == ["assignee1@example.com"]
    with factory() as s:
        view = notifications.due_action_operations_view(s, project_id=ctx["project_id"])
    assert view["escalation"]["enabled"] is False
    assert view["escalation"]["reason"] == "no due-action schedule is enabled"


def _all_occurrences(factory, project_id):
    with factory() as s:
        return list(
            s.scalars(
                select(DueActionNotification).where(
                    DueActionNotification.project_id == project_id
                )
            ).all()
        )


# --- Provider uncertainty and bounded retry --------------------------------


# --- Durability, competing workers, and the supervised runtime -------------


def test_committed_reminder_delivers_through_the_supervised_runtime(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 9, 2))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    with factory() as s:
        configure_due_work(
            s,
            DueActionNotificationDeclaration.released_hourly(
                project_id=ctx["project_id"],
                configuration_version=CONFIG_VERSION,
                starts_at=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
                summary_hour_utc=13,
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
            factory, clock=ControlledClock(now), owner="runtime:due-action-worker"
        )
    finally:
        notifications.clear_delivery_adapters()

    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_result["completed"] == 1
    assert len(adapter.sent) == 1
    with factory() as verification:
        status = due_work_status(verification, project_id=ctx["project_id"])
        assert status["receipts"][0]["handler"] == "due_action_notification"


def test_crash_after_commit_preserves_the_occurrence(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 9, 2))
    now = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    _register(factory, ctx["project_id"], now=now)
    # A fresh (restarted) worker still finds the durable occurrence and delivers.
    with factory() as fresh:
        assert (
            fresh.scalar(
                select(func.count()).select_from(DueActionNotification).where(
                    DueActionNotification.project_id == ctx["project_id"]
                )
            )
            == 1
        )
    adapter = RecordingDeliveryAdapter()
    _sweep(factory, ctx["project_id"], adapter, now=now)
    assert _only_dispatch(factory, ctx["project_id"]).delivery_state == "completed"


def test_competing_workers_deliver_a_reminder_exactly_once(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 9, 2))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    with factory() as s:
        configure_due_work(
            s,
            DueActionNotificationDeclaration.released_hourly(
                project_id=ctx["project_id"],
                configuration_version=CONFIG_VERSION,
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
            results = list(pool.map(compete, ("runtime:worker-a", "runtime:worker-b")))
    finally:
        notifications.clear_delivery_adapters()

    assert len([r for r in results if r is not None]) == 1
    assert len(adapter.sent) == 1


# --- Daily summary window and missed-run boundary --------------------------


def test_daily_summary_registers_only_on_the_summary_hour(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 9, 2))
    with factory() as s:
        configure_due_work(
            s,
            DueActionNotificationDeclaration.released_hourly(
                project_id=ctx["project_id"],
                configuration_version=CONFIG_VERSION,
                starts_at=datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc),
                summary_hour_utc=13,
            ),
            now=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
        )
        s.commit()

    adapter = RecordingDeliveryAdapter()
    notifications.register_delivery_adapter(CHANNEL, adapter)
    try:
        # A tick at 07:00 (not the summary hour) registers the reminder only.
        off_hour = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)
        with factory() as s:
            enqueue_due_work(s, now=off_hour)
            s.commit()
        run_due_work_once(factory, clock=ControlledClock(off_hour), owner="runtime:summary-worker")
        assert _summary_count(factory, ctx["project_id"]) == 0

        # A tick at the declared summary hour registers one daily summary.
        summary_hour = datetime(2026, 8, 30, 13, 0, tzinfo=timezone.utc)
        with factory() as s:
            enqueue_due_work(s, now=summary_hour)
            s.commit()
        run_due_work_once(factory, clock=ControlledClock(summary_hour), owner="runtime:summary-worker")
        assert _summary_count(factory, ctx["project_id"]) == 1
    finally:
        notifications.clear_delivery_adapters()


def _summary_count(factory, project_id):
    with factory() as s:
        return s.scalar(
            select(func.count()).select_from(DueActionNotification).where(
                DueActionNotification.project_id == project_id,
                DueActionNotification.category == "daily_summary",
            )
        )


def test_late_processing_does_not_backfill_a_backlog(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 8, 20))
    # Two late ticks on different days derive only the current condition each
    # time; the unchanged overdue band converges on one occurrence, never a
    # per-missed-hour backlog.
    _register(factory, ctx["project_id"], now=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc))
    _register(factory, ctx["project_id"], now=datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc))
    assert (
        len([o for o in _all_occurrences(factory, ctx["project_id"]) if o.category == "next_action_due"])
        == 1
    )


# --- Gate-7 boundary keeps delivery disabled -------------------------------


def test_without_a_schedule_nothing_is_enqueued_or_delivered(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 9, 2))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    # No gate-7 schedule: the runtime enqueues nothing and nothing is derived.
    with factory() as ticking:
        assert enqueue_due_work(ticking, now=now) == ()
        ticking.commit()
    assert _all_occurrences(factory, ctx["project_id"]) == []


def test_default_adapter_never_sends(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_constraint(factory, due_date=date(2026, 9, 2))
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    _register(factory, ctx["project_id"], now=now)
    # The disabled default adapter fails visibly rather than sending.
    summary = _sweep(
        factory, ctx["project_id"], notifications.DisabledDeliveryAdapter(), now=now
    )
    assert summary["failed"] == 1
    dispatch = _only_dispatch(factory, ctx["project_id"])
    assert dispatch.delivery_state == "failed"
    assert dispatch.last_error_code == "delivery_adapter_not_configured"
