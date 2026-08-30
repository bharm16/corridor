"""End-to-end sign-in and project-scoped access (#331).

These tests drive the real authentication path over ordinary HTTP: nothing
overrides identity, so a request is authorized only by a session cookie that a
consumed magic link established. The email seam is a non-sending recorder, so a
link is captured, never mailed. The session fixture is rollback-scoped like the
rest of the suite; the one test that needs truly concurrent committed
transactions uses the harness-owned ``runtime_database``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor import access, audit
from corridor.db import Session, engine
from corridor.models import (
    AuditLog,
    Dependency,
    DocumentationFieldConfirmation,
    DocPage,
    Document,
    EvidenceLink,
    PersonIdentity,
    Project,
    SignInToken,
    WebSession,
)
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import app, get_session

OPERATOR = HumanPrincipal("local:operations")


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def sender():
    return auth.RecordingEmailSender()


@pytest.fixture
def client(session, sender):
    # Override only the plumbing (database, mail). Identity is left to the real
    # session-cookie path. https base_url so Secure cookies round-trip.
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    app.dependency_overrides.clear()


# --- helpers --------------------------------------------------------------

def _sha(raw: str) -> str:
    return sha256(raw.encode()).hexdigest()


def make_project(session, slug="alpha", name="Alpha") -> Project:
    project = Project(slug=slug, name=name, is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def enroll(session, project, subject, email, designations):
    return access.enroll_member(
        session,
        project_id=project.id,
        email=email,
        principal=HumanPrincipal(subject),
        display_name=subject.split(":", 1)[1],
        designations=designations,
        operator=OPERATOR,
    )


def request_link(client, email, next_path=""):
    data = {"email": email}
    if next_path:
        data["next"] = next_path
    return client.post("/sign-in/request", data=data, follow_redirects=False)


def link_token(sender) -> str:
    _email, link = sender.sent[-1]
    return parse_qs(urlsplit(link).query)["token"][0]


def sign_in(client, sender, email, *, next_path=""):
    """Request and consume a link; leaves the session cookie on the client."""
    before = len(sender.sent)
    response = request_link(client, email, next_path)
    assert response.status_code == 200
    assert len(sender.sent) == before + 1, "a member must receive exactly one link"
    consume = client.get(
        "/sign-in/consume",
        params={"token": link_token(sender)},
        follow_redirects=False,
    )
    assert consume.status_code == 303
    return consume


def csrf_headers(client) -> dict[str, str]:
    token = client.cookies.get(auth.CSRF_COOKIE)
    return {auth.CSRF_HEADER: token} if token else {}


def authed_post(client, url, data=None):
    return client.post(
        url,
        data=data or {},
        headers=csrf_headers(client),
        follow_redirects=False,
    )


def a_dependency(session, project, ref="DEP-1") -> Dependency:
    dependency = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type="utility_relocation",
        title="Telecom relocation",
    )
    session.add(dependency)
    session.flush()
    return dependency


# --- successful sign-in reaches the permitted project ---------------------

def test_member_signs_in_and_reaches_their_project(client, session, sender):
    project = make_project(session)
    enroll(session, project, "local:alice", "Alice@Example.test", [access.COORDINATION])

    # Case-folded: the address the person types need not match the stored case.
    sign_in(client, sender, "alice@example.test")

    page = client.get(f"/work/{project.slug}")
    assert page.status_code == 200


def test_link_is_hashed_at_rest_and_session_secrets_are_never_plaintext(
    client, session, sender
):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])

    request_link(client, "alice@example.test")
    raw_token = link_token(sender)
    stored = session.scalars(select(SignInToken)).one()
    assert stored.token_sha256 == _sha(raw_token)
    assert raw_token != stored.token_sha256

    client.get("/sign-in/consume", params={"token": raw_token}, follow_redirects=False)
    web_session = session.scalars(select(WebSession)).one()
    raw_session = client.cookies.get(auth.SESSION_COOKIE)
    raw_csrf = client.cookies.get(auth.CSRF_COOKIE)
    assert web_session.session_sha256 == _sha(raw_session)
    assert web_session.csrf_sha256 == _sha(raw_csrf)
    # The stored rows carry no reusable plaintext.
    assert raw_session != web_session.session_sha256
    assert raw_csrf != web_session.csrf_sha256


def test_session_cookie_is_httponly_secure_and_samesite_strict(client, session, sender):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    request_link(client, "alice@example.test")
    consume = client.get(
        "/sign-in/consume",
        params={"token": link_token(sender)},
        follow_redirects=False,
    )
    cookie_header = "; ".join(consume.headers.get_list("set-cookie"))
    assert "corridor_session=" in cookie_header
    lowered = cookie_header.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=strict" in lowered


# --- link consumption is atomic and single-use ---------------------------

def test_a_link_cannot_be_consumed_twice(client, session, sender):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    request_link(client, "alice@example.test")
    token = link_token(sender)

    first = client.get("/sign-in/consume", params={"token": token}, follow_redirects=False)
    assert first.status_code == 303
    # A brand-new client cannot replay the same link.
    with TestClient(app, base_url="https://testserver") as replay:
        app.dependency_overrides[get_session] = lambda: session
        second = replay.get(
            "/sign-in/consume", params={"token": token}, follow_redirects=False
        )
    assert second.status_code == 400
    assert session.scalar(select(func.count()).select_from(WebSession)) == 1


def test_expired_link_establishes_nothing(session):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    issued = access.issue_sign_in_token(
        session, "alice@example.test", redirect_path=None
    )
    # Consume from a clock past the link's lifetime: the atomic guard finds no
    # unexpired row, so nothing is established.
    future = datetime.now(timezone.utc) + access.SIGN_IN_TOKEN_TTL + timedelta(minutes=1)
    assert access.consume_sign_in_token(session, issued.raw_token, now=future) is None
    assert session.scalar(select(func.count()).select_from(WebSession)) == 0


@pytest.mark.parametrize("token", ["", "not-a-real-token", "a" * 64])
def test_a_tampered_or_unknown_token_is_refused(client, session, sender, token):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    request_link(client, "alice@example.test")  # a real live token also exists

    response = client.get(
        "/sign-in/consume", params={"token": token}, follow_redirects=False
    )
    assert response.status_code == 400
    assert session.scalar(select(func.count()).select_from(WebSession)) == 0


def test_concurrent_consumers_cannot_both_win(runtime_database):
    factory = runtime_database.session_factory
    with factory() as setup:
        project = Project(slug=f"c-{uuid4().hex}", name="Concurrent", is_synthetic=True)
        setup.add(project)
        setup.flush()
        access.enroll_member(
            setup,
            project_id=project.id,
            email="race@example.test",
            principal=HumanPrincipal("local:race"),
            display_name="Race",
            designations=[access.COORDINATION],
            operator=OPERATOR,
        )
        issued = access.issue_sign_in_token(
            setup, "race@example.test", redirect_path=None
        )
        setup.commit()
        raw_token = issued.raw_token

    ready = Barrier(2)

    def consume(_):
        ready.wait(timeout=2)
        with factory() as s:
            result = access.consume_sign_in_token(s, raw_token)
            s.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    with factory() as check:
        assert check.scalar(select(func.count()).select_from(WebSession)) == 0
        consumed = check.scalars(select(SignInToken)).one()
        assert consumed.consumed_at is not None


# --- no account / membership enumeration; bounded issuance ----------------

def test_sign_in_request_is_indistinguishable_for_members_and_strangers(
    client, session, sender
):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])

    member = request_link(client, "alice@example.test")
    stranger = request_link(client, "nobody@example.test")

    assert member.status_code == stranger.status_code == 200
    assert member.text == stranger.text
    # Only the real member produced a link and a token row.
    assert [email for email, _ in sender.sent] == ["alice@example.test"]
    assert session.scalar(select(func.count()).select_from(SignInToken)) == 1


def test_duplicate_requests_coalesce_onto_one_live_link(client, session, sender):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])

    for _ in range(3):
        assert request_link(client, "alice@example.test").status_code == 200

    assert len(sender.sent) == 1
    assert session.scalar(select(func.count()).select_from(SignInToken)) == 1


def test_issuance_backoff_bounds_email_attempts(client, session, sender):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])

    # Every request counts toward the ceiling, even coalesced ones, so an
    # unauthenticated caller cannot generate unlimited token attempts.
    for _ in range(access.MAX_ISSUE_PER_EMAIL):
        assert request_link(client, "alice@example.test").status_code == 200
    assert request_link(client, "alice@example.test").status_code == 429
    # Coalescing meant only one link was ever actually sent.
    assert len(sender.sent) == 1


def test_consumption_backoff_bounds_attempts(client, session):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    for _ in range(access.MAX_CONSUME_PER_IP):
        assert (
            client.get(
                "/sign-in/consume", params={"token": "x"}, follow_redirects=False
            ).status_code
            == 400
        )
    assert (
        client.get(
            "/sign-in/consume", params={"token": "x"}, follow_redirects=False
        ).status_code
        == 429
    )


# --- sessions: expiry, revocation, logout ---------------------------------

def test_logout_revokes_the_session(client, session, sender):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    sign_in(client, sender, "alice@example.test")
    assert client.get(f"/work/{project.slug}").status_code == 200

    out = authed_post(client, "/sign-out")
    assert out.status_code == 303
    assert session.scalars(select(WebSession)).one().revoked_at is not None
    # The cookies are cleared, so the next request is unauthenticated.
    assert client.get(f"/work/{project.slug}").status_code == 401


def test_revoked_membership_takes_effect_on_the_next_request(client, session, sender):
    project = make_project(session)
    entry = enroll(
        session, project, "local:alice", "alice@example.test", [access.COORDINATION]
    )
    sign_in(client, sender, "alice@example.test")
    assert client.get(f"/work/{project.slug}").status_code == 200

    entry.active = False
    session.flush()

    # Same valid session cookie, but membership is gone: refused immediately.
    assert client.get(f"/work/{project.slug}").status_code == 404


# --- membership is enforced on every surface, including guessed ids --------

def test_unauthenticated_requests_are_refused_everywhere(client, session):
    project = make_project(session)
    document = _document(session, project)
    assert client.get(f"/work/{project.slug}").status_code == 401
    assert client.get(f"/queue/{project.slug}").status_code == 401
    assert client.get(f"/ledger/{project.slug}").status_code == 401
    assert client.get(f"/page-image/{document.id}/1").status_code == 401


def test_a_member_cannot_reach_another_project(client, session, sender):
    home = make_project(session, slug="home", name="Home")
    other = make_project(session, slug="other", name="Other")
    other_doc = _document(session, other)
    enroll(session, home, "local:alice", "alice@example.test", [access.COORDINATION])
    sign_in(client, sender, "alice@example.test")

    # A project the caller does not belong to is indistinguishable from missing.
    assert client.get(f"/work/{other.slug}").status_code == 404
    assert client.get(f"/queue/{other.slug}").status_code == 404
    assert client.get(f"/ledger/{other.slug}").status_code == 404
    # Guessed source-image id in another project leaks nothing.
    assert client.get(f"/page-image/{other_doc.id}/1").status_code == 404


def test_a_member_can_read_their_own_source_image(client, session, sender):
    project = make_project(session)
    document = _document(session, project)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    sign_in(client, sender, "alice@example.test")
    # The image file is absent in tests, so a member gets 404 for the file —
    # never the 401/404 the gate would raise for a non-member.
    assert client.get(f"/page-image/{document.id}/1").status_code == 404
    assert client.get(f"/page-image/{document.id}/99").status_code == 404


# --- the four designations stay distinct ----------------------------------

def test_coordination_member_can_coordinate_and_the_write_records_them(
    client, session, sender
):
    project = make_project(session)
    dependency = a_dependency(session, project)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    sign_in(client, sender, "alice@example.test")

    # A form field claiming another principal must not be honored.
    response = authed_post(
        client,
        f"/dependencies/{dependency.id}/owner",
        {"slug": project.slug, "owner": "Field Lead", "principal": "local:someone-else"},
    )
    assert response.status_code == 303
    recorded = session.scalars(
        select(AuditLog).where(AuditLog.action == audit.ASSIGN_INTERNAL_OWNER)
    ).all()
    assert recorded
    assert all(entry.human_principal == "local:alice" for entry in recorded)


def test_coordination_alone_cannot_review_or_release(client, session, sender):
    project = make_project(session)
    dependency = a_dependency(session, project)
    enroll(session, project, "local:carol", "carol@example.test", [access.COORDINATION])
    sign_in(client, sender, "carol@example.test")
    before = session.scalar(select(func.count()).select_from(AuditLog))

    review = authed_post(
        client,
        f"/dependencies/{dependency.id}/evidence/1/satisfies",
        {"slug": project.slug},
    )
    release = authed_post(
        client, f"/reports/{project.slug}/release", {"artifact_id": "1"}
    )

    assert review.status_code == 403
    assert release.status_code == 403
    # A refused request writes nothing.
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before


def test_documentation_review_alone_cannot_coordinate(client, session, sender):
    project = make_project(session)
    dependency = a_dependency(session, project)
    enroll(
        session,
        project,
        "local:dana",
        "dana@example.test",
        [access.DOCUMENTATION_REVIEW],
    )
    sign_in(client, sender, "dana@example.test")

    coordinate = authed_post(
        client,
        f"/dependencies/{dependency.id}/owner",
        {"slug": project.slug, "owner": "Field Lead"},
    )
    assert coordinate.status_code == 403
    # The review designation itself passes its own gate (not a 403).
    review = authed_post(
        client,
        f"/dependencies/{dependency.id}/evidence/999/satisfies",
        {"slug": project.slug},
    )
    assert review.status_code != 403


def test_only_documentation_reviewer_can_confirm_a_cited_approval(
    client, session, sender
):
    project = make_project(session, slug="document-review")
    dependency = Dependency(
        project_id=project.id,
        ref_code="DOC-ACCESS-1",
        dep_type="utility_relocation",
        title="Approval confirmation",
        resolution_strategy="relocate",
    )
    document = Document(
        project_id=project.id,
        sha256="document-review-approval",
        filename="approval.pdf",
        doc_type="agreement",
        parse_status="parsed",
    )
    session.add_all((dependency, document))
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text="The relocation is approved.",
        )
    )
    evidence = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="The relocation is approved.",
        verified=True,
    )
    session.add(evidence)
    session.flush()
    enroll(session, project, "local:coordinator", "coord@example.test", [access.COORDINATION])
    enroll(
        session,
        project,
        "local:dana",
        "reviewer@example.test",
        [access.DOCUMENTATION_REVIEW],
    )

    sign_in(client, sender, "coord@example.test")
    refused = authed_post(
        client,
        f"/dependencies/{dependency.id}/documentation/confirm-approval",
        {"slug": project.slug, "evidence_link_id": evidence.id},
    )
    assert refused.status_code == 403
    assert session.scalar(select(func.count()).select_from(DocumentationFieldConfirmation)) == 0

    sign_in(client, sender, "reviewer@example.test")
    confirmed = authed_post(
        client,
        f"/dependencies/{dependency.id}/documentation/confirm-approval",
        {"slug": project.slug, "evidence_link_id": evidence.id},
    )
    assert confirmed.status_code == 303
    assert session.scalar(select(func.count()).select_from(DocumentationFieldConfirmation)) == 1


def test_external_release_alone_cannot_coordinate(client, session, sender):
    project = make_project(session)
    dependency = a_dependency(session, project)
    enroll(
        session,
        project,
        "local:erin",
        "erin@example.test",
        [access.EXTERNAL_RELEASE],
    )
    sign_in(client, sender, "erin@example.test")

    coordinate = authed_post(
        client,
        f"/dependencies/{dependency.id}/owner",
        {"slug": project.slug, "owner": "Field Lead"},
    )
    assert coordinate.status_code == 403
    # The release designation passes its own gate (reaching the domain refusal).
    release = authed_post(
        client, f"/reports/{project.slug}/release", {"artifact_id": "999999"}
    )
    assert release.status_code != 403


# --- request forgery protection on authenticated writes -------------------

def test_authenticated_write_without_a_csrf_token_is_refused(client, session, sender):
    project = make_project(session)
    dependency = a_dependency(session, project)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    sign_in(client, sender, "alice@example.test")
    before = session.scalar(select(func.count()).select_from(AuditLog))

    # No X-CSRF-Token header and no csrf form field.
    response = client.post(
        f"/dependencies/{dependency.id}/owner",
        data={"slug": project.slug, "owner": "Field Lead"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert session.scalar(select(func.count()).select_from(AuditLog)) == before


def test_a_forged_csrf_token_is_refused(client, session, sender):
    project = make_project(session)
    dependency = a_dependency(session, project)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    sign_in(client, sender, "alice@example.test")

    response = client.post(
        f"/dependencies/{dependency.id}/owner",
        data={"slug": project.slug, "owner": "Field Lead"},
        headers={auth.CSRF_HEADER: "not-the-real-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403


# --- redirect targets are constrained to the application ------------------

def test_open_redirect_targets_are_rejected(client, session, sender):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])

    request_link(client, "alice@example.test", next_path="https://evil.example/steal")
    consume = client.get(
        "/sign-in/consume",
        params={"token": link_token(sender)},
        follow_redirects=False,
    )
    assert consume.status_code == 303
    assert consume.headers["location"] == "/"


def test_same_app_redirect_is_honored(client, session, sender):
    project = make_project(session)
    enroll(session, project, "local:alice", "alice@example.test", [access.COORDINATION])
    consume = sign_in(
        client, sender, "alice@example.test", next_path=f"/work/{project.slug}"
    )
    assert consume.headers["location"] == f"/work/{project.slug}"


# --- managed enrollment is attributable and project-scoped ----------------

def test_enrollment_is_attributable_and_scoped_to_one_project(session):
    alpha = make_project(session, slug="alpha", name="Alpha")
    beta = make_project(session, slug="beta", name="Beta")
    enroll(session, alpha, "local:frank", "frank@example.test", [access.COORDINATION])

    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == audit.ENROLL_PROJECT_MEMBER)
    ).one()
    assert entry.human_principal == OPERATOR.subject
    assert entry.entity_id == alpha.id
    assert entry.after_json["designations"] == [access.COORDINATION]

    # Enrolment in alpha grants nothing in beta.
    assert access.resolve_membership(session, "local:frank", beta.id) is None
    membership = access.resolve_membership(session, "local:frank", alpha.id)
    assert membership is not None and membership.has(access.COORDINATION)


def test_an_email_cannot_be_rebound_to_a_different_principal(session):
    alpha = make_project(session)
    enroll(session, alpha, "local:frank", "frank@example.test", [access.COORDINATION])
    with pytest.raises(access.AccessError):
        access.enroll_member(
            session,
            project_id=alpha.id,
            email="frank@example.test",
            principal=HumanPrincipal("local:imposter"),
            display_name="Imposter",
            designations=[access.COORDINATION],
            operator=OPERATOR,
        )
    assert session.scalar(select(func.count()).select_from(PersonIdentity)) == 1


# --- helpers that need models ---------------------------------------------

def _document(session, project) -> Document:
    document = Document(
        project_id=project.id,
        sha256=sha256(f"{project.id}:{uuid4().hex}".encode()).hexdigest(),
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text="FOC1-1 AT&T Texas (SWBT) 1149+00",
            image_path="/tmp/corridor-missing-page.png",
        )
    )
    session.flush()
    return document
