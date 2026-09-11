"""Provision guarded disposable PostgreSQL for acceptance and rehearsal runs.

Ad-hoc database creation was rejected because cleanup, migration identity, and
production separation then depended on each caller. This module owns that
lifecycle and applies ADR-0049's server-observed production-database refusal
before any provisioned database reaches an experimental workflow.

It now owns the *names* too. Owning the lifecycle while every caller minted its
own prefix meant nothing could recognise a disposable database from outside the
process that made it: eight modules invented eight unrelated prefixes, and
`scripts/clean_test_databases.py` chased them with twenty-two hand-written
regular expressions that missed most of them and matched single-digit pids
only. A caller now names a *label*, this module builds
`corridor_disposable_<label>_<pid>_<hex>` from it, and
`is_disposable_database_name` is the one predicate the sweeper and the
namespace guards ask.

The pid is in the name because process ownership is how an abandoned copy is
recognised: `reclaim_abandoned_database_copies` and `_drop_abandoned_templates`
drop a database only when `os.kill(pid, 0)` proves its owner is gone, so a
sibling xdist worker mid-build is never taken out from under itself.

Refusals are `DisposableDatabaseRefused`. Callers used to pass an `error_cls`
in — seventy-eight call sites, four different classes, all describing the same
refusal — and a caller whose public seam owes its own exception type catches
this one and re-raises.
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
import time
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


class DisposableDatabaseRefused(ValueError):
    """A guarded disposable database could not be provisioned, or was unsafe."""


MAX_DATABASE_NAME_LENGTH = 63
DISPOSABLE_NAMESPACE = "corridor_disposable_"
_LABEL = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_DISPOSABLE_NAME = re.compile(
    rf"^{DISPOSABLE_NAMESPACE}[a-z0-9_]+_(?P<pid>[1-9][0-9]*)_[0-9a-f]{{12}}$"
)


def disposable_database_prefix(label: str) -> str:
    """The prefix every database minted for one label shares.

    Callers hold this to write a `LIKE` scan or to refuse a name from outside
    their own label's namespace; they no longer invent the prefix itself.
    """

    if _LABEL.fullmatch(label) is None:
        raise DisposableDatabaseRefused(
            f"disposable database label {label!r} is not lowercase_snake_case"
        )
    return f"{DISPOSABLE_NAMESPACE}{label}_"


def disposable_database_name(label: str) -> str:
    """Name a disposable database so a later process can prove it is abandoned."""

    database_name = (
        f"{disposable_database_prefix(label)}{os.getpid()}_{uuid4().hex[:12]}"
    )
    if len(database_name) > MAX_DATABASE_NAME_LENGTH:
        raise DisposableDatabaseRefused(
            f"disposable database name {database_name!r} exceeds PostgreSQL's "
            f"{MAX_DATABASE_NAME_LENGTH}-character limit; shorten the label"
        )
    return database_name


def is_disposable_database_name(name: str) -> bool:
    """Whether this module minted the name, and may therefore reclaim it."""

    return _DISPOSABLE_NAME.fullmatch(str(name)) is not None


def disposable_database_owner_pid(name: str) -> int | None:
    """The process that minted the name, or `None` if this module did not."""

    match = _DISPOSABLE_NAME.fullmatch(str(name))
    return int(match.group("pid")) if match else None


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
    label: str,
    migration_revision: str = "head",
    reuse_migrated_template: bool = True,
    template_database: str | None = None,
) -> Iterator[ProvisionedDatabase]:
    """Create, migrate to one revision, and destroy a guarded PostgreSQL database.

    ``label`` names the workflow the database belongs to; this module builds the
    name from it, so every disposable database lives in one recognisable
    namespace. ``reuse_migrated_template`` copies a per-process template that
    this module migrated to the requested revision, instead of replaying the
    same Alembic path for every database. Each revision gets a distinct
    template.

    Copying is the default because replaying the chain costs three seconds
    against a tenth of a second for the copy, and the callers that were paying
    it wanted the schema, not the replay. A caller whose subject *is* the
    migration -- a fresh-database contract, or a faked chain -- now asks for
    the replay by passing ``False``.

    ``template_database`` names a migrated database the caller already owns,
    so a harness that migrated one schema for its whole run does not have this
    module migrate a second one per process. The caller owns that database's
    lifecycle; this module only copies it.
    """

    error_cls = DisposableDatabaseRefused
    if template_database is not None and migration_revision != "head":
        raise error_cls(
            "a caller-owned template carries the revision it was migrated to; "
            "a historical revision needs its own migration"
        )
    parsed = make_url(admin_url)
    if parsed.get_backend_name() != "postgresql":
        raise error_cls("a disposable database requires PostgreSQL")
    require_local_postgres_host(parsed.host, error_cls=error_cls)
    maintenance_url = parsed.set(database="postgres")
    admin_engine = create_engine(
        maintenance_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        future=True,
    )
    uses_template = reuse_migrated_template and template_database is None
    database_name = disposable_database_name(label)
    database_created = False
    database_engine = None
    primary_error: BaseException | None = None
    try:
        with admin_engine.connect() as connection:
            version = connection.scalar(text("show server_version"))
        require_postgres_16(
            version,
            error_cls=error_cls,
            target=_url_target(parsed),
        )
        template_name: str | None = template_database
        if uses_template:
            template_name = _ensure_migrated_template(
                admin_engine,
                maintenance_url,
                repo_root=repo_root,
                error_cls=error_cls,
                label=label,
                migration_revision=migration_revision,
            )
        elif template_name is not None:
            # Reclaiming this label's abandoned copies is on the way to
            # building a template, not part of building one. A caller who
            # brings a template skips that path and would otherwise leave
            # every copy a killed run abandoned for the sweeper to find.
            with admin_engine.connect() as connection:
                reclaim_abandoned_database_copies(connection, label)
        if template_name is None:
            with admin_engine.connect() as connection:
                connection.execute(text(f'create database "{database_name}"'))
        else:
            _copy_migrated_template(
                admin_engine,
                template_name,
                database_name,
                error_cls=error_cls,
            )
        database_created = True
        database_url = parsed.set(database=database_name)
        if template_name is None:
            apply_schema_migrations(
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
                    "also failed to drop disposable database "
                    f"{database_name!r}: {cleanup_error}"
                )
            else:
                raise error_cls(
                    f"could not drop disposable database {database_name!r}"
                ) from cleanup_error


def _url_target(parsed: URL) -> str:
    """Where a refusal actually looked, rather than the development default.

    The message used to name `localhost:5433` outright. A run pointed at any
    other local port then reported the wrong address in its own refusal, which
    is exactly the port confusion the boot guard exists to prevent.
    """

    return f"{parsed.host or 'localhost'}:{parsed.port or 5432}"


def require_postgres_16(
    version: str, *, error_cls: type[Exception], target: str | None = None
) -> None:
    if not str(version).startswith("16."):
        raise error_cls(
            "a disposable database requires PostgreSQL 16"
            + (f" on {target}" if target else "")
            + f", got {version!r}"
        )


def require_local_postgres_host(
    host: str | None, *, error_cls: type[Exception]
) -> None:
    if host not in {None, "localhost", "127.0.0.1", "::1"}:
        raise error_cls("a disposable database must target local PostgreSQL only")


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


_TEMPLATE_LABEL = "migrated_template"
_TEMPLATE_PREFIX = disposable_database_prefix(_TEMPLATE_LABEL)
_TEMPLATE_COPY_ATTEMPTS = 30
_TEMPLATE_COPY_PAUSE_SECONDS = 0.2
_migrated_templates: dict[tuple[str, str], str] = {}


def _copy_migrated_template(
    admin_engine,
    template_name: str,
    database_name: str,
    *,
    error_cls: type[Exception],
) -> None:
    """Copy a migrated template, waiting out another copier that holds it.

    PostgreSQL refuses to copy a database another session is connected to. A
    per-process template is only ever copied by the process that made it, but
    a template the caller owns is shared by construction -- every xdist worker
    copies the one schema its run migrated -- so the collision is expected and
    brief. ``tests/conftest.py`` has always waited it out when copying its own
    worker databases from that same template.
    """

    for _ in range(_TEMPLATE_COPY_ATTEMPTS):
        with admin_engine.connect() as connection:
            try:
                connection.execute(
                    text(
                        f'create database "{database_name}" '
                        f'template "{template_name}"'
                    )
                )
            except Exception as exc:  # noqa: BLE001 - narrowed immediately
                if "being accessed by other users" not in str(exc):
                    raise
            else:
                return
        time.sleep(_TEMPLATE_COPY_PAUSE_SECONDS)
    raise error_cls(f"migrated template {template_name!r} stayed busy")


def _ensure_migrated_template(
    admin_engine,
    admin_url: URL,
    *,
    repo_root: Path,
    error_cls: type[Exception],
    label: str,
    migration_revision: str,
) -> str:
    """Migrate one template per process and requested revision.

    The first request still runs the real Alembic path. Later tests clone that
    exact schema and can seed their own historical rows before upgrading it.
    """

    admin_url_text = admin_url.render_as_string(hide_password=False)
    cache_key = (admin_url_text, migration_revision)
    with admin_engine.connect() as connection:
        reclaim_abandoned_database_copies(connection, label)
        cached = _migrated_templates.get(cache_key)
        if cached is not None:
            return cached
        _drop_abandoned_templates(connection)
        template_name = disposable_database_name(_TEMPLATE_LABEL)
        connection.execute(text(f'drop database if exists "{template_name}"'))
        connection.execute(text(f'create database "{template_name}"'))
    try:
        apply_schema_migrations(
            admin_url.set(database=template_name),
            repo_root=repo_root,
            error_cls=error_cls,
            revision=migration_revision,
        )
    except BaseException:
        _drop_template(cache_key, admin_url_text, template_name)
        raise
    _migrated_templates[cache_key] = template_name
    atexit.register(_drop_template, cache_key, admin_url_text, template_name)
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
        owner_pid = disposable_database_owner_pid(str(name))
        if owner_pid is None or process_is_running(owner_pid):
            continue
        try:
            connection.execute(text(f'drop database if exists "{name}"'))
        except Exception:
            continue


def reclaim_abandoned_database_copies(connection, label: str) -> None:
    """Drop one label's disposable databases once their owning process exited."""

    prefix = disposable_database_prefix(label)
    names = connection.scalars(
        text("select datname from pg_database where datname like :prefix"),
        {"prefix": f"{prefix}%"},
    )
    for raw_name in names:
        database_name = str(raw_name)
        owner_pid = disposable_database_owner_pid(database_name)
        if owner_pid is None or process_is_running(owner_pid):
            continue
        try:
            connection.execute(
                text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = :name and pid <> pg_backend_pid()"
                ),
                {"name": database_name},
            )
            connection.execute(text(f'drop database if exists "{database_name}"'))
        except Exception:
            continue


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _drop_template(
    cache_key: tuple[str, str],
    admin_url_text: str,
    template_name: str,
) -> None:
    """Destroy a process template, tolerating an already-torn-down server."""

    _migrated_templates.pop(cache_key, None)
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
    label: str,
    expected_current_revision: str,
    target_revision: str,
) -> DatabaseUpgradeReceipt:
    """Upgrade only an explicitly named disposable database between exact pins."""

    if not database.name.startswith(
        disposable_database_prefix(label)
    ) or not is_disposable_database_name(database.name):
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
    apply_schema_migrations(
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


def apply_schema_migrations(
    database_url: URL,
    *,
    repo_root: Path,
    error_cls: type[Exception],
    revision: str = "head",
) -> None:
    """Run Alembic against one database in a subprocess, and report its failure.

    Public because the pytest harness upgrades its own worker databases and had
    a byte-for-byte copy of this body in `tests/conftest.py`.
    """
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
