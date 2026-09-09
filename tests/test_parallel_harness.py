"""Public test-harness contract for xdist worker database isolation."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
from pathlib import Path
import re
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url

import conftest as harness
from corridor.db import engine


SOURCE_URL = "postgresql+psycopg://corridor:corridor@localhost:1/source_database"
RUN_ID = "101_deadbeef"
TEMPLATE = f"corridor_pytest_{RUN_ID}_tmpl"


def test_migration_cases_keep_the_source_identity_read_only_without_worker_databases(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", SOURCE_URL)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(harness, "_validated_admin_url", lambda *_: pytest.fail("configuration connected to PostgreSQL"))
    config = SimpleNamespace(args=[str(harness.ROOT / "tests/test_migration_baseline.py")], option=SimpleNamespace(markexpr="migration", keyword="", collectonly=False))
    harness.pytest_configure(config)
    try:
        assert os.environ["DATABASE_URL"] == SOURCE_URL
        assert not hasattr(config, "_corridor_pytest_controller")
        assert not hasattr(config, "_corridor_pytest_database")
        callback = config._corridor_migration_identity_guard
        source_parameters = {"dbname": "source_database", "options": "-c jit=off"}
        callback(SimpleNamespace(name="postgresql"), None, [], source_parameters)
        assert source_parameters["options"].endswith("-c default_transaction_read_only=on")
        for database in ("postgres", "corridor_baseline_fresh_123"):
            parameters = {"dbname": database}
            callback(SimpleNamespace(name="postgresql"), None, [], parameters)
            assert parameters == {"dbname": database}
    finally:
        harness.pytest_unconfigure(config)
    assert not event.contains(Engine, "do_connect", callback)


def test_migration_database_ownership_does_not_exempt_a_broader_selection():
    owner = str(harness.ROOT / "tests/test_migration_baseline.py")
    for args, mark in [([str(harness.ROOT / "tests")], "migration"), ([owner, "tests/test_facts.py"], "migration"), ([owner], "not slow")]:
        config = SimpleNamespace(args=args, option=SimpleNamespace(markexpr=mark))
        assert not harness._migration_cases_own_their_databases(config)


def _controller(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("DATABASE_URL", SOURCE_URL)
    monkeypatch.setattr(harness, "new_worker_run_id", lambda: RUN_ID)
    for name in (harness.RUN_ID_ENV, harness.TEMPLATE_ENV, harness.COORDINATION_ENV, harness.SOURCE_DATABASE_URL_ENV):
        monkeypatch.delenv(name, raising=False)
    config = SimpleNamespace(
        args=[str(Path(__file__))], getoption=lambda _name: 2,
    )
    harness.pytest_configure(config)
    return config


def _record_provisioning(monkeypatch):
    calls = []

    def validate(url):
        calls.append(("validate", make_url(url).database))
        return make_url(url)

    monkeypatch.setattr(harness, "_validated_admin_url", validate)
    monkeypatch.setattr(harness, "reap_abandoned_worker_databases", lambda _url: calls.append(("reap",)))
    monkeypatch.setattr(harness, "_create_database", lambda _url, name: calls.append(("create", name)))
    monkeypatch.setattr(harness, "_clone_database", lambda _url, template, name: calls.append(("clone", template, name)))
    monkeypatch.setattr(harness, "_migrate_database", lambda url: calls.append(("migrate", url.database)))
    monkeypatch.setattr(harness, "_drop_databases", lambda _url, names: calls.append(("drop", names)))
    return calls


def test_pure_controller_and_worker_configuration_and_cleanup_never_connect(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("pure tests contacted PostgreSQL")

    monkeypatch.setattr(harness, "_validated_admin_url", forbidden)
    monkeypatch.setattr(harness, "_worker_database_names", forbidden)
    monkeypatch.setattr(harness, "_drop_databases", forbidden)
    controller = _controller(monkeypatch)
    directory = Path(os.environ[harness.COORDINATION_ENV])
    worker = SimpleNamespace()
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    try:
        harness.pytest_configure(worker)
        state = worker._corridor_pytest_database
        assert state.admin_url == make_url(SOURCE_URL)
        assert make_url(os.environ["DATABASE_URL"]).database == f"corridor_pytest_{RUN_ID}_gw0"
        assert os.environ[harness.SOURCE_DATABASE_URL_ENV] == SOURCE_URL
        assert not state.provisioned
        assert not (directory / "database-started").exists()
        assert event.contains(Engine, "do_connect", state.on_connect)
    finally:
        if hasattr(worker, "_corridor_pytest_database"):
            harness.pytest_unconfigure(worker)
        harness.pytest_unconfigure(controller)
    assert not event.contains(Engine, "do_connect", state.on_connect)
    assert not directory.exists()


def test_the_first_dbapi_connection_provisions_once_and_later_connections_reuse_it(tmp_path, monkeypatch):
    calls = _record_provisioning(monkeypatch)
    state = harness.LazyWorkerDatabase(
        make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw0", TEMPLATE, tmp_path
    )
    worker_engine = create_engine(state.admin_url.set(database=state.database_name))

    class StopBeforeDBAPI(Exception):
        pass

    def stop(*_args, **_kwargs):
        assert state.provisioned
        raise StopBeforeDBAPI

    monkeypatch.setattr(worker_engine.dialect, "connect", stop)
    event.listen(Engine, "do_connect", state.on_connect)
    try:
        assert calls == []
        for _ in range(2):
            with pytest.raises(StopBeforeDBAPI):
                worker_engine.connect()
    finally:
        event.remove(Engine, "do_connect", state.on_connect)
        worker_engine.dispose()
    assert calls == [
        ("validate", "source_database"), ("reap",), ("create", TEMPLATE),
        ("migrate", TEMPLATE), ("clone", TEMPLATE, state.database_name),
    ]
    assert (tmp_path / "database-started").exists()
    assert (tmp_path / "template-ready").exists()


@pytest.mark.parametrize("database", ["postgres", "source_database", "corridor_due_work_test_fixture"])
def test_admin_and_independent_runtime_databases_do_not_provision_the_default_worker(tmp_path, monkeypatch, database):
    calls = _record_provisioning(monkeypatch)
    state = harness.LazyWorkerDatabase(
        make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw0", TEMPLATE, tmp_path
    )
    state.on_connect(SimpleNamespace(name="postgresql"), None, [], {"dbname": database})
    assert calls == []
    assert not state.cleanup_needed
    assert list(tmp_path.iterdir()) == []


def test_workers_wait_for_the_shared_lock_then_migrate_one_template(tmp_path, monkeypatch):
    calls = _record_provisioning(monkeypatch)
    attempted = Event()
    flock = fcntl.flock

    def observed_lock(file, operation):
        if operation == fcntl.LOCK_EX:
            attempted.set()
        return flock(file, operation)

    monkeypatch.setattr(harness.fcntl, "flock", observed_lock)
    first = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw0", TEMPLATE, tmp_path)
    second = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw1", TEMPLATE, tmp_path)
    with (tmp_path / "database.lock").open("a+b") as held:
        flock(held, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(first.ensure_provisioned)
            try:
                assert attempted.wait(2), "worker never attempted to acquire its lock"
                assert calls == [], "a worker provisioned before acquiring the shared lock"
            finally:
                flock(held, fcntl.LOCK_UN)
            waiting.result(timeout=2)
    second.ensure_provisioned()
    assert [call for call in calls if call[0] == "migrate"] == [("migrate", TEMPLATE)]
    assert [call for call in calls if call[0] == "clone"] == [
        ("clone", TEMPLATE, first.database_name), ("clone", TEMPLATE, second.database_name),
    ]


def test_failed_template_migration_never_publishes_readiness_or_clones_a_worker(tmp_path, monkeypatch):
    calls = _record_provisioning(monkeypatch)
    state = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw0", TEMPLATE, tmp_path)

    def fail(_url):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(harness, "_migrate_database", fail)
    with pytest.raises(RuntimeError, match="migration failed"):
        state.ensure_provisioned()
    assert not state.provisioned
    assert not (tmp_path / "template-ready").exists()
    assert not any(call[0] == "clone" for call in calls)
    assert calls[-1] == ("drop", (TEMPLATE,))
    before_retry = list(calls)
    with pytest.raises(RuntimeError, match="previously failed"):
        state.ensure_provisioned()
    assert calls == before_retry
    sibling = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw1", TEMPLATE, tmp_path)
    with pytest.raises(RuntimeError, match="failed in another worker"):
        sibling.ensure_provisioned()
    assert [call for call in calls if call[0] == "create"] == [("create", TEMPLATE)]


def test_a_worker_without_a_shared_template_migrates_only_when_connected(monkeypatch):
    calls = _record_provisioning(monkeypatch)
    state = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw0")
    assert calls == []
    state.ensure_provisioned()
    assert calls == [
        ("validate", "source_database"), ("create", state.database_name),
        ("migrate", state.database_name),
    ]


def test_controller_cleanup_uses_the_captured_source_url_and_only_its_run(monkeypatch):
    calls = _record_provisioning(monkeypatch)
    controller = _controller(monkeypatch)
    directory = Path(os.environ[harness.COORDINATION_ENV])
    (directory / "database-started").touch()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost:2/changed_database")
    monkeypatch.setattr(harness, "_run_database_names", lambda url, run_id: (TEMPLATE,) if url == make_url(SOURCE_URL) and run_id == RUN_ID else pytest.fail("cleanup used a rewritten URL or another run"))
    harness.pytest_unconfigure(controller)
    assert calls == [("validate", "source_database"), ("drop", (TEMPLATE,))]
    assert not directory.exists()


@pytest.mark.parametrize("url", ["sqlite:///test.db", "postgresql+psycopg://external.example/test"])
def test_lazy_configuration_preserves_the_local_postgres_refusal(url):
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        harness._local_postgres_url(url)


@pytest.fixture
def broad_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv(harness.LOCAL_BROAD_REASON_ENV, raising=False)
    directory = tmp_path / "tests"
    directory.mkdir()
    for name in ("test_one.py", "test_two.py"):
        (directory / name).touch()

    def config(args=None, *, keyword="", collectonly=False):
        return SimpleNamespace(
            args=["tests"] if args is None else args,
            option=SimpleNamespace(keyword=keyword, collectonly=collectonly),
            invocation_params=SimpleNamespace(dir=tmp_path),
            getoption=lambda _name: 4,
        )

    return config


@pytest.mark.parametrize("arguments", [
    None, [], ["tests"], ["."], ["tests/test_one.py", "tests/test_two.py"],
])
def test_broad_local_guard_refuses_default_directories_and_complete_file_lists_before_setup(
    broad_guard, monkeypatch, arguments,
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("a refused broad run reached controller or database setup")

    for name in ("_configured_database_url", "new_worker_run_id", "_validated_admin_url", "_drop_databases"):
        monkeypatch.setattr(harness, name, forbidden)
    config = broad_guard(arguments)
    with pytest.raises(pytest.UsageError, match="make test-focused.*CI owns"):
        harness.pytest_configure(config)
    assert not hasattr(config, "_corridor_pytest_controller")
    harness.pytest_unconfigure(config)


def test_broad_local_guard_normalizes_absolute_and_relative_paths(broad_guard):
    config = broad_guard([str(harness.ROOT / "tests/../tests")])
    with pytest.raises(pytest.UsageError, match="diagnostic reason"):
        harness._require_local_broad_reason(config)
    config.args = [str(path) for path in (harness.ROOT / "tests").glob("test_*.py")]
    with pytest.raises(pytest.UsageError, match="diagnostic reason"):
        harness._require_local_broad_reason(config)


@pytest.mark.parametrize("arguments", [
    ["tests/test_one.py"], ["tests/test_one.py::test_specific"],
    ["tests/test_one.py::test_specific", "tests/test_two.py::test_other"],
])
def test_bounded_explicit_files_and_nodes_need_no_broad_reason(broad_guard, arguments):
    harness._require_local_broad_reason(broad_guard(arguments))


@pytest.mark.parametrize("keyword,allowed", [
    ("", False), ("   ", False), ("test", False), ("test_", False),
    ("not legacy", False), ("specific or not legacy", False),
    ("test or specific", False), ("test and not legacy", False),
    ("specific", True), ("specific and not legacy", True),
    ("specific or another_specific", True),
    ("(specific and not legacy) or another-specific", True),
])
def test_keyword_focus_requires_a_specific_positive_constraint(broad_guard, keyword, allowed):
    config = broad_guard(keyword=keyword)
    if allowed:
        harness._require_local_broad_reason(config)
    else:
        with pytest.raises(pytest.UsageError):
            harness._require_local_broad_reason(config)


@pytest.mark.parametrize("reason", ["failure-reproduction", "performance-investigation"])
def test_explicit_diagnostic_reasons_allow_broad_local_execution(broad_guard, monkeypatch, reason):
    monkeypatch.setenv(harness.LOCAL_BROAD_REASON_ENV, reason)
    harness._require_local_broad_reason(broad_guard())


@pytest.mark.parametrize("reason", ["", "true", "validation", "performance"])
def test_other_reason_values_do_not_disable_the_broad_guard(broad_guard, monkeypatch, reason):
    monkeypatch.setenv(harness.LOCAL_BROAD_REASON_ENV, reason)
    with pytest.raises(pytest.UsageError):
        harness._require_local_broad_reason(broad_guard())


def test_engine_absent_acceptance_cannot_waive_the_guard_in_the_default_environment(broad_guard, monkeypatch):
    config = broad_guard()
    config.option.engine_absent_proof = True
    # The test itself also runs in the engine-absent acceptance. Simulate a
    # normal interpreter to prove the flag cannot grant that environment entry.
    monkeypatch.setattr(harness.sys, "prefix", str(harness.ROOT / ".venv"))
    with pytest.raises(pytest.UsageError, match="prepared isolated environment"):
        harness._require_local_broad_reason(config)


def test_collect_only_and_github_ci_keep_complete_collection(broad_guard, monkeypatch):
    harness._require_local_broad_reason(broad_guard(collectonly=True))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    harness._require_local_broad_reason(broad_guard())


def test_worker_run_identity_carries_the_controller_process():
    run_id = harness.new_worker_run_id()

    assert re.fullmatch(rf"{os.getpid()}_[0-9a-f]{{8}}", run_id)


def test_abandoned_worker_reclamation_keeps_live_process_databases(monkeypatch):
    dead = "corridor_pytest_101_deadbeef_gw0"
    live = "corridor_pytest_202_feedface_gw1"
    legacy = "corridor_pytest_0123456789ab_gw0"
    dropped = []
    monkeypatch.setattr(
        harness,
        "_worker_database_names",
        lambda _admin_url: (dead, live, legacy),
    )
    monkeypatch.setattr(harness, "_process_is_running", lambda pid: pid == 202)
    monkeypatch.setattr(
        harness,
        "_drop_databases",
        lambda _admin_url, names: dropped.extend(names),
    )

    harness.reap_abandoned_worker_databases(object())

    assert dropped == [dead]


def test_run_cleanup_selects_only_its_own_worker_databases(monkeypatch):
    run_id = "101_deadbeef"
    own = f"corridor_pytest_{run_id}_gw0"
    sibling = "corridor_pytest_202_feedface_gw0"
    monkeypatch.setattr(
        harness,
        "_worker_database_names",
        lambda _admin_url: (own, sibling),
    )

    assert harness._run_database_names(object(), run_id) == (own,)


def test_xdist_worker_uses_a_guarded_isolated_database(worker_id):
    with engine.connect() as connection:
        database_name = str(connection.scalar(text("select current_database()")))

    if worker_id == "master":
        assert database_name
        return
    assert re.fullmatch(
        rf"corridor_pytest_[1-9][0-9]*_[0-9a-f]{{8}}_{re.escape(worker_id)}",
        database_name,
    )


def test_shared_source_and_workers_coordinate_one_migration_and_distinct_clones(tmp_path, monkeypatch):
    calls = _record_provisioning(monkeypatch)
    first = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw0", TEMPLATE, tmp_path)
    second = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw1", TEMPLATE, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        source = executor.submit(first.shared_empty_source_url)
        worker = executor.submit(second.ensure_provisioned)
        source_url = source.result(timeout=2)
        worker.result(timeout=2)
    assert second.shared_empty_source_url() == source_url
    name = f"corridor_pytest_{RUN_ID}_source"
    assert make_url(source_url).database == name
    assert [c for c in calls if c[0] == "migrate"] == [("migrate", TEMPLATE)]
    assert sorted(c for c in calls if c[0] == "clone") == sorted([
        ("clone", TEMPLATE, name), ("clone", TEMPLATE, second.database_name),
    ])
    assert not first.provisioned
    assert not first.cleanup_needed
    assert (tmp_path / "shared-source-ready").exists()


def test_template_can_be_prepared_without_worker_population(tmp_path, monkeypatch):
    calls = _record_provisioning(monkeypatch)
    state = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw0", TEMPLATE, tmp_path)
    assert state.ensure_template() == TEMPLATE
    state.shared_empty_source_url()
    assert [c for c in calls if c[0] == "migrate"] == [("migrate", TEMPLATE)]
    assert [c for c in calls if c[0] == "clone"] == [("clone", TEMPLATE, f"corridor_pytest_{RUN_ID}_source")]
    assert not state.provisioned


def test_shared_source_clone_failure_is_not_retried_or_published(tmp_path, monkeypatch):
    calls = _record_provisioning(monkeypatch)
    state = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw0", TEMPLATE, tmp_path)
    state.ensure_template()
    def fail(*_args):
        raise RuntimeError("clone failed")
    monkeypatch.setattr(harness, "_clone_database", fail)
    with pytest.raises(RuntimeError, match="clone failed"):
        state.shared_empty_source_url()
    assert not (tmp_path / "shared-source-ready").exists()
    assert (tmp_path / "shared-source-failed").exists()
    assert calls[-1] == ("drop", (f"corridor_pytest_{RUN_ID}_source",))
    sibling = harness.LazyWorkerDatabase(make_url(SOURCE_URL), f"corridor_pytest_{RUN_ID}_gw1", TEMPLATE, tmp_path)
    with pytest.raises(RuntimeError, match="shared source provisioning failed"):
        sibling.shared_empty_source_url()


@pytest.mark.parametrize("github,marked", [("false", "1"), ("true", ""), ("false", "")])
def test_shared_source_without_disposable_ci_opt_in_preserves_original_url(monkeypatch, github, marked):
    monkeypatch.setenv(harness.SOURCE_DATABASE_URL_ENV, SOURCE_URL)
    monkeypatch.setenv("GITHUB_ACTIONS", github)
    monkeypatch.setenv(harness.EMPTY_CI_SOURCE_ENV, marked)
    monkeypatch.setattr(harness, "_configured_source_has_relations", lambda *_: pytest.fail("unexpected source inspection"))
    assert harness._shared_source_url(SimpleNamespace()) == SOURCE_URL


@pytest.mark.parametrize("relations", [True, False])
def test_only_relation_free_explicit_ci_source_gets_a_clone(monkeypatch, relations):
    source = harness.DEFAULT_DATABASE_URL
    monkeypatch.setenv(harness.SOURCE_DATABASE_URL_ENV, source)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv(harness.EMPTY_CI_SOURCE_ENV, "1")
    monkeypatch.setattr(harness, "_configured_source_has_relations", lambda url: relations if url == source else pytest.fail("wrong source"))
    state = SimpleNamespace(admin_url=make_url(source), shared_empty_source_url=lambda: "empty-template-clone")
    config = SimpleNamespace(_corridor_pytest_database=state)
    assert harness._shared_source_url(config) == (source if relations else "empty-template-clone")
    if relations:
        # Existing populated or partial schemas are never hidden, even without
        # a usable harness. Their own corpus reads remain the source of truth.
        assert harness._shared_source_url(SimpleNamespace()) == source
    else:
        with pytest.raises(RuntimeError, match="coordinated xdist"):
            harness._shared_source_url(SimpleNamespace())
        state.admin_url = make_url(SOURCE_URL)
        with pytest.raises(RuntimeError, match="controller's configured database"):
            harness._shared_source_url(config)


def test_disposable_marker_cannot_replace_another_configured_source(monkeypatch):
    monkeypatch.setenv(harness.SOURCE_DATABASE_URL_ENV, SOURCE_URL)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv(harness.EMPTY_CI_SOURCE_ENV, "1")
    monkeypatch.setattr(harness, "_configured_source_has_relations", lambda *_: pytest.fail("another configured source was inspected"))
    assert harness._shared_source_url(SimpleNamespace()) == SOURCE_URL


def test_controller_cleanup_includes_shared_source_but_not_configured_database(monkeypatch):
    names = (f"corridor_pytest_{RUN_ID}_source", TEMPLATE, f"corridor_pytest_{RUN_ID}_gw1")
    monkeypatch.setattr(harness, "_worker_database_names", lambda _: (*names, "corridor", "corridor_pytest_202_feedface_source"))
    assert harness._run_database_names(object(), RUN_ID) == names
    dropped = []
    monkeypatch.setattr(harness, "_process_is_running", lambda pid: pid == 202)
    monkeypatch.setattr(harness, "_drop_databases", lambda _, rows: dropped.extend(rows))
    harness.reap_abandoned_worker_databases(object())
    assert dropped == list(names)


def test_empty_shared_source_has_schema_but_never_inherits_worker_rows(request):
    state = getattr(request.config, "_corridor_pytest_database", None)
    if state is None:
        pytest.skip("worker/source isolation requires the xdist harness")
    shared = create_engine(state.shared_empty_source_url())
    try:
        with engine.connect() as worker, worker.begin():
            worker.execute(text("insert into projects(slug,name,is_synthetic) values ('shared-source-isolation','Worker only',true)"))
            with shared.connect() as source:
                assert source.scalar(text("select to_regclass('public.projects')")) is not None
                assert source.scalar(text("select count(*) from projects where slug='shared-source-isolation'")) == 0
                assert source.scalar(text("select current_database()")) != state.database_name
                assert harness._configured_source_has_relations(state.shared_empty_source_url())
            worker.rollback()
    finally:
        shared.dispose()
