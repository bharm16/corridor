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


_DEFAULT_SENDER = LoggingEmailSender()


def get_email_sender() -> EmailSender:
    """FastAPI dependency; overridden in tests with a recording sender."""
    return _DEFAULT_SENDER
