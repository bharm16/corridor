"""Derivation, registration, and gate-7 validation of due-action work (#352).

These tests drive the domain seams through a rollback-scoped session: soon-due
and past-due Next Actions are derived for both Constraint and statement subjects
from their current authoritative plans and the existing check semantics, over the
complete population; an unknown Action Due Date is never invented; escalation is
declared and deduplicated; daily summaries are one durable occurrence per
recipient and window; and the gate-7 configuration refuses incomplete or invalid
input.  The committed delivery sweep is proven in the runtime test module.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

from corridor import notifications
from corridor.db import Session, engine
from corridor.due_work import (
    DueActionNotificationDeclaration,
    DueWorkRefusal,
    configure_due_action_notification,
    due_work_status,
)
from corridor.external_statements import (
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import (
    DueActionNotification,
    DueActionNotificationDispatch,
    DueWorkSchedule,
    ExternalOrg,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
)
from corridor.principals import HumanPrincipal
from corridor.work_decisions import (
    CoordinationSubject,
    assign_internal_owner,
    cancel_next_action,
    complete_next_action,
    defer_work,
    set_next_action,
)

RECORDER = HumanPrincipal("local:due-action-coordinator")
TODAY = date(2026, 8, 30)
NOW = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(slug="due-action-test", name="Due Action", is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _assignee(index: int) -> HumanPrincipal:
    return HumanPrincipal(f"local:due-action-assignee-{index}")


def _roster(
    session,
    project,
    principal: HumanPrincipal,
    *,
    display_name: str,
    active: bool = True,
    email: str | None = None,
) -> ProjectRosterEntry:
    entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject=principal.subject,
        display_name=display_name,
        active=active,
        can_coordinate=True,
    )
    session.add(entry)
    session.flush()
    resolved_email = email or f"{principal.subject.split(':')[-1]}@example.com"
    existing = session.scalar(
        select(PersonIdentity).where(
            PersonIdentity.principal_subject == principal.subject
        )
    )
    if existing is None:
        session.add(
            PersonIdentity(
                email_normalized=resolved_email, principal_subject=principal.subject
            )
        )
        session.flush()
    return entry


def _constraint(session, project, *, ref_code: str) -> int:
    from corridor.models import Dependency

    dependency = Dependency(
        project_id=project.id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title=f"Constraint {ref_code}",
    )
    session.add(dependency)
    session.flush()
    return dependency.id


def _assign(session, subject, roster):
    """Assign the owner and register the #351 occurrence the reminder resolves through."""
    decision = assign_internal_owner(
        session, subject, roster.display_name, principal=RECORDER
    )
    notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    return decision


def _constraint_plan(
    session,
    project,
    *,
    ref_code: str,
    assignee_index: int = 1,
    due_date: date | None = None,
    due_date_unknown_reason: str | None = None,
    roster: ProjectRosterEntry | None = None,
) -> int:
    dependency_id = _constraint(session, project, ref_code=ref_code)
    subject = CoordinationSubject.dependency(dependency_id)
    if roster is None:
        roster = _roster(
            session,
            project,
            _assignee(assignee_index),
            display_name=f"Owner {assignee_index}",
        )
    _assign(session, subject, roster)
    set_next_action(
        session,
        subject,
        "Coordinate this constraint with the organization",
        due_date=due_date,
        due_date_unknown_reason=due_date_unknown_reason,
        principal=RECORDER,
    )
    return dependency_id


def _statement_plan(
    session,
    project,
    *,
    ref_code: str,
    assignee_index: int = 1,
    due_date: date | None = None,
) -> int:
    from corridor.models import Dependency

    dependency = Dependency(
        project_id=project.id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title=f"Statement subject {ref_code}",
    )
    session.add(dependency)
    session.flush()
    party = ExternalOrg(name=f"Party {ref_code}")
    session.add(party)
    session.flush()
    dependency.external_org_id = party.id
    session.flush()
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="verbal",
        event_date=date(2026, 8, 12),
        description=f"Party {ref_code} will provide the relocation schedule.",
        new_timing=StatementTiming.day("August 20, 2026", date(2026, 8, 20)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
    )
    lineage_id = event.commitment_lineage_id
    subject = CoordinationSubject.statement(lineage_id)
    roster = _roster(
        session,
        project,
        _assignee(assignee_index),
        display_name=f"Owner {assignee_index}",
    )
    _assign(session, subject, roster)
    set_next_action(
        session,
        subject,
        "Confirm the stated timing with the organization",
        due_date=due_date,
        principal=RECORDER,
    )
    return lineage_id


def _derive(session, project, *, today=TODAY, urgent_overdue_days=3, escalation=None):
    return notifications.derive_due_action_conditions(
        session,
        project_id=project.id,
        today=today,
        urgent_overdue_days=urgent_overdue_days,
        escalation_roster_entry_id=escalation,
    )


# --- Constraint derivation over the existing check semantics ---------------


def test_constraint_soon_due_reminder_is_derived(session, project):
    _constraint_plan(session, project, ref_code="C-1", due_date=date(2026, 9, 2))

    derivation = _derive(session, project)

    assert len(derivation.reminders) == 1
    reminder = derivation.reminders[0]
    assert reminder.subject_kind == "constraint"
    assert reminder.urgency == "soon"
    assert reminder.action_due_date == date(2026, 9, 2)
    assert reminder.assignee.principal_subject == _assignee(1).subject


def test_constraint_overdue_reminder_is_derived(session, project):
    _constraint_plan(session, project, ref_code="C-2", due_date=date(2026, 8, 25))

    derivation = _derive(session, project)

    assert len(derivation.reminders) == 1
    assert derivation.reminders[0].urgency == "overdue"
    assert derivation.reminders[0].days == 5


def test_unknown_action_due_date_is_never_an_overdue_finding(session, project):
    _constraint_plan(
        session,
        project,
        ref_code="C-3",
        due_date=None,
        due_date_unknown_reason="awaiting_external_information",
    )

    derivation = _derive(session, project)

    assert derivation.reminders == ()


def test_external_timing_does_not_substitute_for_action_due_date(session, project):
    # A Need Date and a Committed Date are set, but no Action Due Date: the
    # project's own action timing is unknown, so nothing fires (ADR-0038).
    from corridor.models import Dependency

    dependency_id = _constraint_plan(
        session,
        project,
        ref_code="C-4",
        due_date=None,
        due_date_unknown_reason="awaiting_schedule_information",
    )
    dependency = session.get(Dependency, dependency_id)
    dependency.need_date = date(2026, 9, 1)
    dependency.committed_date = date(2026, 8, 20)
    session.flush()

    derivation = _derive(session, project)

    assert derivation.reminders == ()


def test_action_due_beyond_horizon_is_not_soon(session, project):
    _constraint_plan(session, project, ref_code="C-5", due_date=date(2026, 10, 30))

    assert _derive(session, project).reminders == ()


def test_soon_horizon_boundary_is_inclusive(session, project):
    # Default action_due_soon_days is 7; exactly 7 days out is soon, 8 is not.
    roster = _roster(session, project, _assignee(1), display_name="Owner 1")
    _constraint_plan(
        session, project, ref_code="C-6a", due_date=date(2026, 9, 6), roster=roster
    )
    _constraint_plan(
        session, project, ref_code="C-6b", due_date=date(2026, 9, 7), roster=roster
    )

    reminders = _derive(session, project).reminders

    assert len(reminders) == 1
    assert reminders[0].action_due_date == date(2026, 9, 6)


def test_due_today_is_soon_not_overdue(session, project):
    _constraint_plan(session, project, ref_code="C-7", due_date=TODAY)

    reminder = _derive(session, project).reminders[0]

    assert reminder.urgency == "soon"
    assert reminder.days == 0


# --- Statement derivation over the same thresholds -------------------------


def test_statement_soon_due_reminder_is_derived(session, project):
    _statement_plan(session, project, ref_code="S-1", due_date=date(2026, 9, 2))

    derivation = _derive(session, project)

    assert len(derivation.reminders) == 1
    assert derivation.reminders[0].subject_kind == "statement"
    assert derivation.reminders[0].urgency == "soon"


def test_statement_plan_marked_for_review_is_withheld(session, project):
    from corridor.models import CommitmentLineage

    lineage_id = _statement_plan(
        session, project, ref_code="S-2", due_date=date(2026, 8, 25)
    )
    lineage = session.get(CommitmentLineage, lineage_id)
    lineage.plan_needs_review = True
    session.flush()

    assert _derive(session, project).reminders == ()


# --- Complete population beyond the paginated Work List --------------------


def test_derivation_covers_the_complete_population(session, project):
    roster = _roster(session, project, _assignee(1), display_name="Owner 1")
    for index in range(25):
        _constraint_plan(
            session,
            project,
            ref_code=f"POP-{index}",
            due_date=date(2026, 9, 1),
            roster=roster,
        )

    derivation = _derive(session, project)

    assert len(derivation.reminders) == 25


# --- Deferral suppresses immediate reminders until its return date ---------


def test_deferred_item_is_withheld_until_its_return_date(session, project):
    dependency_id = _constraint_plan(
        session, project, ref_code="D-1", due_date=date(2026, 8, 25)
    )
    subject = CoordinationSubject.dependency(dependency_id)
    defer_work(
        session,
        subject,
        reason="waiting_for_external_party",
        return_date=date(2026, 9, 5),
        principal=RECORDER,
    )

    # While deferred (today < return date) the immediate reminder is withheld.
    assert _derive(session, project, today=date(2026, 8, 30)).reminders == ()
    # On and after the return date it is derived again.
    assert len(_derive(session, project, today=date(2026, 9, 6)).reminders) == 1


# --- Urgent overdue, escalation, and deduplication -------------------------


def test_urgent_overdue_escalates_to_assignee_and_one_contact(session, project):
    escalation = _roster(
        session, project, _assignee(9), display_name="Escalation Contact"
    )
    _constraint_plan(session, project, ref_code="E-1", due_date=date(2026, 8, 20))

    derivation = _derive(
        session, project, urgent_overdue_days=3, escalation=escalation.id
    )

    assert len(derivation.reminders) == 1
    assert derivation.reminders[0].urgent is True
    assert len(derivation.escalations) == 1
    assert derivation.escalations[0].escalation.principal_subject == _assignee(9).subject
    assert derivation.escalation_enabled is True


def test_escalation_deduplicates_when_contact_is_the_assignee(session, project):
    shared = _roster(session, project, _assignee(1), display_name="Owner 1")
    _constraint_plan(
        session, project, ref_code="E-2", due_date=date(2026, 8, 20), roster=shared
    )

    derivation = _derive(
        session, project, urgent_overdue_days=3, escalation=shared.id
    )

    # The assignee still gets the overdue reminder, but no separate escalation is
    # raised to the same person, and the team is never broadcast to.
    assert len(derivation.reminders) == 1
    assert derivation.escalations == ()


def test_missing_escalation_leaves_escalation_disabled_and_visible(session, project):
    _constraint_plan(session, project, ref_code="E-3", due_date=date(2026, 8, 20))

    derivation = _derive(session, project, urgent_overdue_days=3, escalation=None)

    assert len(derivation.reminders) == 1
    assert derivation.escalations == ()
    assert derivation.escalation_enabled is False
    assert derivation.escalation_disabled_reason == "no escalation contact configured"


def test_overdue_below_urgent_threshold_does_not_escalate(session, project):
    escalation = _roster(
        session, project, _assignee(9), display_name="Escalation Contact"
    )
    _constraint_plan(session, project, ref_code="E-4", due_date=date(2026, 8, 29))

    derivation = _derive(
        session, project, urgent_overdue_days=5, escalation=escalation.id
    )

    assert derivation.reminders[0].urgent is False
    assert derivation.escalations == ()


# --- Registration converges on one occurrence per condition ----------------


def _register(session, project, *, today=TODAY, urgent_overdue_days=3, escalation=None,
              register_summary=False, window=(TODAY, TODAY)):
    return notifications.register_due_action_notifications(
        session,
        project_id=project.id,
        configuration_version="due-action-notification-v1",
        today=today,
        urgent_overdue_days=urgent_overdue_days,
        escalation_roster_entry_id=escalation,
        channel="email",
        owner="runtime:test",
        register_summary=register_summary,
        summary_window_start=window[0],
        summary_window_end=window[1],
    )


def _occurrence_count(session, project):
    return session.scalar(
        select(func.count()).select_from(DueActionNotification).where(
            DueActionNotification.project_id == project.id
        )
    )


def test_unchanged_condition_is_not_a_new_event_on_every_poll(session, project):
    _constraint_plan(session, project, ref_code="R-1", due_date=date(2026, 9, 2))

    _register(session, project)
    _register(session, project)
    _register(session, project)

    assert _occurrence_count(session, project) == 1
    assert (
        session.scalar(
            select(func.count()).select_from(DueActionNotificationDispatch).where(
                DueActionNotificationDispatch.project_id == project.id
            )
        )
        == 1
    )


def test_a_changed_plan_is_a_new_occurrence(session, project):
    dependency_id = _constraint_plan(
        session, project, ref_code="R-2", due_date=date(2026, 9, 2)
    )
    _register(session, project)
    # A new Next Action decision (a new due date) changes the plan identity.
    set_next_action(
        session,
        CoordinationSubject.dependency(dependency_id),
        "Coordinate this constraint with the organization",
        due_date=date(2026, 9, 4),
        principal=RECORDER,
    )
    _register(session, project)

    assert _occurrence_count(session, project) == 2


def test_soon_to_overdue_crossing_is_a_new_occurrence(session, project):
    _constraint_plan(session, project, ref_code="R-3", due_date=date(2026, 9, 1))

    _register(session, project, today=date(2026, 8, 30))  # soon
    _register(session, project, today=date(2026, 9, 5))   # overdue

    urgencies = set(
        session.scalars(
            select(DueActionNotification.urgency).where(
                DueActionNotification.project_id == project.id
            )
        ).all()
    )
    assert urgencies == {"soon", "overdue"}


def test_registration_records_binding_and_check_identity(session, project):
    _constraint_plan(session, project, ref_code="R-4", due_date=date(2026, 9, 2))

    _register(session, project)

    occurrence = session.scalar(
        select(DueActionNotification).where(
            DueActionNotification.project_id == project.id
        )
    )
    assert occurrence.category == "next_action_due"
    assert occurrence.recipient_role == "assignee"
    assert occurrence.plan_decision_id is not None
    assert occurrence.check_identity is not None
    assert occurrence.observation_start == TODAY
    assert occurrence.observation_end == TODAY


# --- Daily summaries ------------------------------------------------------


def test_daily_summary_is_one_occurrence_per_recipient_and_window(session, project):
    roster = _roster(session, project, _assignee(1), display_name="Owner 1")
    _constraint_plan(
        session, project, ref_code="SUM-1", due_date=date(2026, 9, 1), roster=roster
    )
    _constraint_plan(
        session, project, ref_code="SUM-2", due_date=date(2026, 8, 20), roster=roster
    )

    _register(
        session,
        project,
        register_summary=True,
        window=(date(2026, 8, 30), date(2026, 8, 30)),
    )

    summaries = session.scalars(
        select(DueActionNotification).where(
            DueActionNotification.project_id == project.id,
            DueActionNotification.category == "daily_summary",
        )
    ).all()
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.recipient_principal_subject == _assignee(1).subject
    assert summary.observation_start == date(2026, 8, 30)
    assert summary.observation_end == date(2026, 8, 30)
    assert summary.summary_json["counts"]["soon"] == 1
    assert summary.summary_json["counts"]["overdue"] == 1
    assert summary.summary_json["window_start"] == "2026-08-30"


def test_daily_summary_is_idempotent_within_a_window(session, project):
    _constraint_plan(session, project, ref_code="SUM-3", due_date=date(2026, 9, 1))

    _register(session, project, register_summary=True)
    _register(session, project, register_summary=True)

    assert (
        session.scalar(
            select(func.count()).select_from(DueActionNotification).where(
                DueActionNotification.project_id == project.id,
                DueActionNotification.category == "daily_summary",
            )
        )
        == 1
    )


# --- Gate-7 configuration --------------------------------------------------


def test_gate7_valid_configuration_enables_the_schedule(session, project):
    schedule = configure_due_action_notification(
        session,
        DueActionNotificationDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="due-action-notification-v1",
            starts_at=NOW,
        ),
        now=NOW,
    )

    assert schedule.handler_key == "due_action_notification"
    assert schedule.disabled_at is None
    status = due_work_status(session, project_id=project.id)
    assert status["jobs"][0]["handler"] == "due_action_notification"
    assert status["jobs"][0]["notification_budget"] == 500


@pytest.mark.parametrize(
    "overrides",
    [
        {"notification_budget": 0},
        {"urgent_overdue_days": 0},
        {"summary_hour_utc": 24},
        {"summary_window_days": 0},
    ],
)
def test_gate7_invalid_configuration_refuses_and_writes_nothing(
    session, project, overrides
):
    with pytest.raises(DueWorkRefusal):
        configure_due_action_notification(
            session,
            DueActionNotificationDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="due-action-notification-v1",
                starts_at=NOW,
                **overrides,
            ),
            now=NOW,
        )
    assert (
        session.scalar(
            select(func.count()).select_from(DueWorkSchedule).where(
                DueWorkSchedule.project_id == project.id
            )
        )
        == 0
    )


def test_gate7_model_budget_must_be_zero(session, project):
    declaration = DueActionNotificationDeclaration.released_hourly(
        project_id=project.id,
        configuration_version="due-action-notification-v1",
        starts_at=NOW,
    )
    tampered = DueActionNotificationDeclaration(
        **{**declaration.__dict__, "model_token_budget": 1}
    )
    with pytest.raises(DueWorkRefusal):
        configure_due_action_notification(session, tampered, now=NOW)


def test_gate7_escalation_contact_must_be_an_active_member(session, project):
    other = Project(slug="other-due", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    foreign = _roster(
        session, other, _assignee(9), display_name="Foreign Contact"
    )
    with pytest.raises(DueWorkRefusal):
        configure_due_action_notification(
            session,
            DueActionNotificationDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="due-action-notification-v1",
                starts_at=NOW,
                escalation_roster_entry_id=foreign.id,
            ),
            now=NOW,
        )


def test_completion_removes_the_condition_from_the_next_derivation(session, project):
    dependency_id = _constraint_plan(
        session, project, ref_code="X-1", due_date=date(2026, 8, 25)
    )
    assert len(_derive(session, project).reminders) == 1
    complete_next_action(
        session,
        CoordinationSubject.dependency(dependency_id),
        principal=RECORDER,
        no_follow_up_reason="no_immediate_follow_up",
    )
    assert _derive(session, project).reminders == ()


def test_cancellation_removes_the_condition_from_the_next_derivation(session, project):
    dependency_id = _constraint_plan(
        session, project, ref_code="X-2", due_date=date(2026, 8, 25)
    )
    cancel_next_action(
        session,
        CoordinationSubject.dependency(dependency_id),
        principal=RECORDER,
        cancellation_reason="no_longer_needed",
        no_follow_up_reason="no_immediate_follow_up",
    )
    assert _derive(session, project).reminders == ()
