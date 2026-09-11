"""Parallel test-process isolation over the real PostgreSQL stack.

Ordinary tests share the rollback-scoped ``session`` fixture defined here.
This harness only changes which migrated database each xdist worker reaches, so
schema rehearsals and fixed fixture identities cannot deadlock across workers.
Provisioning starts at the first real connection to a worker database; pure
tests never contact PostgreSQL, even when xdist is enabled. Serial pytest
runs continue to use the configured development database. The shared-corpus
fixture also preserves that database; only an explicitly disposable empty CI
source uses a separate empty clone of the same migrated template.

That template is also the one schema every isolated-database fixture copies,
through `provision_isolated_database`. Alembic therefore runs once per run
rather than once per fixture that wanted a database of its own.
"""

from __future__ import annotations

import ast
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from threading import Lock, get_ident
import time
from uuid import uuid4

from dotenv import dotenv_values
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.orm import Session as SessionType
from sqlalchemy.pool import NullPool

from scripts.test_gate.broad_run import (
    DIAGNOSTIC_ENV, DIAGNOSTIC_REASONS, authorized, selects_whole_suite,
)


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
    r"^corridor_pytest_([1-9][0-9]*)_[0-9a-f]{8}_(gw[0-9]+|tmpl|source)$"
)
TEMPLATE_ENV = "CORRIDOR_PYTEST_TEMPLATE"
COORDINATION_ENV = "CORRIDOR_PYTEST_COORDINATION"
EMPTY_CI_SOURCE_ENV = "CORRIDOR_CI_EMPTY_SHARED_SOURCE"


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
    if getattr(options, "engine_absent_proof", False):
        # #766 explicitly requires the complete suite after proving both
        # engines absent. This dedicated acceptance is not a routine broad
        # run: the flag alone cannot waive the default-environment guard.
        from scripts.engine_absent_suite import (
            ENVIRONMENT_NAME, REPO_ROOT, assert_absent, probe_absence,
        )
        if Path(sys.prefix).resolve() != (REPO_ROOT / ENVIRONMENT_NAME).resolve():
            raise pytest.UsageError("engine-absent proof requires its prepared isolated environment")
        for project in (REPO_ROOT, REPO_ROOT / "workers/render"):
            assert_absent(probe_absence(dict(os.environ), project), str(project))
        return
    if getattr(options, "collectonly", False) or os.environ.get("GITHUB_ACTIONS") == "true":
        return
    if authorized(os.environ):
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
        invocation_dir / str(argument)
        for argument in arguments if "::" not in str(argument)
    }
    if selects_whole_suite(paths, test_root):
        raise pytest.UsageError(
            "Broad local tests require a concrete diagnostic reason. "
            "Use make test-focused ARGS='tests/test_file.py::test_name'; "
            "required CI owns the complete release proof. For an actual diagnostic, "
            f"set {DIAGNOSTIC_ENV}={DIAGNOSTIC_REASONS[0]} or "
            f"{DIAGNOSTIC_ENV}={DIAGNOSTIC_REASONS[1]}."
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
    _provisioning_thread: int | None = field(default=None, repr=False)

    def on_connect(self, dialect, connection_record, args, parameters) -> None:
        # Admin queries and runtime_database's independent scratch databases
        # must never recurse into the default worker's provisioning path.
        if dialect.name == "postgresql" and parameters.get("dbname") == self.database_name:
            self.ensure_provisioned()

    def ensure_provisioned(self) -> None:
        if self._provisioning_thread == get_ident():
            # The migration this provisioning is running opened a connection
            # to the very database it is provisioning. Blocking on our own
            # lock here is the silent hang this refusal replaces.
            raise RuntimeError(
                "parallel test worker database provisioning re-entered itself: "
                "the harness migration must not connect to the worker database"
            )
        with self._provision_lock:
            if self.provisioned:
                return
            if self.failed:
                raise RuntimeError("parallel test worker database provisioning previously failed")
            self._provisioning_thread = get_ident()
            try:
                if self.coordination is None:
                    self._provision()
                    return
                # Share one template across processes. Readiness is published
                # only after migration succeeds, while the file lock is held.
                with self._database_lock():
                    self._provision()
            except BaseException:
                self.failed = True
                raise
            finally:
                self._provisioning_thread = None

    @contextmanager
    def _database_lock(self):
        if self.coordination is None:
            raise RuntimeError("shared test databases require controller coordination")
        with (self.coordination / "database.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _ensure_template(self, parsed: URL) -> str:
        """Publish the migrated template once, while the shared lock is held."""
        if self.coordination is None or not self.template:
            raise RuntimeError("shared test databases require a controller template")
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
                failed.touch()
                _drop_databases(parsed, (self.template,))
                raise
            ready.touch()
        return self.template

    def ensure_template(self) -> str:
        """Provision the empty schema without creating or reusing a worker DB."""
        with self._provision_lock, self._database_lock():
            parsed = _validated_admin_url(self.admin_url.render_as_string(hide_password=False))
            return self._ensure_template(parsed)

    def shared_empty_source_url(self) -> str:
        """Share one empty clone, never a worker's mutable fixture population."""
        with self._provision_lock, self._database_lock():
            parsed = _validated_admin_url(self.admin_url.render_as_string(hide_password=False))
            template = self._ensure_template(parsed)
            name = template.removesuffix("_tmpl") + "_source"
            ready = self.coordination / "shared-source-ready"
            failed = self.coordination / "shared-source-failed"
            if failed.exists():
                raise RuntimeError("shared source provisioning failed in another worker")
            if not ready.exists():
                try:
                    _clone_database(parsed, template, name)
                except BaseException:
                    failed.touch()
                    _drop_databases(parsed, (name,))
                    raise
                ready.touch()
            return parsed.set(database=name).render_as_string(hide_password=False)

    def _provision(self) -> None:
        parsed = _validated_admin_url(self.admin_url.render_as_string(hide_password=False))
        if self.coordination is not None:
            self._ensure_template(parsed)
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


def pytest_addoption(parser) -> None:
    parser.addoption("--engine-absent-proof", action="store_true", default=False,
                     help="complete retirement acceptance in the verified engine-absent environment")


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
    # Bind the real migration now, before any test runs: a test that
    # monkeypatches the application module's ``apply_schema_migrations`` must
    # not be able to redirect the harness's own worker-database provisioning
    # through its fake, which is how one such test deadlocked on this lock.
    _harness_migration()
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
                # Only the capability engines a test actually asked for exist;
                # reaching for the other names here would build a pool solely
                # in order to dispose of it.
                corridor_db.dispose_engines()
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


def _configured_source_has_relations(source_url: str) -> bool:
    """Observe existing shared state without changing the configured database.

    Any user relation, including a partial schema, preserves the original source.
    A broken corpus must fail its reader, not disappear behind an empty clone.
    """
    engine = create_engine(source_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return bool(connection.scalar(text("""
                select exists (
                    select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace
                    where n.nspname not in ('pg_catalog', 'information_schema')
                      and n.nspname not like 'pg_toast%'
                      and c.relkind in ('r', 'p', 'v', 'm', 'S', 'f')
                )
            """)))
    finally:
        engine.dispose()


def _shared_source_url(config) -> str:
    source = os.environ.get(SOURCE_DATABASE_URL_ENV) or _configured_database_url()
    # Only the explicitly marked database created by ci_postgres.sh may use an
    # empty clone. Local and externally prepared corpora keep their original URL.
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get(EMPTY_CI_SOURCE_ENV) != "1":
        return source
    parsed = _local_postgres_url(source)
    if parsed.database != "corridor" or parsed.port != 5433:
        return source
    if _configured_source_has_relations(source):
        return source
    state = getattr(config, "_corridor_pytest_database", None)
    if state is None:
        raise RuntimeError("empty CI shared source requires the coordinated xdist harness")
    if state.admin_url != parsed:
        raise RuntimeError("shared source differs from the controller's configured database")
    return state.shared_empty_source_url()


@pytest.fixture(scope="session")
def shared_source_database_url(request) -> str:
    """Configured corpus, or a schema-only CI clone when no corpus exists."""
    return _shared_source_url(request.config)


@pytest.fixture
def session():
    """One rollback-scoped Session over this worker's migrated database.

    Every database test used to declare this same fixture, and the copies had
    drifted: some rolled back unconditionally, some guarded on
    ``transaction.is_active``, three had a ``finally`` and the rest did not.
    The engine is resolved through the capability accessor at request time
    rather than imported at module import, because ``pytest_configure``
    rewrites ``DATABASE_URL`` and the worker database is provisioned on the
    first real connection: a module that resolved the engine while it was being
    imported would fix the wrong URL. This module imports nothing
    from ``corridor`` at import time for the same reason -- ``corridor.config``
    reads the environment when it is imported, and this file is imported before
    ``pytest_configure`` rewrites ``DATABASE_URL``.
    """

    with rollback_scoped_session() as scoped:
        yield scoped


@contextmanager
def rollback_scoped_session():
    """One transaction on the owner engine that always rolls back on exit.

    The ``session`` fixture is this and nothing more; a test that must prove
    the rollback itself, rather than rely on it, opens the same seam directly.
    """

    from corridor.db import capability_engine

    connection = capability_engine("owner").connect()
    transaction = connection.begin()
    scoped = SessionType(bind=connection)
    try:
        yield scoped
    finally:
        scoped.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


def synthetic_project(session):
    """Flush one synthetic Project with a fresh slug into the given session."""

    from corridor.models import Project

    row = Project(slug=f"project-{uuid4().hex[:8]}", name="Project", is_synthetic=True)
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def project(session):
    """One synthetic Project, flushed inside the test's own transaction.

    The slug carries a fresh suffix so a module that commits a scenario cannot
    collide with one that rolls back. A test that asserts on a project's own
    slug or display name still declares its own fixture: this one says only
    that a project exists.
    """

    return synthetic_project(session)


def _harness_migrated_template(config) -> str | None:
    """The schema this run has already migrated, for a fixture to copy.

    The coordinated harness publishes exactly one migrated template per run,
    behind the cross-process file lock, and every worker database is a copy of
    it. Handing that same template to the disposable-database provisioner is
    what keeps one Alembic subprocess per *run* from becoming one per process
    and one more per isolated-database fixture. Without the coordinated harness
    -- a serial pytest process, or the migration cases that own their own
    databases -- there is nothing published yet, and `m8_acceptance_database`
    migrates one template for the process instead.
    """

    state = getattr(config, "_corridor_pytest_database", None)
    if state is None or state.coordination is None or not state.template:
        return None
    return state.ensure_template()


@pytest.fixture(scope="session")
def provision_isolated_database(request):
    """Provision one disposable database carrying this run's migrated schema.

    Every isolated-database fixture used to call
    `provision_disposable_postgres` itself and take the replay-the-chain
    default it had then, so an Alembic subprocess ran once per fixture for a
    schema the run had already built -- measured at 3.0s against 0.13s for the
    copy. The harness is the one that knows a migrated template exists, so the
    template lives here and each fixture asks this seam for a copy of it.
    """

    template = _harness_migrated_template(request.config)

    @contextmanager
    def provision(label: str):
        from corridor.config import settings
        from corridor import m8_acceptance_database

        with m8_acceptance_database.provision_disposable_postgres(
            settings.database_url,
            repo_root=ROOT,
            label=label,
            template_database=template,
        ) as database:
            yield database

    return provision


@pytest.fixture
def runtime_database(provision_isolated_database):
    """Harness-owned migrated database for real competing Due Work transactions."""

    with provision_isolated_database("due_work_test") as database:
        yield database


@pytest.fixture(scope="module")
def customer_environment_databases(provision_isolated_database):
    """Two customer databases and an independently removable control-plane DB.

    Only this harness provisions databases. Exiting a customer context performs
    the actual DROP DATABASE used by the external-custody proof (#656).
    """
    source = _configured_database_url()
    admin = _validated_admin_url(source).set(database="postgres")
    name = f"{DATABASE_PREFIX}{new_worker_run_id()}_gw0"
    _create_database(admin, name)
    control_engine = create_engine(admin.set(database=name), poolclass=NullPool)

    @contextmanager
    def customer():
        with provision_isolated_database("customer_test") as database:
            yield database

    try:
        with ExitStack() as stack:
            yield control_engine, stack.enter_context(customer()), customer
    finally:
        control_engine.dispose()
        _drop_databases(admin, (name,))


@pytest.fixture(scope="module")
def control_plane_capabilities(customer_environment_databases):
    """Real scoped logins, isolated from the shared customer runtime roles."""
    from corridor.control_plane_schema import initialize_control_plane

    owner, _, _ = customer_environment_databases
    initialize_control_plane(owner)
    names = ["corridor_cp_test_" + uuid4().hex[:12] for _ in range(2)]
    engines = []
    try:
        for name, role in zip(names, ("corridor_control_operations", "corridor_control_resolver")):
            with owner.begin() as connection:
                connection.execute(text(f"create role {name} login password 'control-proof-password' nosuperuser nocreatedb nocreaterole nobypassrls"))
                connection.execute(text(f"grant {role} to {name}"))
            engines.append(create_engine(owner.url.set(username=name, password="control-proof-password"), poolclass=NullPool, hide_parameters=True))
        yield tuple(engines)
    finally:
        for engine in engines:
            engine.dispose()
        with owner.begin() as connection:
            for name in names:
                connection.execute(text(f"drop role if exists {name}"))


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


_HARNESS_MIGRATION = None


def _harness_migration():
    """The migration the harness runs, bound once and immune to monkeypatching.

    `pytest_configure` binds it in every worker before a test can run. Reading
    ``corridor.m8_acceptance_database.apply_schema_migrations`` at call time
    instead let a test's monkeypatch of that attribute run inside the worker's
    own provisioning, where its first connection re-entered the provisioning
    lock and hung the worker.
    """

    global _HARNESS_MIGRATION
    if _HARNESS_MIGRATION is None:
        from corridor.m8_acceptance_database import apply_schema_migrations

        _HARNESS_MIGRATION = apply_schema_migrations
    return _HARNESS_MIGRATION


def _migrate_database(database_url: URL) -> None:
    """Upgrade one worker database through the module that owns the subprocess.

    This body was a byte-for-byte copy of
    `m8_acceptance_database.apply_schema_migrations` in a file that already
    imports that module; it now calls the callable bound at configure time.
    """

    _harness_migration()(
        database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        revision="head",
    )


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


@pytest.fixture
def missing_cluster_role(runtime_database):
    """Own one initially absent global role for cross-database bootstrap races."""
    from corridor.db import engine

    role = f"corridor_bootstrap_test_{uuid4().hex}"
    independent = runtime_database.session_factory.kw["bind"]
    try:
        yield engine, independent, role
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'drop role if exists "{role}"'))
