"""End-to-end document-notification delivery over the shared runtime (#353).

These tests use the harness-owned disposable database so discovery and delivery
run through real committed transactions, a controlled clock, and a fake
(non-sending) adapter. They cover a lost-support interruption reaching both
recipients, a re-checked condition that resolved before dispatch, a reviewer who
left the project, an unresolved contact, bounded retry, durability across a crash,
the supervised runtime that discovers and delivers in one pass, competing workers,
and the gate-7 boundary that keeps real delivery disabled.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

from sqlalchemy import func, select

from corridor import document_notifications as dn
from corridor import notifications
from corridor.documentation_checklist import confirm_interpretation, read_checklist
from corridor.due_work import (
    DocumentNotificationDeclaration,
    DueWorkRefusal,
    configure_document_notification,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
import pytest
from corridor.models import (
    Dependency,
    DocPage,
    Document,
    DocumentNotification,
    DocumentNotificationAttempt,
    DocumentNotificationDispatch,
    EvidenceLink,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
)
from corridor.notifications import DeliveryOutcome, RecordingDeliveryAdapter
from corridor.principals import HumanPrincipal
from corridor.supersession import SupersessionDeclaration, register_supersessions
from corridor.work_decisions import (
    FollowUpPlanDraft,
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    save_follow_up_plan,
)

CHANNEL = "email"
COORDINATOR = HumanPrincipal("local:docrt-coordinator")
REGISTRAR = "runtime:docrt-registrar"


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _assignee(index: int) -> HumanPrincipal:
    return HumanPrincipal(f"local:docrt-assignee-{index}")


def _reviewer(index: int) -> HumanPrincipal:
    return HumanPrincipal(f"local:docrt-reviewer-{index}")


def _document(session, project, *, name, text, doc_type="agreement", registry_id=None):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=(name * 64)[:64],
        filename=f"{name}.pdf",
        doc_type=doc_type,
        parse_status="parsed",
        doc_date=date(2026, 8, 29),
    )
    session.add(document)
    session.flush([document])
    session.add(DocPage(document_id=document.id, page_no=1, text=text))
    session.flush()
    return document


def _support(session, dependency, document, quote, *, verified=True):
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=quote,
        verified=verified,
    )
    session.add(link)
    session.flush([link])
    return link


def _roster(session, project, principal, *, display_name, email):
    entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject=principal.subject,
        display_name=display_name,
        active=True,
        can_coordinate=True,
        can_review_documentation=True,
    )
    session.add(entry)
    session.flush([entry])
    if email is not None:
        session.add(
            PersonIdentity(email_normalized=email, principal_subject=principal.subject)
        )
        session.flush()
    return entry


def _committed_loss(
    factory,
    *,
    index: int = 1,
    reviewer_member: bool = True,
    assignee_email: str | None = "dana@example.com",
    reviewer_email: str | None = "rae@example.com",
    register: bool = True,
):
    """Commit an affirmative review whose reviewed support then lapses."""

    reviewer = _reviewer(index)
    assignee = _assignee(index)
    with factory() as s:
        project = Project(
            slug=f"docrt-{uuid4().hex}", name="Doc Runtime", is_synthetic=True
        )
        s.add(project)
        s.flush([project])
        dependency = Dependency(
            project_id=project.id,
            ref_code=f"DR-{index}",
            dep_type="utility_relocation",
            title="Gas crossing",
            resolution_strategy="relocate",
        )
        s.add(dependency)
        s.flush([dependency])
        as_built = _document(
            s, project, name=f"asbuilt-{index}", text="The as-built package is on file."
        )
        _support(s, dependency, as_built, "The as-built package is on file.")
        approval = _document(
            s,
            project,
            name=f"approval-{index}",
            text="The relocation is approved.",
            registry_id=f"APPROVAL-{index}",
        )
        approval_link = _support(s, dependency, approval, "The relocation is approved.")
        confirm_interpretation(s, dependency.id, approval_link.id, principal=reviewer)
        assert read_checklist(s, dependency.id).is_ready is True

        assignee_roster = _roster(
            s, project, assignee, display_name=f"Assignee {index}", email=assignee_email
        )
        if reviewer_member:
            _roster(
                s, project, reviewer, display_name=f"Reviewer {index}", email=reviewer_email
            )
        save_follow_up_plan(
            s,
            FollowUpPlanDraft(
                dependency_id=dependency.id,
                internal_owner_roster_entry_id=assignee_roster.id,
                next_action=FOLLOW_UP_NEXT_ACTION_CHOICES[0],
                action_due_date=date(2026, 9, 1),
                action_due_date_unknown_reason=None,
            ),
            principal=COORDINATOR,
        )
        # Register a real supersession so the reviewed approval letter is no
        # longer current support.
        successor = _document(
            s,
            project,
            name=f"approval-{index}-b",
            text="A newer superseding revision.",
            registry_id=f"SUCC-{index}",
        )
        _document(
            s,
            project,
            name=f"idx-{index}",
            text=f"APPROVAL-{index} superseded by SUCC-{index} on 2026-08-01",
            doc_type="other",
            registry_id=f"IDX-{index}",
        )
        register_supersessions(
            s,
            [
                SupersessionDeclaration(
                    predecessor_registry_id=f"APPROVAL-{index}",
                    successor_registry_id=f"SUCC-{index}",
                    replacement_date=date(2026, 8, 1),
                    source_registry_id=f"IDX-{index}",
                    source_page=1,
                )
            ],
            project_id=project.id,
        )
        s.refresh(dependency)
        assert read_checklist(s, dependency.id).is_ready is False
        if register:
            dn.register_project_document_notifications(
                s, project_id=project.id, registered_by=REGISTRAR
            )
        result = {
            "project_id": project.id,
            "dependency_id": dependency.id,
            "successor_id": successor.id,
            "assignee": assignee,
            "reviewer": reviewer,
        }
        s.commit()
    return result


def _sweep(factory, project_id, adapter, *, now, max_attempts=3, backoff_seconds=60):
    return dn.deliver_project_document_notifications(
        factory,
        project_id=project_id,
        configuration_version="document-notification-v1",
        channel=CHANNEL,
        adapter=adapter,
        clock=ControlledClock(now),
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=500,
        owner="runtime:docrt-worker",
    )


def _dispatch_for(factory, project_id, principal):
    with factory() as s:
        return s.scalar(
            select(DocumentNotificationDispatch)
            .join(
                DocumentNotification,
                DocumentNotification.id == DocumentNotificationDispatch.notification_id,
            )
            .where(
                DocumentNotification.project_id == project_id,
                DocumentNotification.recipient_principal_subject == principal.subject,
            )
        )


# --- Delivery outcomes ----------------------------------------------------


def test_loss_delivers_to_current_assignee_and_original_reviewer(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_loss(factory)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    adapter = RecordingDeliveryAdapter()

    summary = _sweep(factory, ctx["project_id"], adapter, now=now)

    assert summary["completed"] == 2
    assert summary["health"] == "healthy"
    contacts = {r.recipient_contact for r in adapter.sent}
    assert contacts == {"dana@example.com", "rae@example.com"}
    # Each message preserves the earlier review and asks for current work.
    for request in adapter.sent:
        assert request.subject_summary["category"] == dn.CATEGORY_DOCUMENTATION_LOSS
        assert request.subject_summary["earlier_review"]["author"] == ctx["reviewer"].subject
    assert _dispatch_for(factory, ctx["project_id"], ctx["assignee"]).delivery_state == "completed"


def test_condition_resolved_before_dispatch_is_a_benign_skip(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_loss(factory, index=2)
    # Re-review: confirm the requirement on the newer document, restoring Ready.
    with factory() as s:
        successor = s.get(Document, ctx["successor_id"])
        page = s.scalars(select(DocPage).where(DocPage.document_id == successor.id)).one()
        page.text = "The relocation is approved."
        s.flush()
        link = _support(
            s, s.get(Dependency, ctx["dependency_id"]), successor, "The relocation is approved."
        )
        confirm_interpretation(
            s, ctx["dependency_id"], link.id, principal=ctx["reviewer"]
        )
        assert read_checklist(s, ctx["dependency_id"]).is_ready is True
        s.commit()

    adapter = RecordingDeliveryAdapter()
    summary = _sweep(
        factory, ctx["project_id"], adapter, now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    )

    assert adapter.sent == []
    assert summary["skipped"] == 2
    dispatch = _dispatch_for(factory, ctx["project_id"], ctx["assignee"])
    assert dispatch.delivery_limitation == dn.LIMITATION_CONDITION_RESOLVED


def test_reviewer_who_left_the_project_is_a_visible_limitation(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_loss(factory, index=3, reviewer_member=False)
    adapter = RecordingDeliveryAdapter()

    _sweep(
        factory, ctx["project_id"], adapter, now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    )

    reviewer_dispatch = _dispatch_for(factory, ctx["project_id"], ctx["reviewer"])
    assert reviewer_dispatch.delivery_state == "failed"
    assert reviewer_dispatch.delivery_limitation == notifications.LIMITATION_REVOKED_MEMBERSHIP
    # Only the current member (the assignee) was handed to the provider.
    assert [r.recipient_contact for r in adapter.sent] == ["dana@example.com"]


def test_unresolved_contact_never_invents_a_recipient(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_loss(factory, index=4, assignee_email=None)
    adapter = RecordingDeliveryAdapter()

    _sweep(
        factory, ctx["project_id"], adapter, now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    )

    dispatch = _dispatch_for(factory, ctx["project_id"], ctx["assignee"])
    assert dispatch.delivery_state == "failed"
    assert dispatch.delivery_limitation == notifications.LIMITATION_UNRESOLVED_CONTACT
    assert "dana@example.com" not in {r.recipient_contact for r in adapter.sent}


def test_failure_retries_then_terminal_failure(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_loss(factory, index=5, reviewer_member=False)
    adapter = RecordingDeliveryAdapter(
        DeliveryOutcome(status="failed", error_code="smtp_550", retryable=True)
    )
    start = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)

    first = _sweep(factory, ctx["project_id"], adapter, now=start)
    assert first["retry_due"] == 1
    assert _dispatch_for(factory, ctx["project_id"], ctx["assignee"]).delivery_state == "retry_due"

    _sweep(factory, ctx["project_id"], adapter, now=start + timedelta(seconds=120))
    third = _sweep(factory, ctx["project_id"], adapter, now=start + timedelta(seconds=600))
    assert third["failed"] == 1
    dispatch = _dispatch_for(factory, ctx["project_id"], ctx["assignee"])
    assert dispatch.delivery_state == "failed"
    attempts = _attempts_for(factory, dispatch.id)
    assert len(attempts) == 3
    assert all(a.outcome == "failed" for a in attempts)


def _attempts_for(factory, dispatch_id):
    with factory() as s:
        return list(
            s.scalars(
                select(DocumentNotificationAttempt)
                .where(DocumentNotificationAttempt.dispatch_id == dispatch_id)
                .order_by(DocumentNotificationAttempt.attempt_number)
            ).all()
        )


def test_crash_after_commit_preserves_the_notification(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_loss(factory, index=6, reviewer_member=False)
    with factory() as fresh:
        # Both occurrences survive a restart; membership is re-checked at dispatch.
        assert (
            fresh.scalar(
                select(func.count())
                .select_from(DocumentNotification)
                .where(DocumentNotification.project_id == ctx["project_id"])
            )
            == 2
        )
    adapter = RecordingDeliveryAdapter()
    _sweep(
        factory, ctx["project_id"], adapter, now=datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    )
    assert _dispatch_for(factory, ctx["project_id"], ctx["assignee"]).delivery_state == "completed"


# --- The supervised runtime discovers and delivers -------------------------


def test_committed_loss_discovers_and_delivers_through_the_runtime(runtime_database):
    factory = runtime_database.session_factory
    # No pre-registration: the runtime handler discovers the affected population.
    ctx = _committed_loss(factory, index=7, register=False)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    with factory() as s:
        configure_document_notification(
            s,
            DocumentNotificationDeclaration.released_hourly(
                project_id=ctx["project_id"],
                configuration_version="document-notification-v1",
                starts_at=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
            ),
            now=now,
        )
        s.commit()

    adapter = RecordingDeliveryAdapter()
    notifications.register_delivery_adapter(CHANNEL, adapter)
    try:
        with factory() as ticking:
            enqueue_due_work(ticking, now=now)
            ticking.commit()
        result = run_due_work_once(
            factory, clock=ControlledClock(now), owner="runtime:docrt-worker"
        )
    finally:
        notifications.clear_delivery_adapters()

    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_result["completed"] == 2
    assert len(adapter.sent) == 2
    with factory() as verification:
        status = due_work_status(verification, project_id=ctx["project_id"])
        assert status["receipts"][0]["handler"] == "document_notification"
        assert status["receipts"][0]["execution_outcome"] == "completed"


def test_competing_workers_deliver_each_notification_once(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_loss(factory, index=8, register=False)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)
    with factory() as s:
        configure_document_notification(
            s,
            DocumentNotificationDeclaration.released_hourly(
                project_id=ctx["project_id"],
                configuration_version="document-notification-v1",
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

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1
    # Each of the two recipients is delivered exactly once.
    assert len(adapter.sent) == 2
    assert len({r.recipient_contact for r in adapter.sent}) == 2


def test_gate7_missing_or_invalid_config_keeps_delivery_disabled(runtime_database):
    factory = runtime_database.session_factory
    ctx = _committed_loss(factory, index=9, reviewer_member=False)
    now = datetime(2026, 8, 30, 7, 5, tzinfo=timezone.utc)

    with factory() as ticking:
        assert enqueue_due_work(ticking, now=now) == ()
        ticking.commit()
    assert _dispatch_for(factory, ctx["project_id"], ctx["assignee"]).delivery_state == "queued"

    from corridor.models import DueWorkSchedule

    with factory() as s:
        with pytest.raises(DueWorkRefusal):
            configure_document_notification(
                s,
                DocumentNotificationDeclaration.released_hourly(
                    project_id=ctx["project_id"],
                    configuration_version="document-notification-v1",
                    starts_at=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
                    notification_budget=0,
                ),
                now=now,
            )
        assert (
            s.scalar(
                select(func.count())
                .select_from(DueWorkSchedule)
                .where(DueWorkSchedule.project_id == ctx["project_id"])
            )
            == 0
        )
    with factory() as reading:
        view = dn.operations_document_notifications_view(
            reading, project_id=ctx["project_id"]
        )
    assert view["delivery_enabled"] is False
