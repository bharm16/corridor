"""Separate control-plane databases converge on their shared PostgreSQL roles."""
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from corridor.control_plane_schema import OPERATIONS_ROLE, _ensure_control_plane_role


class RoleConnection:
    def __init__(self, observations, error=None):
        self.observations = list(observations)
        self.error = error
        self.events = []

    def scalar(self, statement, parameters):
        assert parameters == {"role": OPERATIONS_ROLE}
        assert "pg_catalog.pg_roles" in str(statement)
        self.events.append("catalog read")
        return self.observations.pop(0)

    def begin_nested(self):
        self.events.append("savepoint")
        return SimpleNamespace(rollback=lambda: self.events.append("rollback"),
                               commit=lambda: self.events.append("commit"))

    def execute(self, statement):
        assert str(statement) == f"create role {OPERATIONS_ROLE} nologin nosuperuser nocreatedb nocreaterole nobypassrls"
        self.events.append("create role")
        if self.error:
            raise self.error


def provider_error(code, constraint=None):
    original = RuntimeError("provider catalog diagnostic")
    original.sqlstate = code
    original.diag = SimpleNamespace(constraint_name=constraint)
    return DBAPIError("CREATE ROLE", None, original)


@pytest.mark.parametrize("code,constraint", [("42710", None), ("23505", "pg_authid_rolname_index")])
def test_role_creation_loser_rolls_back_and_rechecks_the_winners_capabilities(code, constraint):
    connection = RoleConnection([None, True], provider_error(code, constraint))
    _ensure_control_plane_role(connection, OPERATIONS_ROLE)
    assert connection.events == ["catalog read", "savepoint", "create role", "rollback", "catalog read"]
    assert not connection.observations


@pytest.mark.parametrize("state", [None, False])
def test_catalog_collision_is_not_success_without_the_exact_bounded_role(state):
    failure = provider_error("23505", "pg_authid_rolname_index")
    connection = RoleConnection([None, state], failure)
    with pytest.raises(DBAPIError) as raised:
        _ensure_control_plane_role(connection, OPERATIONS_ROLE)
    assert raised.value is failure
    assert connection.events[-2:] == ["rollback", "catalog read"]


@pytest.mark.parametrize("code,constraint", [("23505", "another_unique_index"), ("42501", None)])
def test_unrelated_provider_errors_are_not_misclassified_as_creation_races(code, constraint):
    failure = provider_error(code, constraint)
    connection = RoleConnection([None], failure)
    with pytest.raises(DBAPIError) as raised:
        _ensure_control_plane_role(connection, OPERATIONS_ROLE)
    assert raised.value is failure
    assert connection.events == ["catalog read", "savepoint", "create role", "rollback"]


def test_existing_roles_require_bounded_capabilities_and_new_roles_commit_creation():
    existing = RoleConnection([True])
    _ensure_control_plane_role(existing, OPERATIONS_ROLE)
    assert existing.events == ["catalog read"]
    with pytest.raises(ValueError, match="incompatible"):
        _ensure_control_plane_role(RoleConnection([False]), OPERATIONS_ROLE)
    created = RoleConnection([None])
    _ensure_control_plane_role(created, OPERATIONS_ROLE)
    assert created.events == ["catalog read", "savepoint", "create role", "commit"]
