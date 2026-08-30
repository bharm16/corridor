"""Provision guarded disposable PostgreSQL for acceptance and rehearsal runs.

Ad-hoc database creation was rejected because cleanup, migration identity, and
production separation then depended on each caller. This module owns that
lifecycle and applies ADR-0049's server-observed production-database refusal
before any provisioned database reaches an experimental workflow.
"""

from __future__ import annotations

import atexit
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
    reuse_migrated_template: bool = False,
) -> Iterator[ProvisionedDatabase]:
    """Create, migrate to one revision, and destroy a guarded PostgreSQL database.

    ``reuse_migrated_template`` copies a per-process template that this module
    already migrated, instead of replaying the Alembic chain per database. It
    applies only to ``head``; an exact historical pin always replays the chain,
    because rehearsing the chain is the point of a pinned revision.
    """

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
        template_name: str | None = None
        if reuse_migrated_template and migration_revision == "head":
            template_name = _ensure_migrated_template(
                admin_engine,
                parsed,
                repo_root=repo_root,
                error_cls=error_cls,
            )
        with admin_engine.connect() as connection:
            if template_name is None:
                connection.execute(text(f'create database "{database_name}"'))
            else:
                connection.execute(
                    text(
                        f'create database "{database_name}" '
                        f'template "{template_name}"'
                    )
                )
            database_created = True
        database_url = parsed.set(database=database_name)
        if template_name is None:
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
    """Read the stamped revision from the target database itself.

    Shelling out to ``alembic current`` was rejected because it costs a full
    interpreter start per call and this runs once per provisioned database.
    ``alembic current`` reports exactly the rows of ``alembic_version``, so the
    query answers the same question. ``repo_root`` stays in the signature
    because callers name the repository that owns the migration chain.
    """

    del repo_root
    engine = create_engine(
        make_url(str(database_url)),
        poolclass=NullPool,
        future=True,
    )
    try:
        with engine.connect() as connection:
            revisions = list(
                connection.scalars(text("select version_num from alembic_version"))
            )
    except Exception as exc:
        raise error_cls(f"could not read Alembic head: {exc}") from exc
    finally:
        engine.dispose()
    if len(revisions) != 1:
        raise error_cls("could not determine exactly one Alembic revision")
    return str(revisions[0])


_TEMPLATE_PREFIX = "corridor_migrated_template_"
_migrated_templates: dict[str, str] = {}


def _ensure_migrated_template(
    admin_engine,
    admin_url: URL,
    *,
    repo_root: Path,
    error_cls: type[Exception],
) -> str:
    """Migrate one reusable template per process and reuse it for every copy.

    Replaying the whole chain per disposable database dominated the runtime
    suite. PostgreSQL copies an already-migrated template in constant time, and
    the template is still built by the real chain, so a copied database is
    indistinguishable from a migrated one.
    """

    cache_key = admin_url.render_as_string(hide_password=False)
    cached = _migrated_templates.get(cache_key)
    if cached is not None:
        return cached

    template_name = f"{_TEMPLATE_PREFIX}{os.getpid()}_{uuid4().hex[:8]}"
    with admin_engine.connect() as connection:
        _drop_abandoned_templates(connection)
        connection.execute(text(f'drop database if exists "{template_name}"'))
        connection.execute(text(f'create database "{template_name}"'))
    try:
        _apply_schema_migrations(
            admin_url.set(database=template_name),
            repo_root=repo_root,
            error_cls=error_cls,
            revision="head",
        )
    except BaseException:
        _drop_template(cache_key, template_name)
        raise
    _migrated_templates[cache_key] = template_name
    atexit.register(_drop_template, cache_key, template_name)
    return template_name


def _drop_abandoned_templates(connection) -> None:
    """Reclaim templates left behind by a test process that was killed outright.

    ``atexit`` covers an orderly exit, but a killed xdist worker never runs it.
    Only a template whose owning process is gone is dropped, so a sibling worker
    building its own template at this moment is never taken out from under it.
    """

    names = connection.scalars(
        text("select datname from pg_database where datname like :prefix"),
        {"prefix": f"{_TEMPLATE_PREFIX}%"},
    ).all()
    for name in names:
        owner_pid = _template_owner_pid(str(name))
        if owner_pid is None or _process_is_running(owner_pid):
            continue
        try:
            connection.execute(text(f'drop database if exists "{name}"'))
        except Exception:
            continue


def _template_owner_pid(template_name: str) -> int | None:
    match = re.fullmatch(rf"{_TEMPLATE_PREFIX}(\d+)_[0-9a-f]+", template_name)
    return int(match.group(1)) if match else None


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _drop_template(admin_url_text: str, template_name: str) -> None:
    """Destroy a process template, tolerating an already-torn-down server."""

    _migrated_templates.pop(admin_url_text, None)
    try:
        engine = create_engine(
            make_url(admin_url_text),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
            future=True,
        )
        try:
            with engine.connect() as connection:
                connection.execute(
                    text(
                        "select pg_terminate_backend(pid) from pg_stat_activity "
                        "where datname = :name and pid <> pg_backend_pid()"
                    ),
                    {"name": template_name},
                )
                connection.execute(text(f'drop database if exists "{template_name}"'))
        finally:
            engine.dispose()
    except Exception:
        pass


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
