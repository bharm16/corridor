"""Project-scoped access for the assignment inbox, flag path, and ops view (#351).

These drive the real HTTP routes with the #331 access gate: a member sees only
their own inbox in one project, only the assigned person may flag, the operator
view needs the technical-operations designation, and no route leaks another
project's records.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor import access, notifications
from corridor.db import Session, engine
from corridor.models import (
    AssignmentNotification,
    AssignmentNotificationFeedback,
    Dependency,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
)
from corridor.principals import HumanPrincipal
from corridor.web.app import app, get_human_principal, get_session
from corridor.work_decisions import CoordinationSubject, assign_internal_owner
from access_support import request_scoped, seed_membership

COORDINATOR = HumanPrincipal("local:web-coordinator")
ASSIGNEE = HumanPrincipal("local:web-assignee")
OTHER_MEMBER = HumanPrincipal("local:web-other")
OPERATOR = HumanPrincipal("local:web-operator")


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
    project = Project(slug="notify-web", name="Notify Web", is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _acting_client(session, principal, *, per_request_transaction=False):
    """A client acting as ``principal`` over the test's session.

    ``per_request_transaction`` gives each simulated request the transaction
    boundary a real request has (``access_support.request_scoped``). A test
    that signs in as two people needs it: the project-authorization scope is
    declared per request and one transaction holds one, so without a boundary
    the second person's request inherits the first person's declaration and is
    refused (#657, #662). That boundary rolls each request back, so only a test
    whose requests read may ask for it — a test that asserts what a request
    wrote must keep the shared session.
    """
    if per_request_transaction:
        override_session = request_scoped(session)
    else:

        def override_session():
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_human_principal] = lambda: principal
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _identity(session, principal, email):
    if session.scalar(
        select(PersonIdentity).where(
            PersonIdentity.principal_subject == principal.subject
        )
    ):
        return
    session.add(
        PersonIdentity(email_normalized=email, principal_subject=principal.subject)
    )
    session.flush()


def _assignment(session, project, assignee=ASSIGNEE, email="assignee@example.com"):
    dependency = Dependency(
        project_id=project.id,
        ref_code="NW-1",
        dep_type="utility_relocation",
        title="Water main",
    )
    session.add(dependency)
    session.flush()
    roster = seed_membership(
        session,
        project,
        assignee,
        designations=(access.COORDINATION,),
        display_name="Assignee",
    )
    _identity(session, assignee, email)
    decision = assign_internal_owner(
        session,
        CoordinationSubject.dependency(dependency.id),
        roster.display_name,
        principal=COORDINATOR,
    )
    notification = notifications.register_new_assignment_notification(
        session, assignment_decision=decision, roster_entry=roster, principal=COORDINATOR
    )
    return dependency, roster, notification


def test_inbox_shows_only_the_signed_in_members_own_assignments(session, project):
    """Two people read the same project's inbox; each sees only their own.

    Both requests only read, so each takes a real request's transaction
    boundary: the scope a request declares names the person as well as the
    project, and a transaction holds one (#657, #662).
    """
    _assignment(session, project)
    seed_membership(session, project, OTHER_MEMBER, designations=(access.COORDINATION,))

    assignee_client = _acting_client(session, ASSIGNEE, per_request_transaction=True)
    response = assignee_client.get("/assignments/notify-web/inbox")
    assert response.status_code == 200
    assert "Constraint NW-1" in response.text

    # A different member sees none of the assignee's inbox.
    other_client = _acting_client(session, OTHER_MEMBER, per_request_transaction=True)
    other_response = other_client.get("/assignments/notify-web/inbox")
    assert other_response.status_code == 200
    assert "Constraint NW-1" not in other_response.text


def test_inbox_refuses_a_non_member_like_a_missing_project(session, project):
    _assignment(session, project)
    outsider_client = _acting_client(session, HumanPrincipal("local:web-outsider"))
    response = outsider_client.get("/assignments/notify-web/inbox")
    assert response.status_code == 404


def test_assigned_person_flags_incorrect_without_changing_the_assignment(
    session, project
):
    _dependency, _roster, notification = _assignment(session, project)
    client = _acting_client(session, ASSIGNEE)
    response = client.post(
        f"/assignments/notify-web/notifications/{notification.id}/flag-incorrect",
        data={"note": "belongs to drainage"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotificationFeedback).where(
                AssignmentNotificationFeedback.notification_id == notification.id
            )
        )
        == 1
    )


def test_non_recipient_member_cannot_flag(session, project):
    _dependency, _roster, notification = _assignment(session, project)
    seed_membership(session, project, OTHER_MEMBER, designations=(access.COORDINATION,))
    client = _acting_client(session, OTHER_MEMBER)
    response = client.post(
        f"/assignments/notify-web/notifications/{notification.id}/flag-incorrect",
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert (
        session.scalar(
            select(func.count()).select_from(AssignmentNotificationFeedback)
        )
        == 0
    )


def test_flag_refuses_a_notification_from_another_project(session, project):
    _dependency, _roster, notification = _assignment(session, project)
    other = Project(slug="notify-web-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    # The assignee is also a member of the other project, but the notification
    # belongs to the first project, so the other project's route must refuse it.
    seed_membership(session, other, ASSIGNEE, designations=(access.COORDINATION,))
    client = _acting_client(session, ASSIGNEE)
    response = client.post(
        f"/assignments/notify-web-other/notifications/{notification.id}/flag-incorrect",
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_operations_delivery_view_requires_technical_operations(session, project):
    _assignment(session, project)
    # The assignee has coordination but not technical operations.
    member_client = _acting_client(session, ASSIGNEE)
    forbidden = member_client.get("/operations/notify-web/deliveries")
    assert forbidden.status_code == 403

    seed_membership(
        session, project, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    operator_client = _acting_client(session, OPERATOR)
    allowed = operator_client.get("/operations/notify-web/deliveries")
    assert allowed.status_code == 200
    assert "Constraint NW-1" in allowed.text
    assert "Delivery is disabled" in allowed.text


def test_operations_delivery_view_refuses_a_non_member(session, project):
    _assignment(session, project)
    outsider = _acting_client(session, HumanPrincipal("local:web-outsider"))
    response = outsider.get("/operations/notify-web/deliveries")
    assert response.status_code == 404
