"""Cluster role creation converges when independent database migrations race.

The configured CI database formerly created every global role serially. Fresh
baseline, runtime and worker templates can now reach a missing role together;
losing CREATE ROLE is success only after that exact role is visible. Other DDL
errors must survive, and the normal role-attribute hardening still runs.
"""

from concurrent.futures import ThreadPoolExecutor
import importlib
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError


baseline = importlib.import_module(
    "corridor.migrations.baseline_versions.a1c4e7b0d2f3_consolidated_schema_baseline"
)


class RoleError(Exception):
    def __init__(self, code, constraint=None, message="catalog role creation race"):
        super().__init__(message)
        self.sqlstate = code
        self.diag = SimpleNamespace(constraint_name=constraint)


class RoleConnection:
    def __init__(self, errors):
        self.errors = iter(errors)
        self.statements = []
        self.rollbacks = 0
        self.commits = 0

    def begin_nested(self):
        def rollback():
            self.rollbacks += 1
        def commit():
            self.commits += 1
        return SimpleNamespace(rollback=rollback, commit=commit)

    def exec_driver_sql(self, statement):
        self.statements.append(statement)
        error = next(self.errors, None)
        if error is not None:
            raise DBAPIError(statement, None, error)


@pytest.mark.parametrize("code,constraint", [("42710", None), ("23505", "pg_authid_rolname_index")])
def test_a_losing_role_create_rechecks_visibility_instead_of_repeating_create(monkeypatch, code, constraint):
    connection = RoleConnection([RoleError(code, constraint)])
    reads = []
    def visible_after_rollback(observed, role):
        assert observed is connection and connection.rollbacks == 1
        reads.append(role)
        return False
    monkeypatch.setattr(baseline, "_role_missing", visible_after_rollback)
    monkeypatch.setattr(baseline.time, "sleep", lambda _: pytest.fail("visible role must not retry CREATE"))
    baseline._apply_role_ddl(connection, "create role fixture_role nologin", creating_role="fixture_role")
    assert reads == ["fixture_role"]
    assert len(connection.statements) == 1


def test_role_collision_without_visible_winner_retries_within_the_bound(monkeypatch):
    connection = RoleConnection([RoleError("23505", "pg_authid_rolname_index"), None])
    monkeypatch.setattr(baseline, "_role_missing", lambda *_: True)
    sleeps = []
    monkeypatch.setattr(baseline.time, "sleep", sleeps.append)
    baseline._apply_role_ddl(connection, "create role fixture_role nologin", creating_role="fixture_role")
    assert connection.rollbacks == 1 and connection.commits == 1
    assert len(connection.statements) == 2 and sleeps == [.05]


@pytest.mark.parametrize("code,constraint", [("42501", None), ("23505", "unrelated_unique_index"), ("42601", None)])
def test_other_role_ddl_errors_are_never_mistaken_for_a_successful_race(monkeypatch, code, constraint):
    connection = RoleConnection([RoleError(code, constraint)])
    monkeypatch.setattr(baseline, "_role_missing", lambda *_: pytest.fail("unrelated failure checked role existence"))
    monkeypatch.setattr(baseline.time, "sleep", lambda _: pytest.fail("unrelated failure retried"))
    with pytest.raises(DBAPIError):
        baseline._apply_role_ddl(connection, "create role fixture_role nologin", creating_role="fixture_role")
    assert connection.rollbacks == 1


def test_catalog_update_retry_does_not_swallow_other_duplicate_objects(monkeypatch):
    connection = RoleConnection([RoleError("XX000", message="tuple concurrently updated"), None])
    monkeypatch.setattr(baseline.time, "sleep", lambda _: None)
    baseline._apply_role_ddl(connection, "alter role fixture_role nologin")
    assert connection.rollbacks == 1 and connection.commits == 1
    with pytest.raises(DBAPIError):
        baseline._apply_role_ddl(RoleConnection([RoleError("42710")]), "alter role fixture_role nologin")


def test_independent_database_bootstraps_converge_on_one_missing_cluster_role(missing_cluster_role):
    first_engine, second_engine, role = missing_cluster_role
    attempted = Event()
    create = f'create role "{role}" login inherit'
    def before_execute(_conn, _cursor, statement, *_args):
        if statement == create:
            attempted.set()
    event.listen(second_engine, "before_cursor_execute", before_execute)
    try:
        with first_engine.connect() as first, second_engine.connect() as second:
            assert first.scalar(text("select current_database()")) != second.scalar(text("select current_database()"))
            assert baseline._role_missing(first, role)
            assert baseline._role_missing(second, role)
            baseline._apply_role_ddl(first, create, creating_role=role)
            with ThreadPoolExecutor(max_workers=1) as executor:
                waiting = executor.submit(baseline._apply_role_ddl, second, create, creating_role=role)
                try:
                    assert attempted.wait(2), "second database never attempted CREATE ROLE"
                finally:
                    first.commit()
                waiting.result(timeout=5)
            # The losing migration must continue through its ordinary attribute
            # checks, not mistake presence for the required capability posture.
            assert baseline._role_attributes_differ(second, role, "rolcanlogin or rolinherit")
            baseline._apply_role_ddl(second, f'alter role "{role}" nologin noinherit nosuperuser nocreatedb nocreaterole nobypassrls')
            second.commit()
            assert not baseline._role_attributes_differ(first, role, "rolcanlogin or rolinherit or rolsuper or rolcreatedb or rolcreaterole or rolbypassrls")
    finally:
        event.remove(second_engine, "before_cursor_execute", before_execute)
