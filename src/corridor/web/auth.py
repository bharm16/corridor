"""HTTP glue for individual sign-in: cookies, forgery tokens, mail seam (#331).

The rules and records live in :mod:`corridor.access`.  This module only carries
them across the HTTP boundary: it reads the session cookie, extracts the
request-forgery token a write must echo, sets and clears cookies with safe
flags, and defines the replaceable email seam.  It imports the domain module but
never ``app``, so the FastAPI dependency that ties them together can live beside
the routes without a circular import.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from corridor.config import settings

from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from corridor import access
from corridor.models import WebSession

logger = logging.getLogger("corridor.sign_in")

SESSION_COOKIE = "corridor_session"
CSRF_COOKIE = "corridor_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"

# Only these methods read; every other method is a write and must clear the
# forgery check.  HEAD/OPTIONS ride with GET.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def load_session(
    request: Request, db: Session
) -> WebSession | None:
    """Resolve the live session named by the request's cookie, or ``None``."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    return access.resolve_web_session(db, raw)


async def extract_csrf(request: Request) -> str | None:
    """The forgery token a write supplied, from header or form field.

    Awaiting ``request.form()`` here is safe: Starlette caches the parsed form on
    the request, so the route's own ``Form(...)`` parameters read the same copy.
    """
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()
        value = form.get(CSRF_FIELD)
        if isinstance(value, str):
            return value
    return None


def set_session_cookies(response: Response, new_session: access.NewWebSession) -> None:
    """Hand the browser its session and forgery cookies with safe flags.

    The session cookie is HttpOnly so script cannot read it; both are Secure and
    ``SameSite=Strict`` so no cross-site request carries them — a forged POST
    arrives with no session at all.  The forgery cookie is readable so a
    server-rendered form can echo it back in its hidden field.
    """
    response.set_cookie(
        SESSION_COOKIE,
        new_session.raw_session_id,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        new_session.raw_csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def client_scope(request: Request) -> str:
    """A stable per-caller throttle key: the peer address, never a spoofable header.

    ``X-Forwarded-For`` is deliberately ignored; trusting it would let a caller
    reset its own backoff by forging a header.  A deployment that terminates at a
    trusted proxy resolves the real client upstream.
    """
    client = request.client
    if client is None or not client.host:
        return "unknown"
    return client.host


@runtime_checkable
class EmailSender(Protocol):
    """The replaceable mail seam: tests inject a non-sending capture."""

    def send_sign_in_link(self, *, email: str, link: str) -> None: ...


class LoggingEmailSender:
    """Default seam that never puts the link (a live secret) into the logs.

    Real delivery is a deployment concern: inject an SMTP-backed sender.  This
    default records only that a link was issued, so a misconfigured deployment
    fails safe and visibly rather than mailing — or logging — a usable secret.
    """

    def send_sign_in_link(self, *, email: str, link: str) -> None:
        logger.info("sign-in link issued (delivery adapter not configured)")


class RecordingEmailSender:
    """In-memory capture for tests; holds links without ever sending them."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_sign_in_link(self, *, email: str, link: str) -> None:
        self.sent.append((email, link))


class EmailDeliveryUnavailable(RuntimeError):
    """Delivery is not configured, or the provider refused the message."""


class SesEmailSender:
    """Delivers the link through Amazon SES.

    The link is a live credential, so it appears in the message body and
    nowhere else: not in a log line, not in an exception, not in the bounded
    failure this raises. A delivery failure has to be visible -- a deployment
    that silently swallows it reports success to someone who will never receive
    anything -- but it carries the recipient and SES's own error code only.
    """

    def __init__(self, *, sender: str, region: str, client=None) -> None:
        if not sender:
            raise EmailDeliveryUnavailable(
                "CORRIDOR_SIGN_IN_SENDER is required by the ses backend; SES "
                "refuses an unverified sender identity"
            )
        self.sender = sender
        self._client = client
        self._region = region

    @property
    def client(self):
        if self._client is None:  # pragma: no cover - constructed in deployment
            import boto3

            self._client = boto3.client("ses", region_name=self._region)
        return self._client

    def send_sign_in_link(self, *, email: str, link: str) -> None:
        try:
            self.client.send_email(
                Source=self.sender,
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {"Data": "Your Corridor sign-in link"},
                    "Body": {
                        "Text": {
                            "Data": (
                                "Open this link to sign in to Corridor. It can "
                                f"be used once and then expires.\n\n{link}\n"
                            )
                        }
                    },
                },
            )
        except Exception as error:  # noqa: BLE001 - bounded, and never the link
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            raise EmailDeliveryUnavailable(
                f"sign-in link could not be delivered to {email}"
                + (f" ({code})" if code else "")
            ) from None


# Deployments that may run without real delivery. Anything else refuses to
# start rather than reporting success to a person who receives nothing.
_LOCAL_ENVIRONMENTS = frozenset({"development", "test"})


def build_email_sender(configured=None) -> EmailSender:
    """The deployment's sender, or a refusal to start without one.

    `/sign-in/request` answers the same way whether or not a link was actually
    delivered, which is right -- it must not disclose whether an address is
    enrolled -- but it means a missing adapter is invisible from the outside.
    A deployed environment therefore fails closed here, at startup, instead of
    accepting sign-in requests it cannot fulfil.
    """

    configured = configured or settings
    backend = (configured.email_backend or "logging").lower()

    if backend == "ses":
        return SesEmailSender(
            sender=configured.sign_in_sender_address,
            region=configured.storage_s3_region or "us-east-2",
        )
    if backend == "logging":
        if configured.environment not in _LOCAL_ENVIRONMENTS:
            raise EmailDeliveryUnavailable(
                f"CORRIDOR_EMAIL_BACKEND=logging delivers nothing, and "
                f"CORRIDOR_ENVIRONMENT={configured.environment!r} is a deployed "
                "environment. Nobody could complete a sign-in. Set "
                "CORRIDOR_EMAIL_BACKEND=ses and CORRIDOR_SIGN_IN_SENDER."
            )
        return LoggingEmailSender()
    raise EmailDeliveryUnavailable(f"unknown CORRIDOR_EMAIL_BACKEND {backend!r}")


class PublicOriginInvalid(RuntimeError):
    """The configured origin cannot be used to build a credential URL."""


def build_public_origin(configured=None) -> str:
    """The origin every sign-in link is built from, or "" to use the request.

    Returning "" is only ever allowed for a local clone: `make queue` serves on
    whatever port the developer chose and there is nothing to protect. Every
    other deployment must configure this, because the alternative is deriving a
    credential URL from a header the caller controls.
    """

    configured = configured or settings
    origin = (configured.public_origin or "").strip().rstrip("/")
    local = configured.environment in _LOCAL_ENVIRONMENTS

    if not origin:
        if local:
            return ""
        raise PublicOriginInvalid(
            "CORRIDOR_PUBLIC_ORIGIN is required. Without it a sign-in link is "
            "built from the request's own Host header, so a forged host makes "
            "Corridor email the real user a valid token pointing somewhere "
            "else. Set it to the deployment's public origin, e.g. "
            "https://pilot.example.com."
        )

    parsed = urlparse(origin)
    if parsed.scheme != "https" and not local:
        raise PublicOriginInvalid(
            f"CORRIDOR_PUBLIC_ORIGIN must be https, got {parsed.scheme or 'no'} "
            "scheme; a sign-in token may not travel over plaintext"
        )
    if not parsed.hostname:
        raise PublicOriginInvalid(f"CORRIDOR_PUBLIC_ORIGIN has no host: {origin!r}")
    if parsed.username or parsed.password:
        raise PublicOriginInvalid(
            "CORRIDOR_PUBLIC_ORIGIN must carry no credentials"
        )
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise PublicOriginInvalid(
            "CORRIDOR_PUBLIC_ORIGIN must be a bare origin with no path, query "
            f"or fragment, got {origin!r}"
        )

    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


# Resolved at import, so a deployment configured without delivery fails when
# the web application starts rather than on the first sign-in request. A user
# hitting the lazy version would see the same success page as always, because
# /sign-in/request must not disclose whether an address is enrolled.
_DEFAULT_SENDER: EmailSender = build_email_sender()

# Same reasoning: a misconfigured origin is a startup fault, not something to
# discover when the first user follows a link to the wrong host.
PUBLIC_ORIGIN: str = build_public_origin()


def get_email_sender() -> EmailSender:
    """FastAPI dependency; overridden in tests with a recording sender."""

    return _DEFAULT_SENDER
