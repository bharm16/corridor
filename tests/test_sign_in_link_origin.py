"""The emailed sign-in link must come from configuration, not from the caller.

A magic link carries a live one-time credential. Building it from
`request.base_url` means its host is whatever the caller put in `Host` -- so an
attacker submits a sign-in request for someone else's address with a forged
host, Corridor emails *the real user* a valid token pointing at the attacker's
server, and the token is disclosed the moment the user follows the link they
were expecting.

The user sees a link that looks plausible and a request they may well have
made. Nothing about the flow looks wrong from their side, which is why this has
to be closed in the code that builds the URL rather than by asking anyone to
notice.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from corridor import access
from corridor.models import Project
from corridor.principals import HumanPrincipal
from corridor.web import auth
from corridor.web.app import app, get_session
from corridor.web.auth import PublicOriginInvalid, build_public_origin

OPERATOR = HumanPrincipal("person:operator")
ENROLLED = "person@example.com"


class _Settings:
    def __init__(self, **kwargs):
        self.public_origin = kwargs.get("public_origin", "")
        self.environment = kwargs.get("environment", "development")


# --- the configured origin ---------------------------------------------
def test_a_deployed_environment_requires_a_configured_origin():
    with pytest.raises(PublicOriginInvalid, match="CORRIDOR_PUBLIC_ORIGIN is required"):
        build_public_origin(_Settings(environment="nonproduction"))


def test_a_local_clone_may_fall_back_to_the_request():
    """`make queue` serves on whatever port the developer chose, and there is
    no credential to protect from a local browser."""
    assert build_public_origin(_Settings(environment="development")) == ""


def test_a_deployed_origin_must_be_https():
    """A token may not travel over plaintext."""
    with pytest.raises(PublicOriginInvalid, match="must be https"):
        build_public_origin(
            _Settings(
                public_origin="http://pilot.example.com", environment="nonproduction"
            )
        )


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:secret@pilot.example.com",
        "https://pilot.example.com/some/path",
        "https://pilot.example.com/?next=x",
        "https://pilot.example.com/#fragment",
    ],
)
def test_an_origin_carrying_anything_but_a_host_is_refused(origin):
    with pytest.raises(PublicOriginInvalid):
        build_public_origin(
            _Settings(public_origin=origin, environment="nonproduction")
        )


def test_a_trailing_slash_is_normalized_away():
    resolved = build_public_origin(
        _Settings(
            public_origin="https://pilot.example.com/", environment="nonproduction"
        )
    )

    assert resolved == "https://pilot.example.com"


# --- what actually gets emailed ----------------------------------------
class _CapturingSender:
    def __init__(self):
        self.links: list[str] = []

    def send_sign_in_link(self, *, email: str, link: str) -> None:
        self.links.append(link)


@pytest.fixture
def enrolled(session):
    """An identity that really is enrolled, so a link is really issued.

    Without this the assertions below iterate an empty list and pass while
    proving nothing -- `request_sign_in` only mails a link for an enrolled
    address, and answers identically either way.
    """
    project = Project(slug=f"origin-{id(session)}", name="Origin", is_synthetic=True)
    session.add(project)
    session.flush()
    access.enroll_member(
        session,
        project_id=project.id,
        email=ENROLLED,
        principal=HumanPrincipal("person:enrolled"),
        display_name="Enrolled",
        designations=(),
        operator=OPERATOR,
    )
    session.flush()
    return ENROLLED


def _client(session, sender):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.example.net"},
        {"X-Forwarded-Host": "attacker.example.net"},
        {"Forwarded": "host=attacker.example.net"},
        {
            "Host": "attacker.example.net",
            "X-Forwarded-Host": "attacker.example.net",
            "Forwarded": "host=attacker.example.net;proto=https",
        },
    ],
)
def test_no_request_header_can_steer_the_emailed_link(
    session, enrolled, monkeypatch, headers
):
    """The whole point: none of the three host-bearing headers may appear in
    the credential URL, however they are combined."""
    monkeypatch.setattr(auth, "PUBLIC_ORIGIN", "https://pilot.example.com")
    sender = _CapturingSender()
    client = _client(session, sender)

    client.post(
        "/sign-in/request",
        data={"email": "person@example.com", "next": ""},
        headers=headers,
    )

    assert sender.links, "no link was issued; the assertions below would be vacuous"
    for link in sender.links:
        assert link.startswith("https://pilot.example.com/sign-in/consume?token="), link
        assert "attacker.example.net" not in link


def test_the_configured_origin_is_used_even_when_the_host_is_legitimate(
    session, enrolled, monkeypatch
):
    """Not merely a blocklist: the origin is always configuration, so a
    request arriving on any other valid name still produces Corridor's URL."""
    monkeypatch.setattr(auth, "PUBLIC_ORIGIN", "https://pilot.example.com")
    sender = _CapturingSender()
    client = _client(session, sender)

    client.post(
        "/sign-in/request",
        data={"email": "person@example.com", "next": ""},
        headers={"Host": "corridor-alb-123.us-east-2.elb.amazonaws.com"},
    )

    assert sender.links, "no link was issued; the assertions below would be vacuous"
    for link in sender.links:
        assert link.startswith("https://pilot.example.com/")
        assert "elb.amazonaws.com" not in link


# --- delivery must not distinguish an enrolled address ------------------
class _FailingSender:
    """A provider that is down, or rejecting the message."""

    def send_sign_in_link(self, *, email: str, link: str) -> None:
        from corridor.web.auth import EmailDeliveryUnavailable

        raise EmailDeliveryUnavailable(f"could not deliver to {email}")


class _SlowSender:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.sent = 0

    def send_sign_in_link(self, *, email: str, link: str) -> None:
        import time

        time.sleep(self.seconds)
        self.sent += 1


def test_a_delivery_failure_does_not_change_the_answer(session, enrolled):
    """The send happens only for an enrolled address, so a provider failure
    surfacing as a 500 would tell an unauthenticated caller which addresses are
    enrolled -- undoing the indistinguishability this endpoint is built around,
    through the side door."""
    client = _client(session, _FailingSender())

    enrolled_response = client.post(
        "/sign-in/request", data={"email": ENROLLED, "next": ""}
    )
    unknown_response = client.post(
        "/sign-in/request", data={"email": "nobody@example.com", "next": ""}
    )

    assert enrolled_response.status_code == unknown_response.status_code == 200
    assert enrolled_response.text == unknown_response.text


def test_the_link_is_still_delivered(session, enrolled):
    """Moving the send off the response path must not drop it. TestClient runs
    background tasks as part of the request, so this proves delivery happens;
    the *ordering* is asserted structurally below, because a timing assertion
    against TestClient could only measure itself."""
    slow = _SlowSender(seconds=0.0)
    client = _client(session, slow)

    response = client.post("/sign-in/request", data={"email": ENROLLED, "next": ""})

    assert response.status_code == 200
    assert slow.sent == 1


def test_delivery_is_scheduled_as_a_background_task(session, enrolled):
    """Structural, because a timing assertion is flaky under load: the route
    must hand the send to BackgroundTasks rather than awaiting it inline."""
    import inspect

    from corridor.web import app as app_module

    source = inspect.getsource(app_module.request_sign_in)

    assert "background.add_task(" in source
    assert "_deliver_sign_in_link" in source
    assert "sender.send_sign_in_link" not in source


# --- a failed delivery must not lock the user out -----------------------
@pytest.fixture
def lend_session_to_background(session, monkeypatch):
    """Point the background task's own factory at this test's transaction.

    `_deliver_sign_in_link` opens a fresh session on purpose -- the request's
    transaction has committed by the time it runs -- which means its work
    lands on a different connection and a rollback-scoped test cannot see it.
    """
    from contextlib import contextmanager

    from corridor.web import app as app_module

    @contextmanager
    def _lend():
        yield session

    monkeypatch.setattr(app_module, "WebSession", _lend)
    return session


def test_a_failed_delivery_retires_its_token_so_a_retry_works(
    session, enrolled, lend_session_to_background
):
    """`has_live_token` coalesces duplicate requests, which is right while a
    link is in someone's inbox and wrong when delivery failed: every retry
    inside the 15-minute window would find the dead token, issue nothing, and
    leave an enrolled person unable to sign in. The raw token exists nowhere
    else once the request is over, so it cannot be resent."""
    from corridor import access

    client = _client(session, _FailingSender())

    client.post("/sign-in/request", data={"email": ENROLLED, "next": ""})

    # The dead token must not block the next attempt.
    assert not access.has_live_token(session, ENROLLED), (
        "an undelivered token is still live; every retry would coalesce onto it"
    )

    working = _CapturingSender()
    client = _client(session, working)
    client.post("/sign-in/request", data={"email": ENROLLED, "next": ""})

    assert working.links, "the retry issued no link"


def test_a_successful_delivery_leaves_its_token_alone(session, enrolled):
    """Retiring on success would spend the link before its recipient sees it."""
    from corridor import access

    working = _CapturingSender()
    client = _client(session, working)

    client.post("/sign-in/request", data={"email": ENROLLED, "next": ""})

    assert working.links
    assert access.has_live_token(session, ENROLLED)


def test_retiring_is_idempotent_and_bounded(session, enrolled):
    """The same single-use guard as consumption: a second attempt changes
    nothing rather than resurrecting or double-spending a row."""
    from corridor import access

    issued = access.issue_sign_in_token(session, ENROLLED, redirect_path=None)
    session.flush()

    assert access.retire_undelivered_sign_in_token(session, issued.raw_token)
    assert not access.retire_undelivered_sign_in_token(session, issued.raw_token)
    assert not access.has_live_token(session, ENROLLED)


def test_an_unknown_token_retires_nothing(session):
    from corridor import access

    assert not access.retire_undelivered_sign_in_token(session, "not-a-real-token")


def test_a_retired_token_cannot_still_be_spent(session, enrolled):
    """If the link *was* delivered despite the failure report, it must not
    remain usable -- retiring is the fail-closed direction."""
    from corridor import access

    issued = access.issue_sign_in_token(session, ENROLLED, redirect_path=None)
    session.flush()
    access.retire_undelivered_sign_in_token(session, issued.raw_token)

    assert access.consume_sign_in_token(session, issued.raw_token) is None
