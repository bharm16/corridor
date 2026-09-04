"""Sign-in link delivery: the adapter, and the refusal to run without one.

`/sign-in/request` answers identically whether or not a link was delivered --
it must not disclose whether an address is enrolled -- so a missing adapter is
invisible from outside. Without the fail-closed check a deployed environment
reports success to every user while nobody can ever sign in.
"""

from __future__ import annotations

import pathlib

import pytest

from corridor.web.auth import (
    EmailDeliveryUnavailable,
    LoggingEmailSender,
    SesEmailSender,
    build_email_sender,
)


class _Settings:
    def __init__(self, **kwargs):
        self.email_backend = kwargs.get("email_backend", "logging")
        self.environment = kwargs.get("environment", "development")
        self.sign_in_sender_address = kwargs.get("sign_in_sender_address", "")
        self.storage_s3_region = kwargs.get("storage_s3_region", "us-east-2")


class _RecordingSes:
    def __init__(self, error: Exception | None = None):
        self.calls: list[dict] = []
        self._error = error

    def send_email(self, **kwargs):
        if self._error is not None:
            raise self._error
        self.calls.append(kwargs)
        return {"MessageId": "m-1"}


# --- fail closed --------------------------------------------------------
@pytest.mark.parametrize("environment", ["nonproduction", "production", "pilot"])
def test_a_deployed_environment_refuses_the_logging_sender(environment):
    with pytest.raises(EmailDeliveryUnavailable, match="delivers nothing"):
        build_email_sender(_Settings(environment=environment))


@pytest.mark.parametrize("environment", ["development", "test"])
def test_a_local_clone_still_boots_without_delivery(environment):
    sender = build_email_sender(_Settings(environment=environment))

    assert isinstance(sender, LoggingEmailSender)


def test_the_ses_backend_requires_a_verified_sender_identity():
    """SES refuses an unverified sender, so an empty one is a startup fault
    rather than a per-message failure discovered by a user."""
    with pytest.raises(EmailDeliveryUnavailable, match="CORRIDOR_SIGN_IN_SENDER"):
        build_email_sender(
            _Settings(email_backend="ses", environment="nonproduction")
        )


def test_an_unknown_backend_is_refused():
    with pytest.raises(EmailDeliveryUnavailable, match="unknown"):
        build_email_sender(
            _Settings(email_backend="smtp", environment="nonproduction")
        )


def test_the_ses_backend_is_selected_in_a_deployed_environment():
    sender = build_email_sender(
        _Settings(
            email_backend="ses",
            environment="nonproduction",
            sign_in_sender_address="no-reply@example.com",
        )
    )

    assert isinstance(sender, SesEmailSender)
    assert sender.sender == "no-reply@example.com"


# --- what actually gets sent -------------------------------------------
def test_the_message_carries_the_recipient_and_the_one_time_link():
    client = _RecordingSes()
    sender = SesEmailSender(
        sender="no-reply@example.com", region="us-east-2", client=client
    )

    sender.send_sign_in_link(
        email="person@example.com", link="https://pilot.example.com/sign-in/consume?t=xyz"
    )

    [call] = client.calls
    assert call["Source"] == "no-reply@example.com"
    assert call["Destination"]["ToAddresses"] == ["person@example.com"]
    body = call["Message"]["Body"]["Text"]["Data"]
    assert "https://pilot.example.com/sign-in/consume?t=xyz" in body
    assert "Corridor" in call["Message"]["Subject"]["Data"]


def test_a_delivery_failure_is_raised_without_the_link_in_it():
    """The link is a live credential. A failure has to be visible, but it
    carries the recipient and the provider's code -- never the secret."""
    error = Exception("boom")
    error.response = {"Error": {"Code": "MessageRejected"}}
    sender = SesEmailSender(
        sender="no-reply@example.com", region="us-east-2", client=_RecordingSes(error)
    )

    with pytest.raises(EmailDeliveryUnavailable) as raised:
        sender.send_sign_in_link(
            email="person@example.com", link="https://pilot.example.com/x?t=SECRET"
        )

    message = str(raised.value)
    assert "person@example.com" in message
    assert "MessageRejected" in message
    assert "SECRET" not in message
    assert "t=" not in message


def test_a_failure_without_a_provider_code_still_names_no_link():
    sender = SesEmailSender(
        sender="no-reply@example.com",
        region="us-east-2",
        client=_RecordingSes(RuntimeError("network down")),
    )

    with pytest.raises(EmailDeliveryUnavailable) as raised:
        sender.send_sign_in_link(email="a@b.com", link="https://x/?t=SECRET")

    assert "SECRET" not in str(raised.value)


def test_the_original_exception_is_not_chained_into_the_message():
    """`raise ... from None` on purpose: a chained SES exception can carry the
    request it failed on, and that request contains the link."""
    error = Exception("https://pilot.example.com/x?t=SECRET")
    sender = SesEmailSender(
        sender="no-reply@example.com", region="us-east-2", client=_RecordingSes(error)
    )

    with pytest.raises(EmailDeliveryUnavailable) as raised:
        sender.send_sign_in_link(email="a@b.com", link="https://x/?t=SECRET")

    assert raised.value.__cause__ is None
    assert "SECRET" not in str(raised.value)


def test_the_web_application_refuses_to_start_without_delivery():
    """"Refuses to start" has to mean startup. A lazy check fires on the first
    sign-in request, and that request answers with the same success page either
    way -- so a misconfigured deployment would look healthy while every user
    silently received nothing.

    Run in a clean interpreter rather than by reloading the module here: a
    half-initialised `corridor.web.auth` left in this process would cascade
    into every other test sharing the worker.
    """
    import os
    import subprocess
    import sys

    environment = dict(
        os.environ,
        CORRIDOR_EMAIL_BACKEND="logging",
        CORRIDOR_ENVIRONMENT="nonproduction",
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import corridor.web.auth"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(pathlib.Path(__file__).parents[1]),
    )

    assert completed.returncode != 0, "a deployed environment started anyway"
    assert "EmailDeliveryUnavailable" in completed.stderr
    assert "delivers nothing" in completed.stderr


def test_the_web_application_starts_once_delivery_is_configured():
    """The same interpreter, with the adapter selected, imports cleanly."""
    import os
    import subprocess
    import sys

    environment = dict(
        os.environ,
        CORRIDOR_EMAIL_BACKEND="ses",
        CORRIDOR_SIGN_IN_SENDER="no-reply@example.com",
        CORRIDOR_ENVIRONMENT="nonproduction",
        # Also resolved at import: a deployed environment refuses to start
        # without the origin its sign-in links are built from.
        CORRIDOR_PUBLIC_ORIGIN="https://pilot.example.com",
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import corridor.web.auth"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(pathlib.Path(__file__).parents[1]),
    )

    assert completed.returncode == 0, completed.stderr
