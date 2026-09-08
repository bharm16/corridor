"""Parallel test-process isolation over the real PostgreSQL stack.

Ordinary tests keep defining their own rollback-scoped ``session`` fixtures.
This harness only changes which migrated database each xdist worker reaches, so
schema rehearsals and fixed fixture identities cannot deadlock across workers.
Provisioning starts at the first real connection to a worker database; pure
tests never contact PostgreSQL, even when xdist is enabled. Serial pytest
runs continue to use the configured development database.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
import time
from uuid import uuid4

from dotenv import dotenv_values
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, URL, make_url
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
COORDINATION_ENV = "CORRIDOR_PYTEST_COORDINATION"
LOCAL_BROAD_REASON_ENV = "CORRIDOR_LOCAL_BROAD_REASON"
LOCAL_BROAD_REASONS = frozenset({"failure-reproduction", "performance-investigation"})


def _focused_keyword(keyword: str) -> bool:
    """Every OR branch must contain a specific positive keyword constraint."""
    # Pytest keywords can contain punctuation that is not a Python identifier.
    # Give each atom a temporary name, then inspect only the Boolean structure.
    # Negation alone, or `test or specific`, still selects essentially everything.
    atoms = {}
    tokens = []
    for token in re.findall(r"\(|\)|[^\s()]+", keyword):
        if token in {"(", ")", "and", "or", "not"}:
            tokens.append(token)
        else:
            name = f"atom_{len(atoms)}"
            atoms[name] = token not in {"test", "test_", "tests", "py", ".py", "."}
            tokens.append(name)
    try:
        expression = ast.parse(" ".join(tokens), mode="eval")
    except SyntaxError:
        return False

    def constrained(node):
        if isinstance(node, ast.Name):
            return atoms[node.id]
        if isinstance(node, ast.BoolOp):
            values = [constrained(value) for value in node.values]
            return any(values) if isinstance(node.op, ast.And) else all(values)
        return False

    return constrained(expression.body)


def _require_local_broad_reason(config) -> None:
    """Refuse accidental full local execution before collection or DB setup."""
    options = getattr(config, "option", None)
    if getattr(options, "collectonly", False) or os.environ.get("GITHUB_ACTIONS") == "true":
        return
    if os.environ.get(LOCAL_BROAD_REASON_ENV) in LOCAL_BROAD_REASONS:
        return
    if _focused_keyword(getattr(options, "keyword", "") or ""):
        return
    invocation = getattr(config, "invocation_params", None)
    invocation_dir = Path(getattr(invocation, "dir", Path.cwd()))
    test_root = (ROOT / "tests").resolve()
    # Missing/default args are conservative; mock configurations must explicitly
    # name their focused files just as an actual bounded invocation does.
    arguments = getattr(config, "args", None) or [str(test_root)]
    paths = {
        (invocation_dir / str(argument)).resolve()
        for argument in arguments if "::" not in str(argument)
    }
    selects_directory = any(path == test_root or path in test_root.parents for path in paths)
    all_files = {path.resolve() for path in test_root.glob("test_*.py")}
    selects_all_files = bool(all_files) and all_files <= paths
    if selects_directory or selects_all_files:
        raise pytest.UsageError(
            "Broad local tests require a concrete diagnostic reason. "
            "Use make test-focused ARGS='tests/test_file.py::test_name'; "
            "required CI owns the complete release proof. For an actual diagnostic, "
            "set CORRIDOR_LOCAL_BROAD_REASON=failure-reproduction or "
            "CORRIDOR_LOCAL_BROAD_REASON=performance-investigation."
        )


@dataclass
class LazyWorkerDatabase:
    """Provision only the named worker database, on its first DBAPI connection."""

    admin_url: URL
    database_name: str
    template: str = ""
    coordination: Path | None = None
    provisioned: bool = False
    cleanup_needed: bool = False
    failed: bool = False
    _provision_lock: Lock = field(default_factory=Lock, repr=False)

    def on_connect(self, dialect, connection_record, args, parameters) -> None:
        # Admin queries and runtime_database's independent scratch databases
        # must never recurse into the default worker's provisioning path.
        if dialect.name == "postgresql" and parameters.get("dbname") == self.database_name:
            self.ensure_provisioned()

    def ensure_provisioned(self) -> None:
        with self._provision_lock:
            if self.provisioned:
                return
            if self.failed:
                raise RuntimeError("parallel test worker database provisioning previously failed")
            try:
                if self.coordination is None:
                    self._provision()
                    return
                # Share one template across processes. Readiness is published
                # only after migration succeeds, while the file lock is held.
                with (self.coordination / "database.lock").open("a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        self._provision()
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
            except BaseException:
                self.failed = True
                raise

    def _provision(self) -> None:
        parsed = _validated_admin_url(
            self.admin_url.render_as_string(hide_password=False)
        )
        if self.coordination is not None:
            (self.coordination / "database-started").touch()
            ready = self.coordination / "template-ready"
            failed = self.coordination / "template-failed"
            if failed.exists():
                raise RuntimeError("parallel test template provisioning failed in another worker")
            if not ready.exists():
                reap_abandoned_worker_databases(parsed)
                try:
                    _create_database(parsed, self.template)
                    _migrate_database(parsed.set(database=self.template))
                except BaseException:
                    # A broken migration must not be attempted again by every
                    # remaining test or by the other workers in this run.
                    failed.touch()
                    _drop_databases(parsed, (self.template,))
                    raise
                ready.touch()
        self.cleanup_needed = True
        if self.template:
            _clone_database(parsed, self.template, self.database_name)
        else:
            # A worker configured without a shared controller template still
            # gets the same isolated database and real migration contract.
            _create_database(parsed, self.database_name)
            try:
                _migrate_database(parsed.set(database=self.database_name))
            except BaseException:
                _drop_databases(parsed, (self.database_name,))
                self.cleanup_needed = False
                raise
        self.provisioned = True


def _migration_cases_own_their_databases(config) -> bool:
    if getattr(getattr(config, "option", None), "markexpr", "") != "migration":
        return False
    arguments = getattr(config, "args", ())
    owner = ROOT / "tests/test_migration_baseline.py"
    return bool(arguments) and all(
        Path(str(argument).split("::", 1)[0]).resolve() == owner
        for argument in arguments
    )


def _protect_migration_identity_source(config) -> None:
    """Reuse the real source identity for migration checks, read-only.

    Every case in the migration contract provisions its own target database.
    Giving its source-identity lookup an additional empty worker database
    rebuilt the same head just to compare identities. Keep the actual source
    URL and prevent accidental writes to it instead.
    """
    source = _local_postgres_url(_configured_database_url())

    def read_only_source(dialect, connection_record, args, parameters):
        if dialect.name == "postgresql" and parameters.get("dbname") == source.database:
            options = parameters.get("options", os.environ.get("PGOPTIONS", ""))
            parameters["options"] = options + " -c default_transaction_read_only=on"

    event.listen(Engine, "do_connect", read_only_source)
    config._corridor_migration_identity_guard = read_only_source


def pytest_configure(config) -> None:
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id is None:
        _require_local_broad_reason(config)
    # These short-lived transactional test connections pay compilation cost
    # without an analytical workload to amortize it. This is a client setting,
    # not a change to the developer's PostgreSQL server or production runtime.
    os.environ.setdefault("PGOPTIONS", "-c jit=off")
    if _migration_cases_own_their_databases(config):
        _protect_migration_identity_source(config)
        return
    if worker_id is None:
        if _xdist_is_enabled(config):
            # Capture the original URL before any worker rewrites DATABASE_URL.
            # No connection, version query, orphan scan or DDL belongs here.
            source_url = _configured_database_url()
            run_id = new_worker_run_id()
            coordination = TemporaryDirectory(prefix=f"corridor-pytest-{run_id}-")
            config._corridor_pytest_controller = (source_url, run_id, coordination)
            os.environ[RUN_ID_ENV] = run_id
            os.environ[SOURCE_DATABASE_URL_ENV] = source_url
            os.environ[TEMPLATE_ENV] = f"{DATABASE_PREFIX}{run_id}_tmpl"
            os.environ[COORDINATION_ENV] = coordination.name
        return

    run_id = os.environ.get(RUN_ID_ENV, "")
    if _RUN_ID.fullmatch(run_id) is None or _WORKER_ID.fullmatch(worker_id) is None:
        raise RuntimeError("parallel test worker identity is malformed")
    database_name = f"{DATABASE_PREFIX}{run_id}_{worker_id}"
    if _DATABASE_NAME.fullmatch(database_name) is None:
        raise RuntimeError("parallel test database name is outside the guarded namespace")

    source_url = os.environ.get(SOURCE_DATABASE_URL_ENV) or _configured_database_url()
    parsed = _local_postgres_url(source_url)
    template = os.environ.get(TEMPLATE_ENV, "")
    if template and template != f"{DATABASE_PREFIX}{run_id}_tmpl":
        raise RuntimeError("parallel test template belongs to a different run")
    coordination_name = os.environ.get(COORDINATION_ENV)
    coordination = Path(coordination_name) if coordination_name else None
    if coordination is not None and (not template or not coordination.is_dir()):
        raise RuntimeError("parallel test template coordination is malformed")

    os.environ[SOURCE_DATABASE_URL_ENV] = source_url
    os.environ["DATABASE_URL"] = parsed.set(database=database_name).render_as_string(
        hide_password=False
    )
    state = LazyWorkerDatabase(parsed, database_name, template, coordination)
    config._corridor_pytest_database = state
    # SQLAlchemy's dialect event runs immediately before DBAPI.connect and
    # reaches engines constructed later by both owner and capability sessions.
    event.listen(Engine, "do_connect", state.on_connect)


def pytest_unconfigure(config) -> None:
    identity_guard = getattr(config, "_corridor_migration_identity_guard", None)
    if identity_guard is not None:
        event.remove(Engine, "do_connect", identity_guard)
        return
    state = getattr(config, "_corridor_pytest_database", None)
    if state is not None:
        event.remove(Engine, "do_connect", state.on_connect)
        if state.cleanup_needed:
            corridor_db = sys.modules.get("corridor.db")
            if corridor_db is not None:
                for name in ("engine", "web_engine", "worker_engine"):
                    getattr(corridor_db, name).dispose()
            _drop_databases(state.admin_url, (state.database_name,))
        return

    controller = getattr(config, "_corridor_pytest_controller", None)
    if controller is None:
        return
    source_url, run_id, coordination = controller
    try:
        # Pure suites never create this marker and must not query PostgreSQL
        # merely because the pytest controller is shutting down.
        if (Path(coordination.name) / "database-started").exists():
            parsed = _validated_admin_url(source_url)
            _drop_databases(parsed, _run_database_names(parsed, run_id))
    finally:
        coordination.cleanup()
        for name in (RUN_ID_ENV, TEMPLATE_ENV, COORDINATION_ENV, SOURCE_DATABASE_URL_ENV):
            os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def isolated_content_store(tmp_path, monkeypatch):
    """Every test writes its content-addressed objects under its own tmp_path.

    Renders, token layers, and raw-OCR receipts are persisted through the
    store (#487), so without this a database test would put its artifacts
    into the developer's real `corpus/files`. A test that sets
    `settings.corpus_store` itself still wins: its own fixture runs later.
    """

    from corridor.config import settings

    monkeypatch.setattr(settings, "storage_backend", "filesystem")
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "content-store"))


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


def _local_postgres_url(database_url: str) -> URL:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        raise RuntimeError("parallel tests require PostgreSQL")
    if parsed.host not in {None, "localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("parallel tests require local PostgreSQL")
    return parsed


def _validated_admin_url(database_url: str) -> URL:
    parsed = _local_postgres_url(database_url)
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
