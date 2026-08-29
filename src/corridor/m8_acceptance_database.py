"""Provision guarded disposable PostgreSQL for acceptance and rehearsal runs.

Ad-hoc database creation was rejected because cleanup, migration identity, and
production separation then depended on each caller. This module owns that
lifecycle and applies ADR-0049's server-observed production-database refusal
before any provisioned database reaches an experimental workflow.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from corridor.experimental_database import (
    ProductionDatabaseRefusal,
    require_experimental_database,
)


@dataclass(frozen=True)
class ProvisionedDatabase:
    name: str
    session_factory: sessionmaker
    postgres_version: str
    migration_head: str

    @property
    def current_revision(self) -> str:
        """The exact revision applied, including a requested historical target."""
        return self.migration_head


@dataclass(frozen=True)
class DatabaseUpgradeReceipt:
    """Exact before/after identity for one guarded disposable-DB upgrade."""

    database_name: str
    from_revision: str
    to_revision: str
    verified_revision: str


DatabaseProvisioner = Callable[[str], Any]


@contextmanager
def provision_disposable_postgres(
    admin_url: str,
    *,
    repo_root: Path,
    error_cls: type[Exception],
    database_prefix: str,
    migration_revision: str = "head",
) -> Iterator[ProvisionedDatabase]:
    """Create, migrate to one revision, and destroy a guarded PostgreSQL database."""

    parsed = make_url(admin_url)
    if parsed.get_backend_name() != "postgresql":
        raise error_cls("M8 acceptance requires PostgreSQL")
    require_local_postgres_host(parsed.host, error_cls=error_cls)
    admin_engine = create_engine(
        parsed,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        future=True,
    )
    database_name = _disposable_database_name(database_prefix)
    database_created = False
    database_engine = None
    primary_error: BaseException | None = None
    try:
        with admin_engine.connect() as connection:
            version = connection.scalar(text("show server_version"))
        require_postgres_16(version, error_cls=error_cls)
        with admin_engine.connect() as connection:
            connection.execute(text(f'create database "{database_name}"'))
            database_created = True
        database_url = parsed.set(database=database_name)
        _apply_schema_migrations(
            database_url,
            repo_root=repo_root,
            error_cls=error_cls,
            revision=migration_revision,
        )
        database_engine = create_engine(
            database_url,
            poolclass=NullPool,
            future=True,
        )
        factory = sessionmaker(bind=database_engine, expire_on_commit=False)
        migration_head = read_migration_head(
            database_url.render_as_string(hide_password=False),
            repo_root=repo_root,
            error_cls=error_cls,
        )
        try:
            with factory() as guard_session:
                require_experimental_database(
                    database_url.render_as_string(hide_password=False),
                    session=guard_session,
                )
        except ProductionDatabaseRefusal as exc:
            raise error_cls(str(exc)) from exc
        yield ProvisionedDatabase(
            name=database_name,
            session_factory=factory,
            postgres_version=str(version),
            migration_head=migration_head,
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if database_engine is not None:
            database_engine.dispose()
        cleanup_error: Exception | None = None
        if database_created:
            try:
                with admin_engine.connect() as connection:
                    connection.execute(
                        text(
                            "select pg_terminate_backend(pid) "
                            "from pg_stat_activity "
                            "where datname = :name and pid <> pg_backend_pid()"
                        ),
                        {"name": database_name},
                    )
                    connection.execute(
                        text(f'drop database if exists "{database_name}"')
                    )
            except Exception as exc:
                cleanup_error = exc
        admin_engine.dispose()
        if cleanup_error is not None:
            if primary_error is not None:
                primary_error.add_note(
                    "M8 acceptance also failed to drop disposable database "
                    f"{database_name!r}: {cleanup_error}"
                )
            else:
                raise error_cls(
                    f"could not drop disposable database {database_name!r}"
                ) from cleanup_error


def require_postgres_16(version: str, *, error_cls: type[Exception]) -> None:
    if not str(version).startswith("16."):
        raise error_cls(
            f"M8 acceptance requires PostgreSQL 16 on localhost:5433, got {version!r}"
        )


def require_local_postgres_host(
    host: str | None, *, error_cls: type[Exception]
) -> None:
    if host not in {None, "localhost", "127.0.0.1", "::1"}:
        raise error_cls("M8 acceptance must target local PostgreSQL only")


def read_migration_head(
    database_url: str,
    *,
    repo_root: Path,
    error_cls: type[Exception],
) -> str:
    environment = {
        **os.environ,
        "DATABASE_URL": str(database_url),
    }
    completed = subprocess.run(
        [
            "uv",
            "run",
            "alembic",
            "current",
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise error_cls(
            "could not read Alembic head"
            + (f": {detail[-1]}" if detail else "")
        )
    revisions = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([0-9a-f]+) \(", line)
        if match:
            revisions.append(match.group(1))
        else:
            revisions.append(line.split()[0])
    if len(revisions) != 1:
        raise error_cls("could not determine exactly one Alembic revision")
    return revisions[0]


def upgrade_provisioned_postgres(
    database: ProvisionedDatabase,
    *,
    admin_url: str,
    repo_root: Path,
    error_cls: type[Exception],
    database_prefix: str,
    expected_current_revision: str,
    target_revision: str,
) -> DatabaseUpgradeReceipt:
    """Upgrade only an explicitly named disposable database between exact pins."""

    if (
        not database_prefix
        or not database.name.startswith(database_prefix)
        or database.name == database_prefix
        or re.fullmatch(r"[A-Za-z0-9_]+", database.name) is None
    ):
        raise error_cls("database is outside the disposable namespace")
    if database.current_revision != expected_current_revision:
        raise error_cls("provisioned database does not carry the expected source revision")
    parsed = make_url(admin_url)
    if parsed.get_backend_name() != "postgresql":
        raise error_cls("M8 acceptance requires PostgreSQL")
    require_local_postgres_host(parsed.host, error_cls=error_cls)
    database_url = parsed.set(database=database.name)
    rendered_url = database_url.render_as_string(hide_password=False)
    observed_before = read_migration_head(
        rendered_url,
        repo_root=repo_root,
        error_cls=error_cls,
    )
    if observed_before != expected_current_revision:
        raise error_cls("disposable database is not at the expected source revision")
    bound_engine = database.session_factory.kw.get("bind")
    if bound_engine is not None:
        bound_engine.dispose()
    _apply_schema_migrations(
        database_url,
        repo_root=repo_root,
        error_cls=error_cls,
        revision=target_revision,
    )
    observed_after = read_migration_head(
        rendered_url,
        repo_root=repo_root,
        error_cls=error_cls,
    )
    if observed_after != target_revision:
        raise error_cls("disposable database did not reach the exact target revision")
    return DatabaseUpgradeReceipt(
        database_name=database.name,
        from_revision=observed_before,
        to_revision=target_revision,
        verified_revision=observed_after,
    )


def _disposable_database_name(database_prefix: str) -> str:
    return f"{database_prefix}{uuid4().hex}"


def _apply_schema_migrations(
    database_url: URL,
    *,
    repo_root: Path,
    error_cls: type[Exception],
    revision: str = "head",
) -> None:
    environment = {
        **os.environ,
        "DATABASE_URL": database_url.render_as_string(hide_password=False),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(repo_root / "alembic.ini"),
            "upgrade",
            revision,
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise error_cls(
            "disposable database migration failed"
            + (f": {detail[-1]}" if detail else "")
        )
