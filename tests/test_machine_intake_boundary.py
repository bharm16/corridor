"""Machine intake is served through its own authentication class (#847).

The inbound-mail receipt is the documented primary intake. It authenticates a
transport secret and binds the project on the envelope recipient, and it runs
on the operations capability with no person and no membership. Before this it
was in no route view at all, so the router-level refusal answered 404 on an
enforcing deployment *before the handler's own authentication ran* -- the
substance of #847.

These are the decisive proofs. Every one runs under the enforcing boundary the
harness applies (``live_pilot_web_boundary`` declared), because the whole point
is what happens when the boundary is on: a correctly authenticated delivery is
accepted and reaches the same source ledger the human Sources screen reads,
while an unauthenticated one is refused at the handler's own 401 rather than the
router's 404; and the machine authentication cannot be borrowed from a browser
session, a caller-supplied project id, or the message's own To/Cc.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from access_support import seed_membership
from corridor import access, push_intake, source_register
from corridor.config import settings
from corridor.models import Project
from corridor.principals import HumanPrincipal
from corridor.web.app import (
    app,
    get_human_principal,
    get_machine_session,
    get_review_clock,
    get_session,
)

SECRET = "server-secret"
COORDINATOR = HumanPrincipal("local:coordinator")


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Content staging writes under tmp_path, never the developer's corpus."""
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "store"


@pytest.fixture
def enforcing_client(session, monkeypatch):
    """A client for a deployment that enforces the live-pilot web boundary.

    Both sessions resolve to the one rollback-scoped ``session``: the human
    boundary dependency reads the web login off it, and the machine handler
    would open the operations capability off it. The transport secret is
    configured; the frozen global-address door is left off so only the bound
    alias can name a project.
    """
    monkeypatch.setattr(settings, "live_pilot_web_boundary", True)
    monkeypatch.setattr(settings, "inbound_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "inbound_service_address", "")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_machine_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (
        lambda: datetime.now(timezone.utc)
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def make_project(session, name="Alpha"):
    project = Project(slug=f"intake-{uuid4().hex[:12]}", name=name, is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def bind_alias(session, *, project, alias, customer="acme-utilities"):
    return push_intake.register_push_credential(
        session, customer=customer, project=project,
        channel="project_alias", material=alias,
    )


def alias_for(project):
    return f"intake+{project.slug}@corridor.test"


def raw(*, message_id, body="A utility filed a spreadsheet.", to="ops@example.test",
        cc="", sender="utility@example.test", subject="Monthly UCM", attach=None):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message.set_content(body)
    if attach is not None:
        filename, content = attach
        message.add_attachment(
            content.encode("utf-8"), maintype="application",
            subtype="octet-stream", filename=filename,
        )
    return message.as_bytes()


def deliver(client, *, alias, message_id="<m@example.test>", token=SECRET, **raw_kwargs):
    headers = {"X-Corridor-Delivered-To": alias, "X-Corridor-Delivery-Id": uuid4().hex}
    if token is not None:
        headers["X-Corridor-Inbound-Token"] = token
    return client.post(
        "/intake/inbound", content=raw(message_id=message_id, **raw_kwargs),
        headers=headers,
    )


# --- Criterion 1: authenticate rather than 404 before authentication ---------


def test_the_enforcing_boundary_serves_an_authenticated_machine_delivery(
    enforcing_client, session
):
    """A correctly authenticated delivery is accepted, not 404'd before auth."""
    project = make_project(session)
    bind_alias(session, project=project, alias=alias_for(project))

    accepted = deliver(
        enforcing_client, alias=alias_for(project), message_id="<bound@example.test>",
        attach=("ucm.csv", "row,value\n1,2\n"),
    )

    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["project_id"] == project.id
    assert body["route_status"] == "routed"


def test_the_enforcing_boundary_authenticates_rather_than_404s_an_unauthenticated_one(
    enforcing_client, session
):
    """The decisive #847 proof: no transport secret is a 401, never a 404.

    A 404 here would mean the router refused the route before its own
    authentication ran -- exactly the enforcing-boundary bug this closes.
    """
    project = make_project(session)
    bind_alias(session, project=project, alias=alias_for(project))

    missing = deliver(enforcing_client, alias=alias_for(project), token=None)
    wrong = deliver(enforcing_client, alias=alias_for(project), token="not-the-secret")

    for response in (missing, wrong):
        assert response.status_code == 401, response.text
        assert response.json()["detail"] != "not found"


def test_the_boundary_is_genuinely_enforced_for_this_client(enforcing_client):
    """The 401 above is meaningful only if the boundary is really refusing.

    A human route the pilot does not enable answers 404 under the same client,
    which is the router-level refusal every route sees -- and the one the
    machine route is now exempt from because it is not human traffic.
    """
    refused = enforcing_client.get("/projects/whatever/inbound")
    assert refused.status_code == 404
    assert refused.json() == {"detail": "not found"}


# --- Criterion 3: machine auth cannot be bypassed, three ways -----------------


def test_browser_credentials_do_not_authenticate_a_machine_delivery(
    enforcing_client, session
):
    """A signed-in person's session is not a transport credential.

    ``get_human_principal`` is overridden to a coordinator for this client, so
    the request carries a human identity. Without the transport secret the
    delivery is still refused: the machine route reads no human session.
    """
    project = make_project(session)
    bind_alias(session, project=project, alias=alias_for(project))

    refused = deliver(enforcing_client, alias=alias_for(project), token=None)

    assert refused.status_code == 401, refused.text


def test_a_caller_supplied_project_identifier_does_not_bind(enforcing_client, session):
    """The project comes from the authenticated alias, never from the client.

    Even authenticated, a caller-named project id (here a query parameter and a
    body header the endpoint declares no parameter for) changes nothing: the
    binding is the alias the transport delivered to.
    """
    bound = make_project(session, name="Bound")
    other = make_project(session, name="Other")
    bind_alias(session, project=bound, alias=alias_for(bound))
    bind_alias(session, project=other, alias=alias_for(other))

    headers = {
        "X-Corridor-Inbound-Token": SECRET,
        "X-Corridor-Delivered-To": alias_for(bound),
        "X-Corridor-Delivery-Id": uuid4().hex,
    }
    accepted = enforcing_client.post(
        f"/intake/inbound?project={other.slug}&project_id={other.id}",
        content=raw(message_id="<caller-id@example.test>"),
        headers=headers,
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["project_id"] == bound.id


def test_the_message_to_and_cc_do_not_bind_the_project(enforcing_client, session):
    """The envelope recipient binds; the message's own To/Cc are untrusted body.

    The message is addressed (To and Cc) to the *other* project's alias, but it
    was delivered to the bound project's alias, so it is filed under the bound
    project. The headers inside the body decide nothing (#511, ADR-0078).
    """
    bound = make_project(session, name="Bound")
    other = make_project(session, name="Other")
    bind_alias(session, project=bound, alias=alias_for(bound))
    bind_alias(session, project=other, alias=alias_for(other))

    accepted = deliver(
        enforcing_client, alias=alias_for(bound), message_id="<to-cc@example.test>",
        to=alias_for(other), cc=f"someone@example.test, {alias_for(other)}",
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["project_id"] == bound.id


# --- The machine delivery reaches the human Sources view ----------------------


def test_an_accepted_machine_delivery_appears_on_the_sources_screen(
    enforcing_client, session, monkeypatch
):
    """Not just the HTTP response: the delivery lands in the ledger the human
    Sources screen reads, and that screen is served to a coordinator under the
    same enforcing boundary.

    This is the connection the audit found missing -- machine intake and the
    human source register are the same delivery model, not two mechanisms.
    """
    project = make_project(session)
    seed_membership(session, project, COORDINATOR)
    bind_alias(session, project=project, alias=alias_for(project))

    before = source_register.read_source_register(session, project_id=project.id)
    accepted = deliver(
        enforcing_client, alias=alias_for(project), message_id="<surfaced@example.test>",
        attach=("ucm.csv", "row,value\n1,2\n"),
    )
    assert accepted.status_code == 200, accepted.text

    after = source_register.read_source_register(session, project_id=project.id)
    assert len(after.rows) > len(before.rows), (
        "an accepted mail delivery did not become a row in the source register"
    )
    assert any(row.channel == "project_alias" for row in after.rows), (
        "the register does not show the mail-alias delivery it just took"
    )

    # The human Sources screen itself is served under the enforcing boundary.
    screen = enforcing_client.get(f"/projects/{project.slug}/sources")
    assert screen.status_code == 200, screen.text
