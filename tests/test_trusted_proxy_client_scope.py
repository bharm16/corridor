"""Who the rate limiter thinks a caller is, once the ALB is in front.

`auth.client_scope` uses `request.client.host` and deliberately ignores raw
`X-Forwarded-For`, because trusting a caller-supplied header would let anyone
reset their own backoff. That decision is right, and it moves the problem: the
peer the web process sees behind a load balancer is the load balancer, so every
caller collapses into one throttle identity and one anonymous caller can spend
the whole sign-in allowance for everybody.

The resolution belongs at the ASGI layer, which is what `--proxy-headers
--forwarded-allow-ips <vpc cidr>` configures. These tests pin that behaviour
directly rather than trusting the flag to mean what it reads like.
"""

from __future__ import annotations

import asyncio

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# Must match CORRIDOR_VPC_CIDR in infra/corridor_infra/network_stack.py, which
# is what the task definition passes to --forwarded-allow-ips.
VPC_CIDR = "10.20.0.0/16"

ALB_NODE = "10.20.3.44"          # inside the VPC: the real load balancer
OUTSIDE = "203.0.113.9"          # a caller reaching the port some other way
REAL_CLIENT = "198.51.100.23"


async def _resolved_client(peer: str, forwarded_for: str | None) -> tuple:
    """The client address the application ends up seeing."""
    seen: dict = {}

    async def app(scope, receive, send):
        seen["client"] = scope.get("client")

    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=VPC_CIDR)
    await middleware(
        {
            "type": "http",
            "scheme": "http",
            "client": (peer, 51234),
            "headers": headers,
        },
        None,
        None,
    )
    return seen["client"]


def test_a_forwarded_header_from_the_load_balancer_is_honoured():
    """The ALB is inside the VPC, so its X-Forwarded-For is the real client."""
    client = asyncio.run(_resolved_client(ALB_NODE, REAL_CLIENT))

    assert client[0] == REAL_CLIENT


def test_two_callers_behind_the_load_balancer_get_different_scopes():
    """The failure this prevents: both throttled under the ALB's address."""
    first = asyncio.run(_resolved_client(ALB_NODE, "198.51.100.1"))
    second = asyncio.run(_resolved_client(ALB_NODE, "198.51.100.2"))

    assert first[0] != second[0]
    assert first[0] == "198.51.100.1"
    assert second[0] == "198.51.100.2"


def test_an_untrusted_peer_cannot_choose_its_own_scope():
    """A caller reaching the port from outside the VPC forging the header must
    keep its own address, or it can reset its backoff at will."""
    client = asyncio.run(_resolved_client(OUTSIDE, "1.2.3.4"))

    assert client[0] == OUTSIDE
    assert client[0] != "1.2.3.4"


def test_the_leftmost_forwarded_address_wins_through_a_trusted_chain():
    """A proxy chain appends, so the original client is the first entry."""
    client = asyncio.run(
        _resolved_client(ALB_NODE, f"{REAL_CLIENT}, 10.20.9.9")
    )

    assert client[0] == REAL_CLIENT


def test_ipv6_clients_resolve_too():
    client = asyncio.run(_resolved_client(ALB_NODE, "2001:db8::1234"))

    assert client[0] == "2001:db8::1234"


def test_the_configured_cidr_matches_the_stack():
    """The flag value in the task definition and the network the ALB actually
    sits in have to be the same string; drift silently disables the whole
    mechanism."""
    import pathlib
    import re

    network = (
        pathlib.Path(__file__).parents[1]
        / "infra"
        / "corridor_infra"
        / "network_stack.py"
    ).read_text()
    declared = re.search(r'CORRIDOR_VPC_CIDR = "([^"]+)"', network)

    assert declared, "CORRIDOR_VPC_CIDR is not declared in the network stack"
    assert declared.group(1) == VPC_CIDR
