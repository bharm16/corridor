"""Keep HTTP customer identity separate from the existing project authorization.

Sign-in and probes start from a deployment-owned environment binding. After a
magic link authenticates the person, a signed cookie binds that same customer
to the exact session secret. Caller-supplied hosts, headers and project IDs are
never customer selectors. An invalid binding cannot authorize a read. Only
the sign-in routes may discard it and begin fresh authentication against the
configured environment, after the same registry and local identity checks.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from corridor.config import settings
from corridor.control_plane import RouteRefused
from corridor.customer_routing import CustomerSessionSigner
from corridor.customer_routing_runtime import configured_customer_router
from corridor.web.auth import SESSION_COOKIE, clear_session_cookies


CUSTOMER_COOKIE = "corridor_customer"
SIGN_IN_ROUTES = frozenset(
    {
        ("GET", "/sign-in"),
        ("POST", "/sign-in/request"),
        ("GET", "/sign-in/consume"),
    }
)
BOOTSTRAP_ROUTES = frozenset(
    {
        "/",
        "/sign-in",
        "/sign-in/request",
        "/sign-in/consume",
        "/readyz",
        "/health",
        "/intake/inbound",
    }
)


@contextmanager
def customer_session(request: Request, local_factory, *, capability: str = "web"):
    try:
        router = configured_customer_router()
        if router is None:
            with local_factory() as session:
                yield session
            return
        else:
            signer = CustomerSessionSigner(settings.customer_routing_key)
            raw = request.cookies.get(SESSION_COOKIE, "")
            binding = request.cookies.get(CUSTOMER_COOKIE, "")
            if raw or binding:
                try:
                    identity = signer.authenticate(binding, raw)
                    if identity != router.identity:
                        raise RouteRefused("customer environment unavailable")
                except RouteRefused:
                    if (request.method, request.url.path) not in SIGN_IN_ROUTES:
                        raise
                    # A stale/foreign cookie supplies no authenticated identity.
                    # Only fresh sign-in can use the configured environment;
                    # registry and local database checks still run below.
                    request.state.customer_reauthentication_required = True
                    identity = router.identity
            elif request.url.path in BOOTSTRAP_ROUTES:
                identity = router.identity
            else:
                raise HTTPException(401, "sign in to continue")
            session = router.open_session(identity, capability=capability)
    except RouteRefused:
        raise HTTPException(503, "customer environment unavailable") from None
    with session:
        yield session


def set_customer_cookie(response: Response, raw_session: str) -> None:
    router = configured_customer_router()
    if router is not None:
        binding = CustomerSessionSigner(settings.customer_routing_key).issue(
            router.identity, raw_session
        )
        response.set_cookie(
            CUSTOMER_COOKIE, binding, httponly=True, secure=True, samesite="strict"
        )


def clear_customer_cookie(response: Response) -> None:
    response.delete_cookie(
        CUSTOMER_COOKIE, httponly=True, secure=True, samesite="strict"
    )


def needs_customer_sign_in(request: Request) -> bool:
    """The sign-in routes must ignore a session whose customer binding failed."""
    return bool(getattr(request.state, "customer_reauthentication_required", False))


def clear_invalid_customer_cookies(request: Request, response: Response) -> Response:
    """Expire stale authentication on a sign-in form or unsuccessful attempt.

    Successful token consumption sets fresh cookies instead. These expirations
    do not authenticate the old session or revoke anything in a customer DB.
    """
    if needs_customer_sign_in(request):
        clear_session_cookies(response)
        clear_customer_cookie(response)
    return response
