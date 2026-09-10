"""New-assignment notification registration, resolution, and feedback (#351).

These tests drive the domain seams through a rollback-scoped session: a committed
assignment registers exactly one occurrence bound to its subject, decision, and
roster identity; recipients resolve only through typed verified contacts; the
assigned person can flag an incorrect assignment without changing it; and reads
never leak across projects. The delivery sweep (which owns its own committed
transactions through the shared runtime) is proven in test_notifications_runtime.
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

from corridor import notifications
from corridor.due_work import (
    AssignmentNotificationDeclaration,
    configure_assignment_notification,
)
from corridor.external_statements import (
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.models import (
    AssignmentNotification,
    AssignmentNotificationDispatch,
    AssignmentNotificationFeedback,
    AuditLog,
    Dependency,
    ExternalOrg,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
)
from corridor.principals import HumanPrincipal
from corridor.work_decisions import (
    CoordinationSubject,
    FollowUpPlanDraft,
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    assign_internal_owner,
    current_internal_owner_decision,
    save_follow_up_plan,
)

RECORDER = HumanPrincipal("local:notify-coordinator")
ASSIGNEE = HumanPrincipal("local:notify-assignee")


@pytest.fixture
def dependency(session, project):
    dependency = Dependency(
        project_id=project.id,
        ref_code="NT-1",
        dep_type="utility_relocation",
        title="Water main at 1102+20",
    )
    session.add(dependency)
    session.flush()
    return dependency


def _roster(
    session,
    project,
    principal: HumanPrincipal,
    *,
    display_name: str = "Dana Assignee",
    active: bool = True,
    email: str | None = "dana@example.com",
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
    if email is not None:
        # Identity is global (one email <-> one principal), so a principal who
        # is a member of two projects keeps one verified-contact record.
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


def _constraint_owner_decision(session, dependency, roster):
    return assign_internal_owner(
        session,
        CoordinationSubject.dependency(dependency.id),
        roster.display_name,
        principal=RECORDER,
    )


def _statement_owner_decision(session, project, dependency, roster):
    party = ExternalOrg(name="Notify Statement Party")
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
        description="Notify Statement Party will provide the relocation schedule.",
        new_timing=StatementTiming.day("August 20, 2026", date(2026, 8, 20)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
    )
    decision = assign_internal_owner(
        session,
        CoordinationSubject.statement(event.commitment_lineage_id),
        roster.display_name,
        principal=RECORDER,
    )
    return event, decision


# --- Registration binds subject + decision + identity ---------------------


def test_constraint_assignment_registers_one_bound_occurrence(
    session, project, dependency
):
    roster = _roster(session, project, ASSIGNEE)
    decision = _constraint_owner_decision(session, dependency, roster)

    notification = notifications.register_new_assignment_notification(
        session,
        assignment_decision=decision,
        roster_entry=roster,
        principal=RECORDER,
    )

    assert notification.category == "new_assignment"
    assert notification.subject_kind == "constraint"
    assert notification.dependency_id == dependency.id
    assert notification.commitment_lineage_id is None
    assert notification.assignment_decision_id == decision.id
    assert notification.recipient_roster_entry_id == roster.id
    assert notification.recipient_principal_subject == ASSIGNEE.subject
    assert notification.registered_by == RECORDER.subject
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotification).where(
                AssignmentNotification.project_id == project.id
            )
        )
        == 1
    )
    dispatch = session.scalar(
        select(AssignmentNotificationDispatch).where(
            AssignmentNotificationDispatch.notification_id == notification.id
        )
    )
    assert dispatch.delivery_state == "queued"
    assert dispatch.channel == "email"


def test_statement_assignment_registers_one_bound_occurrence(
    session, project, dependency
):
    roster = _roster(session, project, ASSIGNEE)
    event, decision = _statement_owner_decision(session, project, dependency, roster)

    notification = notifications.register_new_assignment_notification(
        session,
        assignment_decision=decision,
        roster_entry=roster,
        principal=RECORDER,
    )

    assert notification.subject_kind == "statement"
    assert notification.commitment_lineage_id == event.commitment_lineage_id
    assert notification.dependency_id is None
    assert notification.assignment_decision_id == decision.id


# --- Recipient resolution: only typed verified contacts -------------------


def test_verified_contact_resolves_recipient(session, project, dependency):
    roster = _roster(session, project, ASSIGNEE, email="dana@example.com")
    decision = _constraint_owner_decision(session, dependency, roster)
    notification = notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    dispatch = session.scalar(
        select(AssignmentNotificationDispatch).where(
            AssignmentNotificationDispatch.notification_id == notification.id
        )
    )
    assert dispatch.recipient_contact == "dana@example.com"
    assert dispatch.delivery_limitation is None


def test_unresolved_legacy_mapping_is_a_visible_limitation_not_a_guessed_address(
    session, project, dependency
):
    # No PersonIdentity for the principal: the roster identity has no typed
    # verified contact, so delivery is limited and no address is invented.
    roster = _roster(session, project, ASSIGNEE, email=None)
    decision = _constraint_owner_decision(session, dependency, roster)

    before = current_internal_owner_decision(
        session, CoordinationSubject.dependency(dependency.id)
    )
    notification = notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    dispatch = session.scalar(
        select(AssignmentNotificationDispatch).where(
            AssignmentNotificationDispatch.notification_id == notification.id
        )
    )
    assert dispatch.recipient_contact is None
    assert dispatch.delivery_limitation == notifications.LIMITATION_UNRESOLVED_CONTACT
    # The assignment itself is unchanged by the delivery limitation.
    after = current_internal_owner_decision(
        session, CoordinationSubject.dependency(dependency.id)
    )
    assert after.id == before.id
    session.refresh(dependency)
    assert dependency.internal_owner == roster.display_name


def test_repeated_registration_converges_on_one_occurrence(
    session, project, dependency
):
    roster = _roster(session, project, ASSIGNEE)
    decision = _constraint_owner_decision(session, dependency, roster)
    first = notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    second = notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    assert first.id == second.id
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotification).where(
                AssignmentNotification.project_id == project.id
            )
        )
        == 1
    )
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotificationDispatch).where(
                AssignmentNotificationDispatch.project_id == project.id
            )
        )
        == 1
    )


def test_reassignment_registers_a_distinct_occurrence(session, project, dependency):
    first_roster = _roster(session, project, ASSIGNEE)
    first_decision = _constraint_owner_decision(session, dependency, first_roster)
    notifications.register_new_assignment_notification(
        session,
        assignment_decision=first_decision,
        roster_entry=first_roster,
        principal=RECORDER,
    )
    other = HumanPrincipal("local:notify-second")
    second_roster = _roster(
        session, project, other, display_name="Sam Second", email="sam@example.com"
    )
    second_decision = assign_internal_owner(
        session,
        CoordinationSubject.dependency(dependency.id),
        second_roster.display_name,
        principal=RECORDER,
    )
    assert second_decision.id != first_decision.id
    notifications.register_new_assignment_notification(
        session,
        assignment_decision=second_decision,
        roster_entry=second_roster,
        principal=RECORDER,
    )
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotification).where(
                AssignmentNotification.project_id == project.id
            )
        )
        == 2
    )


# --- The committed entry points register through the shared registrar ------


def test_save_follow_up_plan_registers_a_notification(session, project, dependency):
    roster = _roster(session, project, ASSIGNEE)
    result = save_follow_up_plan(
        session,
        FollowUpPlanDraft(
            dependency_id=dependency.id,
            internal_owner_roster_entry_id=roster.id,
            next_action=FOLLOW_UP_NEXT_ACTION_CHOICES[0],
            action_due_date=date(2026, 9, 1),
            action_due_date_unknown_reason=None,
        ),
        principal=RECORDER,
    )
    notification = session.scalar(
        select(AssignmentNotification).where(
            AssignmentNotification.project_id == project.id
        )
    )
    assert notification is not None
    assert notification.subject_kind == "constraint"
    assert notification.assignment_decision_id == result.internal_owner_decision.id
    assert notification.recipient_roster_entry_id == roster.id


def test_rolled_back_save_registers_no_dispatch(session, project, dependency):
    roster = _roster(session, project, ASSIGNEE)
    # A structured-choice violation refuses the whole Save; its nested
    # transaction rolls back, so no notification is left behind.
    from corridor.work_decisions import FollowUpPlanRefusal

    with pytest.raises(FollowUpPlanRefusal):
        save_follow_up_plan(
            session,
            FollowUpPlanDraft(
                dependency_id=dependency.id,
                internal_owner_roster_entry_id=roster.id,
                next_action="free text that is not a structured choice",
                action_due_date=date(2026, 9, 1),
                action_due_date_unknown_reason=None,
            ),
            principal=RECORDER,
        )
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotification)
        )
        == 0
    )
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotificationDispatch)
        )
        == 0
    )


# --- Wrong-assignment feedback preserves the assignment --------------------


def test_assigned_person_can_flag_without_changing_the_assignment(
    session, project, dependency
):
    roster = _roster(session, project, ASSIGNEE)
    decision = _constraint_owner_decision(session, dependency, roster)
    notification = notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )

    feedback = notifications.flag_incorrect_assignment(
        session,
        notification_id=notification.id,
        principal=ASSIGNEE,
        note="This should go to the drainage lead.",
    )
    assert feedback.feedback_kind == "incorrect_assignment"
    assert feedback.flagged_by == ASSIGNEE.subject
    # An attributable audit entry points to the feedback act.
    audit_action = session.scalar(
        select(AuditLog.action).where(AuditLog.id == feedback.audit_log_id)
    )
    assert audit_action == "flag_incorrect_assignment"
    # The assignment and its projection are untouched.
    after = current_internal_owner_decision(
        session, CoordinationSubject.dependency(dependency.id)
    )
    assert after.id == decision.id
    session.refresh(dependency)
    assert dependency.internal_owner == roster.display_name


def test_only_the_assigned_person_may_flag(session, project, dependency):
    roster = _roster(session, project, ASSIGNEE)
    decision = _constraint_owner_decision(session, dependency, roster)
    notification = notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    # Another member of the same project is not the assigned person.
    _roster(
        session,
        project,
        RECORDER,
        display_name="Coordinator",
        email="coord@example.com",
    )
    with pytest.raises(notifications.NotificationAccessRefusal):
        notifications.flag_incorrect_assignment(
            session, notification_id=notification.id, principal=RECORDER
        )
    # A non-member cannot flag either.
    outsider = HumanPrincipal("local:outsider")
    with pytest.raises(notifications.NotificationAccessRefusal):
        notifications.flag_incorrect_assignment(
            session, notification_id=notification.id, principal=outsider
        )


def test_flagging_is_idempotent(session, project, dependency):
    roster = _roster(session, project, ASSIGNEE)
    decision = _constraint_owner_decision(session, dependency, roster)
    notification = notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    first = notifications.flag_incorrect_assignment(
        session, notification_id=notification.id, principal=ASSIGNEE
    )
    second = notifications.flag_incorrect_assignment(
        session, notification_id=notification.id, principal=ASSIGNEE
    )
    assert first.id == second.id
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotificationFeedback)
        )
        == 1
    )


# --- Reads: inbox and operations view, scoped and non-leaking --------------


def test_recipient_inbox_is_scoped_to_member_and_project(session, project, dependency):
    roster = _roster(session, project, ASSIGNEE)
    decision = _constraint_owner_decision(session, dependency, roster)
    notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    # A second project with its own assignment for the same principal.
    other_project = Project(slug="notify-other", name="Other", is_synthetic=True)
    session.add(other_project)
    session.flush()
    other_dependency = Dependency(
        project_id=other_project.id,
        ref_code="NO-1",
        dep_type="utility_relocation",
        title="Other",
    )
    session.add(other_dependency)
    session.flush()
    other_roster = _roster(session, other_project, ASSIGNEE, email="dana@example.com")
    other_decision = assign_internal_owner(
        session,
        CoordinationSubject.dependency(other_dependency.id),
        other_roster.display_name,
        principal=RECORDER,
    )
    notifications.register_new_assignment_notification(
        session,
        assignment_decision=other_decision,
        roster_entry=other_roster,
        principal=RECORDER,
    )

    inbox = notifications.recipient_inbox(
        session, project_id=project.id, principal_subject=ASSIGNEE.subject
    )
    assert len(inbox) == 1
    assert inbox[0]["subject_kind"] == "constraint"
    assert inbox[0]["subject_label"] == "Constraint NT-1"
    # A different member sees none of this member's inbox.
    assert (
        notifications.recipient_inbox(
            session, project_id=project.id, principal_subject=RECORDER.subject
        )
        == []
    )


def test_operations_delivery_view_counts_and_reports_enabled(
    session, project, dependency
):
    roster = _roster(session, project, ASSIGNEE)
    decision = _constraint_owner_decision(session, dependency, roster)
    notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=RECORDER
    )
    view = notifications.operations_delivery_view(session, project_id=project.id)
    assert view["delivery_enabled"] is False
    assert view["counts"]["queued"] == 1
    assert len(view["deliveries"]) == 1
    assert view["deliveries"][0]["recipient"] == ASSIGNEE.subject

    # Recording a valid gate-7 configuration marks delivery enabled.
    configure_assignment_notification(
        session,
        AssignmentNotificationDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="assignment-notification-v1",
            starts_at=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
        ),
        now=datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc),
    )
    enabled_view = notifications.operations_delivery_view(session, project_id=project.id)
    assert enabled_view["delivery_enabled"] is True
