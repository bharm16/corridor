"""Keep HTTP customer identity separate from the existing project authorization.

Sign-in and probes start from a deployment-owned environment binding. After a
magic link authenticates the person, a signed cookie binds that same customer
to the exact session secret. Caller-supplied hosts, headers and project IDs are
never customer selectors. No missing or bad binding falls back to a database.
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
from corridor.web.auth import SESSION_COOKIE


CUSTOMER_COOKIE = "corridor_customer"
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
                identity = signer.authenticate(binding, raw)
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
