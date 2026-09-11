"""The onboarding surface, driven the way a browser drives it (#827).

Every request below goes through the real application, under a real signed-in
session, submitting exactly the fields the rendered page carried. That is the
only way this file can say anything: a hand-written payload proves a route
accepts something, and only the page's own form proves a coordinator could
have submitted it. Under a real session that includes the request-forgery
token, which is the field #821 found missing elsewhere.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import html
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor import access
from corridor.config import settings
from corridor.models import Project
from corridor.onboarding_authorization import (
    ADOPT_BASELINE,
    INSPECT_COMPATIBILITY,
    ONBOARDING_OPERATIONS,
    REACH_PROJECT,
    completed_act,
    onboarding_standing,
    record_onboarding_event,
    record_onboarding_grant,
)
from corridor.operating_mode import ADOPTED_BASELINE, project_operating_mode
from corridor.principals import HumanPrincipal
from corridor.source_intake import receive_upload
from corridor.web import auth
from corridor.web.app import app, get_review_clock, get_session

from access_support import seed_membership
from browser_session_support import form_fields, sign_in, submit_form
from test_onboarding_authorization import (
    AT,
    DEMO,
    OPERATIONS_ACTOR,
    workbook,
)


COORDINATOR = HumanPrincipal("local:web-onboarding-coordinator")
COORDINATOR_EMAIL = "coordinator@onboarding.test"
OPERATOR = HumanPrincipal("local:web-onboarding-operator")
OPERATOR_EMAIL = "operations@onboarding.test"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


@pytest.fixture
def provisioned(session, store):
    """A provisioned project with a coordinator and a recorded grant."""

    project = Project(
        slug=f"web-onboard-{uuid4().hex[:8]}", name="Web Onboarding", is_synthetic=True
    )
    session.add(project)
    session.flush()
    for principal, email, designations in (
        (COORDINATOR, COORDINATOR_EMAIL, [access.COORDINATION]),
        (OPERATOR, OPERATOR_EMAIL, [access.TECHNICAL_OPERATIONS]),
    ):
        access.enroll_member(
            session,
            project_id=project.id,
            email=email,
            principal=principal,
            display_name=email.split("@")[0],
            designations=designations,
            operator=HumanPrincipal("local:provisioner"),
        )
    grant_id = record_onboarding_grant(
        session,
        project_id=int(project.id),
        authorization_id="loa-web",
        grant_version=1,
        customer="lone-star-transit",
        environment="pilot-1",
        permitted_operations=ONBOARDING_OPERATIONS,
        source_scope="ucm workbook revisions",
        governing_authorization_identity="customer-authorization-7",
        governing_authorization_version="2026-04-01",
        evidence_identity="s3://authorizations/7.pdf",
        evidence_sha256="a" * 64,
        issued_at=AT - timedelta(minutes=1),
        expires_at=AT + timedelta(days=14),
        issued_by_actor=OPERATIONS_ACTOR,
        recorded_by_actor=OPERATIONS_ACTOR,
    )
    session.flush()
    return project, grant_id


@pytest.fixture
def browser(session, monkeypatch):
    """The real application on the enforced boundary, with two seams.

    Identity is never overridden: every request below is authorized by a
    cookie a consumed magic link established, and every unsafe request carries
    the token the page it came from rendered.
    """

    monkeypatch.setattr(settings, "live_pilot_web_boundary", True)
    sender = auth.RecordingEmailSender()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    app.dependency_overrides[get_review_clock] = lambda: (lambda: AT)
    with ExitStack() as open_browsers:

        def signed_in(email: str) -> TestClient:
            client = open_browsers.enter_context(
                TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
            )
            sign_in(client, sender, email)
            return client

        yield signed_in
    app.dependency_overrides.clear()


def prose(body: str) -> str:
    """The page as a reader receives it, with markup escaping undone."""

    return html.unescape(body)


def stage(session, project, tmp_path, name="ucm.xlsx"):
    """Hand the workbook over the way the product does, and keep the receipt.

    Staging the bytes alone is not supplying a workbook: the reading is an act
    on the delivery those bytes arrived on (#937), and a request that names
    only a digest cannot say which delivery it read. So these scenarios take
    delivery through the same call the upload route makes, and carry the
    delivery id the page's own control carries.
    """

    received = receive_upload(
        session,
        project=project,
        body=workbook(tmp_path, name=name),
        filename=name,
        principal=COORDINATOR,
        customer=settings.customer_id,
    )
    session.flush()
    return received.staged, received.delivery_id


def prepare(client, project, staged, delivery_id):
    """Ask for the bounded reading the way the page's own control asks."""

    return client.post(
        f"/projects/{project.slug}/baseline/prepare",
        data={
            auth.CSRF_FIELD: client.cookies.get(auth.CSRF_COOKIE, ""),
            "sha256": staged.sha256,
            "filename": staged.filename,
            "source_delivery_id": str(delivery_id),
            "source_identity": "UCM workbook revision C",
            "customer": "Lone Star Transit Authority",
        },
        follow_redirects=False,
    )


# --- the screen an unadopted project opens on -------------------------------


def test_an_unadopted_project_opens_on_its_onboarding_state(browser, provisioned):
    """#849's third step: what Corridor needs next, not "no project"."""

    project, _ = provisioned
    client = browser(COORDINATOR_EMAIL)

    page = client.get(f"/work/{project.slug}", follow_redirects=False)

    assert page.status_code == 200, page.text
    assert "baseline" in page.text.lower()
    assert "Supply the customer's UCM workbook" in prose(page.text)


def test_a_project_with_no_authorization_refuses_with_a_bounded_reason(
    session, browser, store
):
    """Without one the page still opens, and says exactly why nothing follows.

    Fail closed is not the same as fail silent: the coordinator is told that
    Corridor operations has not recorded the permission, which is a fact about
    their project and names nothing about any other customer.
    """

    project = Project(
        slug=f"web-onboard-{uuid4().hex[:8]}", name="Unpermitted", is_synthetic=True
    )
    session.add(project)
    session.flush()
    access.enroll_member(
        session,
        project_id=project.id,
        email=COORDINATOR_EMAIL,
        principal=COORDINATOR,
        display_name="coordinator",
        designations=[access.COORDINATION],
        operator=HumanPrincipal("local:provisioner"),
    )
    session.flush()
    client = browser(COORDINATOR_EMAIL)

    page = client.get(f"/work/{project.slug}", follow_redirects=False)

    assert page.status_code == 200, page.text
    assert "has not recorded an onboarding authorization" in prose(page.text)
    assert "no_onboarding_authorization" in prose(page.text)


def test_the_page_offers_no_adoption_control_before_a_reading_is_prepared(
    browser, provisioned
):
    project, _ = provisioned
    client = browser(COORDINATOR_EMAIL)

    page = client.get(f"/work/{project.slug}", follow_redirects=False)

    assert form_fields(page.text, "/baseline/adopt") is None


def test_a_supplied_workbook_is_offered_the_control_that_reads_it(
    browser, provisioned, tmp_path
):
    """#934: supplying the workbook and reading it are two acts, and both are offered.

    Before this the page linked to the upload and, once a reading existed,
    rendered the adoption form -- and offered nothing at all in between, so a
    coordinator who followed the one control the page carried landed back on a
    page still asking them to supply the workbook.
    """

    project, _ = provisioned
    client = browser(COORDINATOR_EMAIL)
    name = "supplied-ucm.xlsx"
    body = workbook(tmp_path, name=name)
    upload = client.get(
        f"/projects/{project.slug}/sources/upload", follow_redirects=False
    )
    client.post(
        f"/projects/{project.slug}/sources/upload",
        data={**(form_fields(upload.text, "/sources/upload") or {}), "doc_type": "matrix"},
        files={"upload": (name, body)},
        follow_redirects=False,
    )

    page = client.get(f"/work/{project.slug}", follow_redirects=False)

    assert page.status_code == 200, page.text
    readable = prose(page.text)
    assert name in readable, (
        "the page does not say the workbook was supplied at all"
    )
    assert "Prepare a preview of the values this workbook would establish" in readable, (
        "the page still asks for a workbook it has already been given: "
        + readable[:400]
    )
    assert "Workbook received" in readable, (
        "the page does not say the workbook arrived: " + readable[:400]
    )
    offered = form_fields(page.text, "/baseline/prepare")
    assert offered is not None, "no control on the page reaches the reading"
    assert auth.CSRF_FIELD in offered, (
        "the rendered form carries no request-forgery token, so a real "
        "browser submission is refused"
    )
    # The control names the delivery it acts on, not only the bytes (#937):
    # two deliveries of one workbook share a digest, so a form that carried
    # only the digest left the route unable to say which one it read.
    assert offered["source_delivery_id"], (
        "the control offers the reading without naming the delivery it reads"
    )

    prepared = submit_form(
        client, f"/projects/{project.slug}/baseline/prepare", offered
    )

    assert prepared.status_code == 201, prepared.text
    assert "What adopting this would accept" in prose(prepared.text)


def test_a_member_who_may_not_prepare_is_not_offered_the_control(
    session, browser, provisioned, tmp_path
):
    """Rendered from ``may_prepare``, so the page and the rule cannot disagree."""

    project, _ = provisioned
    bystander = HumanPrincipal("local:web-onboarding-bystander")
    access.enroll_member(
        session,
        project_id=project.id,
        email="bystander@onboarding.test",
        principal=bystander,
        display_name="bystander",
        designations=[],
        operator=HumanPrincipal("local:provisioner"),
    )
    # Delivered before this person opens the page rather than through a second
    # browser: one rollback-scoped transaction may declare one person's
    # partition, and two people signing in against it is the scope conflict
    # #662 refuses, not anything this test is about.
    name = "supplied-ucm.xlsx"
    receive_upload(
        session,
        project=project,
        body=workbook(tmp_path, name=name),
        filename=name,
        principal=COORDINATOR,
        customer=settings.customer_id,
    )
    session.flush()

    page = browser("bystander@onboarding.test").get(
        f"/work/{project.slug}", follow_redirects=False
    )

    assert page.status_code == 200, page.text
    assert name in prose(page.text), (
        "the state of the project is readable by every member; only the act "
        "is designated"
    )
    assert form_fields(page.text, "/baseline/prepare") is None, (
        "someone who cannot prepare the reading was offered the control for it"
    )


def test_a_withdrawn_permission_pauses_the_page_rather_than_offering_the_control(
    session, browser, provisioned, tmp_path
):
    """#937: the displayed capability is the permission too, not the designation alone.

    The compatibility permission ADR-0099 grants is per-project, versioned and
    withdrawable, and `prepare_baseline_reading` proves it in the database
    before it opens anything. A page that read only the designation printed the
    paused notice and the Prepare button side by side -- so Technical
    Operations was invited into an act the very same page had just said was
    paused, and met a refusal it already had the words for.
    """

    project, grant_id = provisioned
    name = "supplied-ucm.xlsx"
    receive_upload(
        session,
        project=project,
        body=workbook(tmp_path, name=name),
        filename=name,
        principal=COORDINATOR,
        customer=settings.customer_id,
    )
    record_onboarding_event(
        session,
        project_id=int(project.id),
        grant_id=grant_id,
        kind="withdrawal_requested",
        requested_by="Dana Reyes, records custodian",
        requested_at=AT,
        executed_by_actor="security:duty-officer",
        executed_at=AT,
        reason="counsel review",
    )
    session.flush()

    page = browser(OPERATOR_EMAIL).get(
        f"/work/{project.slug}", follow_redirects=False
    )

    assert page.status_code == 200, page.text
    readable = prose(page.text)
    assert "Onboarding is paused" in readable, readable[:400]
    assert name in readable, (
        "the state of the project is readable by every member; only the act "
        "is designated"
    )
    assert form_fields(page.text, "/baseline/prepare") is None, (
        "Technical Operations was offered a control beside the notice saying "
        "this project's onboarding is paused: " + readable[:600]
    )
    assert "Prepare a preview of the values this workbook would establish" not in (
        readable
    ), (
        "the page invites an act Corridor may not perform on this project: "
        + readable[:400]
    )


def test_the_page_reads_the_reading_permission_not_the_adoption_one(
    session, browser, provisioned, tmp_path
):
    """A grant is selected per operation, so the two permissions really diverge.

    Operations withdrew the version that carried the compatibility reading and
    issued a narrower one that carries only the adoption. Nothing about this
    project is paused -- the grant in force is perfectly good -- and the act
    this page would otherwise offer is one Corridor may not perform. The page
    has the answer already; before #937 it printed the invitation anyway.
    """

    project, grant_id = provisioned
    name = "supplied-ucm.xlsx"
    receive_upload(
        session,
        project=project,
        body=workbook(tmp_path, name=name),
        filename=name,
        principal=COORDINATOR,
        customer=settings.customer_id,
    )
    record_onboarding_grant(
        session,
        project_id=int(project.id),
        authorization_id="loa-web",
        grant_version=2,
        customer="lone-star-transit",
        environment="pilot-1",
        permitted_operations=(REACH_PROJECT, ADOPT_BASELINE),
        source_scope="ucm workbook revisions",
        governing_authorization_identity="customer-authorization-7",
        governing_authorization_version="2026-04-01",
        evidence_identity="s3://authorizations/7.pdf",
        evidence_sha256="a" * 64,
        issued_at=AT - timedelta(minutes=1),
        expires_at=AT + timedelta(days=14),
        issued_by_actor=OPERATIONS_ACTOR,
        recorded_by_actor=OPERATIONS_ACTOR,
    )
    record_onboarding_event(
        session,
        project_id=int(project.id),
        grant_id=grant_id,
        kind="withdrawal_requested",
        requested_by="Dana Reyes, records custodian",
        requested_at=AT,
        executed_by_actor="security:duty-officer",
        executed_at=AT,
        reason="counsel review",
    )
    session.flush()
    assert onboarding_standing(
        session, project_id=int(project.id), operation=ADOPT_BASELINE, at=AT
    ).permitted, "the adoption permission has to stand, or this proves nothing"
    assert not onboarding_standing(
        session, project_id=int(project.id), operation=INSPECT_COMPATIBILITY, at=AT
    ).permitted

    page = browser(OPERATOR_EMAIL).get(
        f"/work/{project.slug}", follow_redirects=False
    )

    assert page.status_code == 200, page.text
    readable = prose(page.text)
    assert "Onboarding is paused" not in readable, (
        "nothing about the grant in force is withdrawn, so the page must not "
        "say the whole of onboarding is paused: " + readable[:400]
    )
    assert form_fields(page.text, "/baseline/prepare") is None, (
        "the page offered the reading under a permission it can see is "
        "withdrawn: " + readable[:600]
    )
    assert "onboarding_authorization_withdrawn" in readable, (
        "the page hides the control and says nothing about why: "
        + readable[:400]
    )


# --- the two acts -----------------------------------------------------------


def test_preparing_a_reading_shows_what_adopting_it_would_accept(
    session, browser, provisioned, tmp_path
):
    project, _ = provisioned
    client = browser(COORDINATOR_EMAIL)
    staged, delivery_id = stage(session, project, tmp_path)

    prepared = prepare(client, project, staged, delivery_id)

    assert prepared.status_code == 201, prepared.text
    assert "What adopting this would accept" in prose(prepared.text)
    assert "rows become the accepted record" in prose(prepared.text)
    assert "Questions only you can answer" in prose(prepared.text)


def test_the_adoption_form_the_page_renders_carries_the_forgery_token(
    session, browser, provisioned, tmp_path
):
    project, _ = provisioned
    client = browser(COORDINATOR_EMAIL)
    prepare(client, project, *stage(session, project, tmp_path))

    page = client.get(f"/work/{project.slug}", follow_redirects=False)
    fields = form_fields(page.text, "/baseline/adopt")

    assert fields is not None, "the page rendered no adoption form"
    assert auth.CSRF_FIELD in fields
    assert fields["binding_fingerprint"]
    assert fields["request_key"]


def test_the_coordinator_adopts_from_the_page_they_were_already_on(
    session, browser, provisioned, tmp_path
):
    """#849's sixth step, end to end and through the granted command."""

    project, _ = provisioned
    client = browser(COORDINATOR_EMAIL)
    prepare(client, project, *stage(session, project, tmp_path))
    page = client.get(f"/work/{project.slug}", follow_redirects=False)
    fields = form_fields(page.text, "/baseline/adopt")

    adopted = submit_form(
        client,
        f"/projects/{project.slug}/baseline/adopt",
        {**fields, **_checked_answers(page.text)},
    )

    assert adopted.status_code == 201, adopted.text
    assert project_operating_mode(session, int(project.id)) == ADOPTED_BASELINE
    assert "The baseline this project accepted" in prose(adopted.text)
    assert "Review and approve what this project issues" in prose(adopted.text)
    assert completed_act(
        session, project_id=int(project.id), operation="adopt_baseline"
    )


def test_adopting_without_answering_a_blocking_question_is_refused(
    session, browser, provisioned, tmp_path
):
    project, _ = provisioned
    client = browser(COORDINATOR_EMAIL)
    prepare(client, project, *stage(session, project, tmp_path))
    page = client.get(f"/work/{project.slug}", follow_redirects=False)
    fields = form_fields(page.text, "/baseline/adopt")

    refused = submit_form(
        client, f"/projects/{project.slug}/baseline/adopt", fields
    )

    assert refused.status_code == 409, refused.text
    assert project_operating_mode(session, int(project.id)) != ADOPTED_BASELINE


def test_a_form_with_no_forgery_token_is_refused_before_anything_is_read(
    session, browser, provisioned, tmp_path
):
    project, _ = provisioned
    client = browser(COORDINATOR_EMAIL)
    prepare(client, project, *stage(session, project, tmp_path))
    page = client.get(f"/work/{project.slug}", follow_redirects=False)
    fields = form_fields(page.text, "/baseline/adopt")
    fields.pop(auth.CSRF_FIELD)

    refused = submit_form(client, f"/projects/{project.slug}/baseline/adopt", fields)

    assert refused.status_code == 403
    assert project_operating_mode(session, int(project.id)) != ADOPTED_BASELINE


def test_a_member_with_no_coordination_designation_is_refused_by_the_command(
    session, browser, provisioned, tmp_path
):
    """The refusal comes from the authority that records the act.

    Operations may prepare the reading -- that is the compatibility work
    ADR-0099 permits them -- and may not make the adoption decision. The
    adapter gates neither; `enforce_coordination_designation` refuses the
    write, which is where #533 and #839 put the same rule.
    """

    project, _ = provisioned
    operator = browser(OPERATOR_EMAIL)
    prepared = prepare(operator, project, *stage(session, project, tmp_path))
    assert prepared.status_code == 201, prepared.text

    page = operator.get(f"/work/{project.slug}", follow_redirects=False)
    fields = form_fields(page.text, "/baseline/adopt")
    refused = submit_form(
        operator,
        f"/projects/{project.slug}/baseline/adopt",
        {**fields, **_checked_answers(page.text)},
    )

    assert refused.status_code >= 400
    assert project_operating_mode(session, int(project.id)) != ADOPTED_BASELINE


# --- what the coordinator sees when onboarding is paused --------------------


def test_a_paused_onboarding_names_this_project_and_nothing_wider(
    session, browser, provisioned
):
    project, grant_id = provisioned
    record_onboarding_event(
        session,
        project_id=int(project.id),
        grant_id=grant_id,
        kind="withdrawal_requested",
        requested_by="Dana Reyes, records custodian",
        requested_at=AT,
        executed_by_actor="security:duty-officer",
        executed_at=AT,
        reason="counsel review",
    )
    session.flush()
    client = browser(COORDINATOR_EMAIL)

    page = client.get(f"/work/{project.slug}", follow_redirects=False)

    assert page.status_code == 200, page.text
    assert "Onboarding is paused" in prose(page.text)
    assert "Confirmation that processing has stopped is pending." in prose(page.text)
    assert "Dana Reyes" not in prose(page.text)
    assert "customer-authorization-7" not in prose(page.text)
    assert "a" * 64 not in prose(page.text)
    assert "lone-star-transit" not in prose(page.text)


def test_an_enforced_withdrawal_says_writes_are_disabled_and_when(
    session, browser, provisioned
):
    project, grant_id = provisioned
    for kind, extra in (
        (
            "withdrawal_requested",
            {"requested_by": "Dana Reyes", "requested_at": AT, "reason": "review"},
        ),
        ("withdrawal_enforced", {}),
    ):
        record_onboarding_event(
            session,
            project_id=int(project.id),
            grant_id=grant_id,
            kind=kind,
            executed_by_actor="security:duty-officer",
            executed_at=AT + timedelta(minutes=3),
            **extra,
        )
    session.flush()
    client = browser(COORDINATOR_EMAIL)

    page = client.get(f"/work/{project.slug}", follow_redirects=False)

    assert "Onboarding writes are disabled for this project as of" in prose(page.text)


def _checked_answers(body: str) -> dict[str, str]:
    """The radio controls the page rendered as already chosen.

    Read out of the markup rather than composed, for the same reason the hidden
    fields are: a payload this test invents proves the route accepts something,
    and only the page's own controls prove a person could have sent it.
    """

    answers = {}
    for match in re.finditer(
        r'<input type="radio"\s+name="([^"]+)"\s+value="([^"]+)"\s+checked>', body
    ):
        answers[match.group(1)] = match.group(2)
    return answers
