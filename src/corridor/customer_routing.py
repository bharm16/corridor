"""Authenticate the customer route before opening its project authorization seam.

Project IDs and browser-session rows are local to one customer database. Using
either as a cross-customer selector lets colliding IDs select someone else's
record. A signed binding carries only deployment/customer/environment identity,
bound to the existing opaque session secret. The registry is re-read for every
session; engines may be reused, authorization and enabled state may not.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import hmac
import json
from threading import Lock

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from corridor.control_plane import ControlPlane, RouteRefused, identifier


@dataclass(frozen=True)
class CustomerIdentity:
    customer_id: str
    environment_id: str
    deployment_id: str

    def __post_init__(self):
        for value in (self.customer_id, self.environment_id, self.deployment_id):
            identifier(value)


class CustomerSessionSigner:
    """A server-issued customer binding; request headers cannot select a route."""

    def __init__(self, key: str):
        if len(key.encode()) < 32:
            raise RouteRefused(
                "customer routing requires a separate signing secret of at least 32 bytes"
            )
        self.key = key.encode()

    def issue(self, identity: CustomerIdentity, raw_session: str) -> str:
        if not raw_session:
            raise RouteRefused("customer authentication unavailable")
        payload = {
            **asdict(identity),
            "session_sha256": sha256(raw_session.encode()).hexdigest(),
            "version": 1,
        }
        encoded = (
            base64.urlsafe_b64encode(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )
        signature = hmac.new(self.key, encoded.encode(), sha256).hexdigest()
        return encoded + "." + signature

    def authenticate(self, binding: str, raw_session: str) -> CustomerIdentity:
        try:
            if len(binding) > 2048 or not raw_session:
                raise ValueError
            encoded, supplied = binding.split(".")
            expected = hmac.new(self.key, encoded.encode(), sha256).hexdigest()
            if not hmac.compare_digest(expected, supplied):
                raise ValueError
            payload = json.loads(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )
            if payload.pop("version") != 1 or not hmac.compare_digest(
                payload.pop("session_sha256"), sha256(raw_session.encode()).hexdigest()
            ):
                raise ValueError
            return CustomerIdentity(**payload)
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise RouteRefused("customer authentication unavailable") from None


def database_address(url: URL) -> tuple[str, int, str]:
    return url.host or "", url.port or 5432, url.database or ""


def bind_customer_environment(engine: Engine, identity: CustomerIdentity) -> None:
    """Owner-only, immutable identity attestation in an already migrated DB."""
    if engine.dialect.name != "postgresql":
        raise RouteRefused("customer environment requires PostgreSQL")
    with engine.begin() as connection:
        connection.execute(
            text(
                "insert into public.customer_environment_binding (singleton, customer_id, environment_id, deployment_id) "
                "values (true, :customer_id, :environment_id, :deployment_id) on conflict do nothing"
            ),
            asdict(identity),
        )
        rows = (
            connection.execute(
                text(
                    "select customer_id, environment_id, deployment_id from public.customer_environment_binding"
                )
            )
            .mappings()
            .all()
        )
        if len(rows) != 1 or dict(rows[0]) != asdict(identity):
            raise RouteRefused("customer database binding does not match")


class CustomerRouter:
    """One configured environment per process; no registry or URL fallback."""

    def __init__(
        self,
        control_plane: ControlPlane,
        identity: CustomerIdentity,
        credentials: Callable[[str], str],
    ):
        self.control_plane = control_plane
        self.identity = identity
        self.credentials = credentials
        self.engines: dict[str, Engine] = {}
        self.lock = Lock()

    def open_session(
        self, identity: CustomerIdentity, *, capability: str = "web"
    ) -> Session:
        if identity != self.identity or capability not in {"web", "worker"}:
            raise RouteRefused("customer environment unavailable")
        registration = self.control_plane.lookup(**asdict(identity))
        reference = (
            registration.web_credential_ref
            if capability == "web"
            else registration.worker_credential_ref
        )
        try:
            configured = self.credentials(reference)
            url = make_url(configured)
            if (
                url.drivername != "postgresql+psycopg"
                or database_address(url)
                != (
                    registration.database_host,
                    registration.database_port,
                    registration.database_name,
                )
                or database_address(url)
                == database_address(self.control_plane.engine.url)
                or url.username != f"corridor_{capability}"
                or set(url.query)
                - {"sslmode", "sslrootcert", "connect_timeout", "application_name"}
            ):
                raise ValueError
            with self.lock:
                engine = self.engines.get(configured)
                if engine is None:
                    engine = create_engine(url, hide_parameters=True)
                    self.engines[configured] = engine
            session = Session(bind=engine)
            try:
                # This is an identity-only relation, before any customer
                # authentication or content read. Endpoint aliases cannot make
                # a misconfigured credential reach a different customer's rows.
                rows = (
                    session.execute(
                        text(
                            "select customer_id, environment_id, deployment_id from public.customer_environment_binding"
                        )
                    )
                    .mappings()
                    .all()
                )
                if len(rows) != 1 or dict(rows[0]) != asdict(identity):
                    raise RouteRefused("customer database binding does not match")
                # A session factory returns an unstarted transaction. Worker
                # callers own begin()/commit(); the immutable attestation read
                # must not consume that public transaction boundary.
                session.rollback()
            except BaseException:
                session.close()
                raise
        except (SQLAlchemyError, ValueError, KeyError, TypeError):
            raise RouteRefused("customer environment unavailable") from None
        return session

    @contextmanager
    def session(
        self, identity: CustomerIdentity, *, capability: str = "web"
    ) -> Iterator[Session]:
        with self.open_session(identity, capability=capability) as session:
            yield session

    def close(self) -> None:
        for engine in self.engines.values():
            engine.dispose()
        self.engines.clear()
