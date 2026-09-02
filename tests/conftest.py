"""Parallel test-process isolation over the real PostgreSQL stack.

Ordinary tests keep defining their own rollback-scoped ``session`` fixtures.
This harness only changes which migrated database each xdist worker reaches, so
schema rehearsals and fixed fixture identities cannot deadlock across workers.
Serial pytest runs continue to use the configured development database.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

from dotenv import dotenv_values
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
)
DATABASE_PREFIX = "corridor_pytest_"
RUN_ID_ENV = "CORRIDOR_PYTEST_RUN_ID"
SOURCE_DATABASE_URL_ENV = "CORRIDOR_PYTEST_SOURCE_DATABASE_URL"
_RUN_ID = re.compile(r"^([1-9][0-9]*)_[0-9a-f]{8}$")
_WORKER_ID = re.compile(r"^gw[0-9]+$")
_DATABASE_NAME = re.compile(
    r"^corridor_pytest_([1-9][0-9]*)_[0-9a-f]{8}_(gw[0-9]+|tmpl)$"
)
TEMPLATE_ENV = "CORRIDOR_PYTEST_TEMPLATE"


def pytest_configure(config) -> None:
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id is None:
        if _xdist_is_enabled(config):
            parsed = _validated_admin_url(_configured_database_url())
            reap_abandoned_worker_databases(parsed)
            run_id = new_worker_run_id()
            os.environ[RUN_ID_ENV] = run_id
            config._corridor_pytest_run_id = run_id
            # Migrate once for the whole run. Every worker used to run the
            # full chain against its own fresh database, so an ordinary
            # change replayed it once per worker per job; cloning a migrated
            # template does that work once and removes the concurrent
            # cluster-role DDL those parallel migrations were racing on
            # (#548, #545).
            template = f"{DATABASE_PREFIX}{run_id}_tmpl"
            _create_database(parsed, template)
            try:
                _migrate_database(parsed.set(database=template))
            except BaseException:
                _drop_databases(parsed, (template,))
                raise
            os.environ[TEMPLATE_ENV] = template
        return

    run_id = os.environ.get(RUN_ID_ENV, "")
    if _RUN_ID.fullmatch(run_id) is None or _WORKER_ID.fullmatch(worker_id) is None:
        raise RuntimeError("parallel test worker identity is malformed")
    database_name = f"{DATABASE_PREFIX}{run_id}_{worker_id}"
    if _DATABASE_NAME.fullmatch(database_name) is None:
        raise RuntimeError("parallel test database name is outside the guarded namespace")

    admin_url = _configured_database_url()
    parsed = _validated_admin_url(admin_url)
    os.environ[SOURCE_DATABASE_URL_ENV] = parsed.render_as_string(
        hide_password=False
    )
    template = os.environ.get(TEMPLATE_ENV, "")
    if template and _DATABASE_NAME.fullmatch(template):
        _clone_database(parsed, template, database_name)
    else:
        # No template: a single-worker run, or a controller that could not
        # build one. Fall back to migrating this database directly.
        _create_database(parsed, database_name)
        worker_url = parsed.set(database=database_name)
        os.environ["DATABASE_URL"] = worker_url.render_as_string(
            hide_password=False
        )
        try:
            _migrate_database(worker_url)
        except BaseException:
            _drop_databases(parsed, (database_name,))
            raise
    worker_url = parsed.set(database=database_name)
    os.environ["DATABASE_URL"] = worker_url.render_as_string(hide_password=False)
    config._corridor_pytest_database = (parsed, database_name)


def pytest_unconfigure(config) -> None:
    worker_database = getattr(config, "_corridor_pytest_database", None)
    if worker_database is not None:
        parsed, database_name = worker_database
        corridor_db = sys.modules.get("corridor.db")
        if corridor_db is not None:
            corridor_db.engine.dispose()
        _drop_databases(parsed, (database_name,))
        return

    run_id = getattr(config, "_corridor_pytest_run_id", None)
    if run_id is None:
        return
    parsed = _validated_admin_url(_configured_database_url())
    _drop_databases(parsed, _run_database_names(parsed, run_id))
    os.environ.pop(RUN_ID_ENV, None)
    os.environ.pop(TEMPLATE_ENV, None)


@pytest.fixture(scope="session")
def shared_source_database_url() -> str:
    """Configured shared state for the few explicitly live-state tests."""

    return os.environ.get(SOURCE_DATABASE_URL_ENV) or _configured_database_url()


@pytest.fixture
def runtime_database():
    """Harness-owned migrated database for real competing Due Work transactions."""

    from corridor.config import settings
    from corridor.m8_acceptance_database import provision_disposable_postgres

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_due_work_test_",
        reuse_migrated_template=True,
    ) as database:
        yield database


def _xdist_is_enabled(config) -> bool:
    workers = config.getoption("numprocesses")
    return workers not in (None, 0, "0")


def new_worker_run_id() -> str:
    """Return a run identity whose owner can be checked after an interrupted run."""

    return f"{os.getpid()}_{uuid4().hex[:8]}"


def _configured_database_url() -> str:
    dotenv = dotenv_values(ROOT / ".env")
    return str(
        os.environ.get("DATABASE_URL")
        or dotenv.get("DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )


def _validated_admin_url(database_url: str) -> URL:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        raise RuntimeError("parallel tests require PostgreSQL")
    if parsed.host not in {None, "localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("parallel tests require local PostgreSQL")
    engine = create_engine(
        parsed,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            version = str(connection.scalar(text("show server_version")))
    finally:
        engine.dispose()
    if not version.startswith("16."):
        raise RuntimeError("parallel tests require PostgreSQL 16")
    return parsed


def _create_database(admin_url: URL, database_name: str) -> None:
    engine = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            connection.execute(text(f'create database "{database_name}"'))
    finally:
        engine.dispose()


def _clone_database(admin_url: URL, template: str, database_name: str) -> None:
    """Copy one migrated template, retrying while another worker holds it.

    PostgreSQL locks the template for the duration of a copy, so workers
    starting together collide; the collision is expected and brief.
    """

    engine = create_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        for attempt in range(30):
            with engine.connect() as connection:
                try:
                    connection.execute(
                        text(
                            f'create database "{database_name}" '
                            f'template "{template}"'
                        )
                    )
                except Exception as error:  # noqa: BLE001 - narrowed below
                    if "being accessed by other users" not in str(error):
                        raise
                    time.sleep(0.2)
                    continue
                return
        raise RuntimeError("parallel test template stayed busy")
    finally:
        engine.dispose()


def _migrate_database(database_url: URL) -> None:
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
            str(ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("parallel test worker database migration failed")


def _worker_database_names(admin_url: URL) -> tuple[str, ...]:
    engine = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            return tuple(
                connection.scalars(
                    text(
                        "select datname from pg_database "
                        "where datname like :prefix order by datname"
                    ),
                    {"prefix": f"{DATABASE_PREFIX}%"},
                ).all()
            )
    finally:
        engine.dispose()


def _run_database_names(admin_url: URL, run_id: str) -> tuple[str, ...]:
    prefix = f"{DATABASE_PREFIX}{run_id}_"
    names = _worker_database_names(admin_url)
    return tuple(
        name
        for name in names
        if str(name).startswith(prefix) and _DATABASE_NAME.fullmatch(str(name))
    )


def reap_abandoned_worker_databases(admin_url: URL) -> None:
    """Drop worker databases only when their controller process no longer exists."""

    abandoned = []
    for database_name in _worker_database_names(admin_url):
        match = _DATABASE_NAME.fullmatch(str(database_name))
        if match is None or _process_is_running(int(match.group(1))):
            continue
        abandoned.append(str(database_name))
    _drop_databases(admin_url, tuple(abandoned))


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _drop_databases(admin_url: URL, database_names: tuple[str, ...]) -> None:
    guarded_names = tuple(
        name for name in database_names if _DATABASE_NAME.fullmatch(name)
    )
    if not guarded_names:
        return
    engine = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            for database_name in guarded_names:
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
    finally:
        engine.dispose()
