"""Keep measurements, replays, and model trials out of production PostgreSQL.

ADR-0049 separates production and experimental Extraction Runs by database
location, not by mutable purpose labels. Comparing URL text was rejected: host
aliases and alternate roles can still reach the same database. The guard reads
the PostgreSQL cluster identifier and current database from both connections,
then verifies any injected session adapter against the explicitly named target.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from corridor.config import settings


class ProductionDatabaseRefusal(ValueError):
    """An experiment did not prove a database distinct from production."""


@dataclass(frozen=True)
class DatabaseLocation:
    """Server-observed identity of one exact PostgreSQL database and role."""

    database: str
    username: str
    server_address: str
    server_port: int
    system_identifier: str

    @property
    def database_identity(self) -> tuple[str, str]:
        """Identity relevant to production separation; roles do not distinguish it."""

        return (self.system_identifier, self.database)


class SessionFactory(Protocol):
    """Construct one context-managed SQLAlchemy Session adapter."""

    def __call__(self) -> AbstractContextManager[Session]: ...


class DatabaseGuard(Protocol):
    """Verify the exact Session that an experimental command will use."""

    def __call__(
        self,
        database_url: str | None,
        *,
        session: Session | None = None,
    ) -> DatabaseLocation: ...


def require_experimental_database(
    database_url: str | None,
    *,
    session: Session | None = None,
    production_database_url: str | None = None,
) -> DatabaseLocation:
    """Return the proved target identity or refuse production and mismatched adapters.

    ``database_url`` is always explicit. When a command supplies ``session``,
    that exact live connection must reach the same database and role.
    """

    target_url = _explicit_postgres_url(database_url, label="experimental")
    production_url = _explicit_postgres_url(
        production_database_url or settings.database_url,
        label="production",
    )
    target = _observe_database_url(target_url)
    production = _observe_database_url(production_url)
    if target.database_identity == production.database_identity:
        raise ProductionDatabaseRefusal(
            "experimental command refused the configured production database"
        )

    if session is not None:
        try:
            injected = _observe_session(session)
        except ProductionDatabaseRefusal:
            raise
        except Exception as exc:
            raise ProductionDatabaseRefusal(
                "could not verify the injected session database"
            ) from exc
        if injected != target:
            raise ProductionDatabaseRefusal(
                "injected session does not reach the explicitly named "
                "experimental database"
            )
    return target


@contextmanager
def experimental_session(
    database_url: str | None,
    *,
    session_factory: SessionFactory | None = None,
    database_guard: DatabaseGuard = require_experimental_database,
) -> Iterator[Session]:
    """Open one guarded experimental Session through production or test adapters."""

    if session_factory is not None:
        with session_factory() as session:
            owns_transaction = bool(
                hasattr(session, "in_transaction")
                and not session.in_transaction()
            )
            if owns_transaction:
                with session.begin():
                    database_guard(database_url, session=session)
                    yield session
            else:
                database_guard(database_url, session=session)
                yield session
        return

    target_url = _explicit_postgres_url(database_url, label="experimental")
    engine = create_engine(target_url, poolclass=NullPool, future=True)
    factory = sessionmaker(bind=engine)
    try:
        with factory() as session:
            with session.begin():
                database_guard(database_url, session=session)
                yield session
    finally:
        engine.dispose()


def _explicit_postgres_url(database_url: str | None, *, label: str) -> str:
    if (
        not isinstance(database_url, str)
        or not database_url
        or database_url != database_url.strip()
    ):
        raise ProductionDatabaseRefusal(
            f"{label} database URL must be explicit and non-empty"
        )
    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise ProductionDatabaseRefusal(
            f"{label} database URL is invalid"
        ) from exc
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.query
        or not parsed.database
        or not parsed.username
    ):
        raise ProductionDatabaseRefusal(
            f"{label} database must be one directly named PostgreSQL database"
        )
    return database_url


def _observe_database_url(database_url: str) -> DatabaseLocation:
    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with engine.connect() as connection:
            return _observe_connection(connection)
    except ProductionDatabaseRefusal:
        raise
    except Exception as exc:
        raise ProductionDatabaseRefusal(
            "could not observe PostgreSQL database identity"
        ) from exc
    finally:
        engine.dispose()


def _observe_session(session: Session) -> DatabaseLocation:
    return _observe_connection(session.connection())


def _observe_connection(connection: Connection) -> DatabaseLocation:
    row = connection.execute(
        text(
            "select current_database(), current_user, "
            "coalesce(inet_server_addr()::text, ''), "
            "coalesce(inet_server_port(), 0), "
            "system_identifier::text from pg_control_system()"
        )
    ).one()
    location = DatabaseLocation(
        database=str(row[0]),
        username=str(row[1]),
        server_address=str(row[2]),
        server_port=int(row[3]),
        system_identifier=str(row[4]),
    )
    if (
        not location.database
        or not location.username
        or not location.system_identifier.isdigit()
    ):
        raise ProductionDatabaseRefusal(
            "observed PostgreSQL database identity is incomplete"
        )
    return location
