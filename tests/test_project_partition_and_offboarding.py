"""Project authorization as a data partition, and offboarding that really revokes (#531).

Two claims are proved here, and both are proved by doing the thing rather than
by reading a flag.

**The partition.** ADR-0083 says a project is an authorization *and data
partition* boundary inside the customer database. Until #531 that boundary was
``_authorize`` plus a ``project_id ==`` in every reader, which one missing
predicate defeats. So the partition tests connect as the real ``corridor_web``
login against committed rows and run the query a careless reader would write —
``select * from source_segments``, no predicate at all — and assert it returns
one project's rows. They also forge the session setting by hand and assert the
forged partition is the empty one.

**Revocation.** "Tests proving revoked access" cannot mean asserting that
``active`` became false. So the offboarding tests drive the real HTTP routes
with the cookie a real magic link established, offboard the person, and assert
the very next request is refused — and that a link already sitting in their
inbox can no longer open a session.

No test here reads the wall clock: ordering is by ``audit_log.id``, the
append-only watermark, and every moment a test needs is passed in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import NullPool

from corridor import access, audit, identity_audit
from corridor.config import settings
from corridor.db import Session, engine
from corridor.models import (
    AuditLog,
    Document,
    PersonIdentity,
    Project,
    ProjectRosterEntry,
    SignInToken,
    SourceSegment,
    WebSession,
)
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import app, get_session

OPERATOR = HumanPrincipal("local:operations")
LEAVER = HumanPrincipal("local:leaver")

WEB_PASSWORD = os.environ.get("CORRIDOR_WEB_DB_PASSWORD") or "corridor_web"


# --- The partition, as the deployed web login -----------------------------


def _web_url(database_name: str) -> str:
    return (
        make_url(settings.database_url)
        .set(
            database=database_name,
            username="corridor_web",
            password=WEB_PASSWORD,
        )
        .render_as_string(hide_password=False)
    )


def _segment(project_id: int, document_id: int, text_value: str) -> SourceSegment:
    return SourceSegment(
        project_id=project_id,
        document_id=document_id,
        kind="spreadsheet_cell",
        exact_text=text_value,
        content_sha256=sha256(text_value.encode()).hexdigest(),
        ordinal=1,
        sheet_name="Conflicts",
        cell_range="A2",
    )


@pytest.fixture
def two_projects(runtime_database):
    """Two committed projects with one segment each, and a member of only one."""

    with runtime_database.session_factory.begin() as owner:
        ours = Project(slug="ours", name="Ours", is_synthetic=True)
        theirs = Project(slug="theirs", name="Theirs", is_synthetic=True)
        owner.add_all([ours, theirs])
        owner.flush()
        documents = {}
        for index, project in enumerate((ours, theirs)):
            document = Document(
                project_id=project.id,
                sha256=str(index + 3) * 64,
                filename=f"{project.slug}.xlsx",
                doc_type="matrix",
                numbering_scheme="project-unique",
                pages=1,
                parse_status="parsed",
            )
            owner.add(document)
            owner.flush()
            documents[project.slug] = document.id
            owner.add(_segment(project.id, document.id, f"{project.slug}-UC-1"))
        owner.add(
            ProjectRosterEntry(
                project_id=ours.id,
                principal_subject=LEAVER.subject,
                display_name="Leaver",
                active=True,
                can_coordinate=True,
            )
        )
        ids = (ours.id, theirs.id)
    return ids


@pytest.fixture
def web_connection(runtime_database):
    """A connection held by the deployed web capability, not by the owner."""

    web_engine = create_engine(
        _web_url(runtime_database.name), poolclass=NullPool, future=True
    )
    with web_engine.connect() as connection:
        yield connection
    web_engine.dispose()


def test_a_reader_that_forgets_its_project_predicate_reads_only_its_partition(
    two_projects, web_connection
):
    """The whole point: the careless query is the safe one now.

    ``select * from source_segments`` names no project. Before #531 it returned
    every customer project's rows to whoever asked; now it returns the rows of
    the declared partition and nothing else.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        careless = web.scalars(select(SourceSegment)).all()

    assert [row.exact_text for row in careless] == ["ours-UC-1"]


def test_no_declared_partition_reads_nothing_at_all(two_projects, web_connection):
    """Fail closed: a connection that declared nothing is not a connection that sees all."""

    with OrmSession(bind=web_connection) as web:
        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []


def test_a_partition_the_database_did_not_seal_is_the_empty_partition(
    two_projects, web_connection
):
    """Setting the scope by hand buys nothing; the seal is what the policy reads.

    This is the difference between a data partition and an application filter.
    The web login can set the session setting — no privilege stops it — and it
    still reads nothing, because the seal it cannot compute does not verify.
    """

    _ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        web.execute(
            text("select set_config('corridor.project_partition', :scope, true)"),
            {"scope": str(theirs)},
        )
        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []


def test_a_forged_seal_is_refused_as_thoroughly_as_no_seal(
    two_projects, web_connection
):
    """A guessed seal is still not the seal; the secret is unreadable, not obscure."""

    _ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        web.execute(
            text("select set_config('corridor.project_partition', :scope, true)"),
            {"scope": str(theirs)},
        )
        web.execute(
            text("select set_config('corridor.project_partition_seal', :seal, true)"),
            {"seal": sha256(b"guess").hexdigest()},
        )
        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []


def test_the_web_capability_cannot_read_the_seal_secret(web_connection):
    """If the capability could read the secret, every proof above would be theatre."""

    with pytest.raises(ProgrammingError) as refused:
        web_connection.execute(text("select secret from project_partition_secrets"))
    web_connection.rollback()

    assert "permission denied for table project_partition_secrets" in str(
        refused.value
    )


def test_the_web_capability_cannot_seal_a_partition_of_its_own(
    two_projects, web_connection
):
    """Only the two commands that prove something may seal a scope.

    ``seal_project_partition`` asks no question — it is the half of the
    mechanism the proving commands call — so nothing is granted execute on it.
    """

    _ours, theirs = two_projects
    with pytest.raises(ProgrammingError) as refused:
        web_connection.execute(
            text("select seal_project_partition(:scope)"), {"scope": str(theirs)}
        )
    web_connection.rollback()
    assert "permission denied for function seal_project_partition" in str(
        refused.value
    )


def test_declaring_a_partition_for_a_project_you_are_not_on_is_refused(
    two_projects, web_connection
):
    """Membership is proved by the database, as the command's owner, not by the caller."""

    _ours, theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=theirs
            )
    web_connection.rollback()


def test_a_cross_project_reading_is_partitioned_by_active_membership(
    two_projects, web_connection
):
    """The #537 cross-project reading is not an exemption from the partition."""

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        scope = access.open_member_project_partition(
            web, principal_subject=LEAVER.subject
        )
        assert scope == (ours,)
        assert [row.exact_text for row in web.scalars(select(SourceSegment)).all()] == [
            "ours-UC-1"
        ]


def test_giving_up_the_partition_leaves_the_connection_reading_nothing(
    two_projects, web_connection
):
    """A unit of work that is finished with a project can say so and stop reading it."""

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        assert len(web.scalars(select(SourceSegment)).all()) == 1

        access.close_project_partition(web)

        assert access.current_project_partition(web) is None
        assert web.scalars(select(SourceSegment)).all() == []


def test_offboarding_leaves_a_live_connection_with_the_empty_partition(
    runtime_database, two_projects, web_connection
):
    """Revocation proved by a refused read, not by a flag.

    The connection is the same one that read a row a moment ago. After the
    offboarding commits, the same query on the same connection returns nothing
    and the partition it may declare is empty.
    """

    ours, _theirs = two_projects
    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web, principal_subject=LEAVER.subject, project_id=ours
        )
        assert len(web.scalars(select(SourceSegment)).all()) == 1

    with runtime_database.session_factory.begin() as owner:
        access.deprovision_principal(owner, principal=LEAVER, operator=OPERATOR)

    with OrmSession(bind=web_connection) as web:
        assert (
            access.open_member_project_partition(
                web, principal_subject=LEAVER.subject
            )
            == ()
        )
        assert web.scalars(select(SourceSegment)).all() == []
        with pytest.raises(access.PartitionRefused):
            access.open_project_partition(
                web, principal_subject=LEAVER.subject, project_id=ours
            )
    web_connection.rollback()


# --- Offboarding over the real sign-in path --------------------------------


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    opened = Session(bind=connection)
    yield opened
    opened.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def sender():
    return auth.RecordingEmailSender()


@pytest.fixture
def client(session, sender):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    with TestClient(app, base_url="https://testserver") as opened:
        yield opened
    app.dependency_overrides.clear()


def _project(session, slug="alpha") -> Project:
    project = Project(slug=slug, name=slug.title(), is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _enroll(session, project, principal, email, designations):
    return access.enroll_member(
        session,
        project_id=project.id,
        email=email,
        principal=principal,
        display_name="Leaver",
        designations=designations,
        operator=OPERATOR,
    )


def _sign_in(client, sender, email):
    response = client.post(
        "/sign-in/request", data={"email": email}, follow_redirects=False
    )
    assert response.status_code == 200
    token = parse_qs(urlsplit(sender.sent[-1][1]).query)["token"][0]
    consume = client.get(
        "/sign-in/consume", params={"token": token}, follow_redirects=False
    )
    assert consume.status_code == 303
    return consume


def test_the_next_request_after_offboarding_is_refused(session, client, sender):
    """The proof the criterion asks for: a real route, a real cookie, refused.

    The person is signed in and reading their project one request before the
    offboarding and refused one request after it, on the same cookie.
    """

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    _sign_in(client, sender, "leaver@example.com")

    before = client.get(f"/work/{project.slug}", follow_redirects=False)
    assert before.status_code == 200

    access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)

    after = client.get(f"/work/{project.slug}", follow_redirects=False)
    assert after.status_code == 401


def test_a_link_already_in_the_inbox_cannot_open_a_session_afterwards(
    session, client, sender
):
    """Deactivating the roster is not enough while an unspent link is still live."""

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    requested = client.post(
        "/sign-in/request",
        data={"email": "leaver@example.com"},
        follow_redirects=False,
    )
    assert requested.status_code == 200
    token = parse_qs(urlsplit(sender.sent[-1][1]).query)["token"][0]

    result = access.deprovision_principal(
        session, principal=LEAVER, operator=OPERATOR
    )
    assert result.tokens_invalidated == 1

    consumed = client.get(
        "/sign-in/consume", params={"token": token}, follow_redirects=False
    )
    assert consumed.status_code != 303
    assert client.cookies.get(auth.SESSION_COOKIE) is None
    assert (
        client.get(f"/work/{project.slug}", follow_redirects=False).status_code == 401
    )


def test_offboarding_deactivates_every_membership_and_strips_every_designation(
    session,
):
    """A later re-enrollment has to grant each authority again, not revive it."""

    first = _project(session, "one")
    second = _project(session, "two")
    _enroll(session, first, LEAVER, "leaver@example.com", access.DESIGNATIONS)
    _enroll(session, second, LEAVER, "leaver@example.com", [access.EXTERNAL_RELEASE])

    result = access.deprovision_principal(
        session, principal=LEAVER, operator=OPERATOR
    )

    assert result.projects_left == (first.id, second.id)
    entries = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.principal_subject == LEAVER.subject
        )
    ).all()
    assert [entry.active for entry in entries] == [False, False]
    assert not any(
        entry.can_coordinate
        or entry.can_review_documentation
        or entry.can_release_externally
        or entry.is_technical_operator
        for entry in entries
    )
    assert access.resolve_membership(session, LEAVER.subject, first.id) is None
    assert access.member_projects(session, LEAVER.subject) == []
    assert access.coordinated_projects(session, LEAVER.subject) == []


def test_offboarding_keeps_the_person_nameable_on_what_they_decided(session):
    """Access ends; authorship does not (ADR-0081, #503).

    An export that could no longer say who made an accepted decision would be a
    worse record, not a safer one.
    """

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    decided = audit.record(
        session,
        principal=LEAVER,
        action=audit.SET_NEXT_ACTION,
        entity_type=audit.PROJECT,
        entity_id=project.id,
        after={"next_action": "call the party"},
    )

    access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)

    identity = session.scalars(
        select(PersonIdentity).where(
            PersonIdentity.principal_subject == LEAVER.subject
        )
    ).first()
    assert identity is not None
    assert identity.email_normalized == "leaver@example.com"
    assert session.get(AuditLog, decided.id).human_principal == LEAVER.subject


def test_a_second_offboarding_revokes_nothing_a_second_time(session, client, sender):
    """Idempotent: the act is recorded, the revocations are not invented twice."""

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    _sign_in(client, sender, "leaver@example.com")

    first = access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)
    second = access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)

    assert first.sessions_revoked == 1
    assert second == access.Deprovisioning(
        principal_subject=LEAVER.subject,
        email_normalized="leaver@example.com",
        projects_left=(),
        sessions_revoked=0,
        tokens_invalidated=0,
    )


def test_every_live_session_of_one_person_is_revoked_together(session):
    """Two browsers is two sessions; offboarding one of them would be no offboarding."""

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    laptop = access.create_web_session(
        session, principal=LEAVER, email_normalized="leaver@example.com"
    )
    phone = access.create_web_session(
        session, principal=LEAVER, email_normalized="leaver@example.com"
    )

    result = access.deprovision_principal(
        session, principal=LEAVER, operator=OPERATOR
    )

    assert result.sessions_revoked == 2
    assert access.resolve_web_session(session, laptop.raw_session_id) is None
    assert access.resolve_web_session(session, phone.raw_session_id) is None
    assert (
        session.scalars(
            select(WebSession).where(
                WebSession.principal_subject == LEAVER.subject,
                WebSession.revoked_at.is_(None),
            )
        ).all()
        == []
    )


def test_a_lapsed_or_spent_link_is_not_counted_as_an_invalidated_one(session):
    """The receipt says what was taken away, not what had already gone."""

    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    moment = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    session.add(
        SignInToken(
            email_normalized="leaver@example.com",
            token_sha256="1" * 64,
            created_at=moment - timedelta(hours=1),
            expires_at=moment - timedelta(minutes=1),
        )
    )
    session.add(
        SignInToken(
            email_normalized="leaver@example.com",
            token_sha256="2" * 64,
            created_at=moment - timedelta(minutes=5),
            expires_at=moment + timedelta(minutes=10),
            consumed_at=moment - timedelta(minutes=4),
        )
    )
    session.add(
        SignInToken(
            email_normalized="leaver@example.com",
            token_sha256="3" * 64,
            created_at=moment - timedelta(minutes=2),
            expires_at=moment + timedelta(minutes=13),
        )
    )
    session.flush()

    result = access.deprovision_principal(
        session, principal=LEAVER, operator=OPERATOR, now=moment
    )

    assert result.tokens_invalidated == 1
    spent = {
        token.token_sha256: token.consumed_at
        for token in session.scalars(select(SignInToken)).all()
    }
    # The lapsed link is left exactly as it lapsed, the already-spent one keeps
    # the moment it was spent, and only the live one is taken away now.
    assert spent["1" * 64] is None
    assert spent["2" * 64] == moment - timedelta(minutes=4)
    assert spent["3" * 64] == moment


# --- The identity and authorization export ---------------------------------


def _watermark(session) -> int:
    """The last audit id before a test's own acts, so the export is read forward."""

    return int(session.scalar(select(AuditLog.id).order_by(AuditLog.id.desc())) or 0)


def test_the_export_carries_every_access_act_in_recorded_order(
    session, client, sender
):
    """Enrollment, sign-in, sign-out, and offboarding, ordered by the append-only id."""

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    _sign_in(client, sender, "leaver@example.com")
    client.post(
        "/sign-out",
        headers={auth.CSRF_HEADER: client.cookies.get(auth.CSRF_COOKIE)},
        follow_redirects=False,
    )
    access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)

    events = identity_audit.identity_events(session, after_id=watermark)

    assert [event.action for event in events] == [
        audit.ENROLL_PROJECT_MEMBER,
        audit.SIGN_IN,
        audit.SIGN_OUT,
        audit.DEPROVISION_PROJECT_MEMBER,
        audit.DEPROVISION_PRINCIPAL,
    ]
    assert [event.id for event in events] == sorted(event.id for event in events)
    offboarding = events[-1]
    assert offboarding.actor == OPERATOR.subject
    assert offboarding.after["principal_subject"] == LEAVER.subject
    # Nothing was left to revoke: the sign-out above already gave the session
    # up, and offboarding reports what it actually took, not what it would have.
    assert offboarding.after["sessions_revoked"] == 0
    assert offboarding.after["identity_binding_retained"] is True


def test_the_export_resumes_exactly_where_the_previous_one_ended(session):
    """Consecutive exports abut: the id watermark, never a clock reading."""

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    first = identity_audit.identity_events(session, after_id=watermark)
    assert len(first) == 1

    access.deprovision_principal(session, principal=LEAVER, operator=OPERATOR)
    resumed = identity_audit.identity_events(session, after_id=first[-1].id)

    assert [event.action for event in resumed] == [
        audit.DEPROVISION_PROJECT_MEMBER,
        audit.DEPROVISION_PRINCIPAL,
    ]


def test_the_export_leaves_the_coordination_history_where_it_is(session):
    """An access review is not a reason to hand over the whole Ledger."""

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    audit.record(
        session,
        principal=LEAVER,
        action=audit.SET_NEXT_ACTION,
        entity_type=audit.PROJECT,
        entity_id=project.id,
        after={"next_action": "call the party"},
    )

    events = identity_audit.identity_events(session, after_id=watermark)

    assert [event.action for event in events] == [audit.ENROLL_PROJECT_MEMBER]


def test_both_export_formats_carry_the_same_acts(session):
    """CSV keeps the flat columns usable and the detail lossless, as JSON text."""

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])
    events = identity_audit.identity_events(session, after_id=watermark)

    as_json = identity_audit.export_json(events)
    as_csv = identity_audit.export_csv(events)

    assert LEAVER.subject in as_json
    header, row, *rest = as_csv.strip().splitlines()
    assert header.split(",")[:5] == [
        "id",
        "action",
        "actor",
        "human_principal",
        "entity_type",
    ]
    assert rest == []
    assert audit.ENROLL_PROJECT_MEMBER in row
    assert "documentation_review" not in row


def test_an_unknown_export_format_is_refused(session):
    with pytest.raises(ValueError):
        identity_audit.export(session, fmt="xlsx")


def test_the_export_adapter_prints_what_the_reader_returns(session, capsys):
    """The export is a file someone can actually produce, not only a function."""

    from corridor import identity_audit_cli

    watermark = _watermark(session)
    project = _project(session)
    _enroll(session, project, LEAVER, "leaver@example.com", [access.COORDINATION])

    identity_audit_cli.main(
        ["--format=csv", f"--after-id={watermark}"],
        session_factory=lambda: session,
    )

    printed = capsys.readouterr().out
    assert printed.startswith("id,action,actor,")
    assert audit.ENROLL_PROJECT_MEMBER in printed
