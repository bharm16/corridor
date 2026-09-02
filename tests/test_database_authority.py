"""The database refuses writes the application is not authorized to make.

#492 replaced "only the record-writing seam writes accepted authority" as a
convention with a boundary PostgreSQL enforces.  These tests connect as the
real runtime logins rather than as the test database owner, because a proof
that runs as the owner proves nothing: the owner can do everything.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool


ADMIN_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
)
OWNER_ROLES = (
    "corridor_source_append",
    "corridor_fact_decision_writer",
    "corridor_statement_retirement",
)
LOGIN_ROLES = ("corridor_web", "corridor_worker", "corridor_legacy_dev")
ACCEPTED_TABLES = (
    "project_record_revisions",
    "fact_decisions",
    "subject_resolution_decisions",
)
LEGACY_ACCEPTED_TABLES = (
    "dependencies",
    "dependency_events",
    "work_decisions",
    "operative_support",
    "dispute_history_resolutions",
    "dispute_settlements",
)


def _url_for(role: str) -> str:
    """The runtime URL for one capability, on the database under test."""

    return make_url(ADMIN_URL).set(username=role, password=role).render_as_string(
        hide_password=False
    )


@pytest.fixture(scope="module")
def admin():
    engine = create_engine(ADMIN_URL, poolclass=NullPool, future=True)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


@pytest.fixture(params=["corridor_web", "corridor_worker"])
def runtime(request):
    """A connection held by an application capability, not the owner."""

    engine = create_engine(_url_for(request.param), poolclass=NullPool, future=True)
    with engine.connect() as connection:
        connection.role = request.param
        yield connection
    engine.dispose()


def _connection_for(role: str):
    engine = create_engine(_url_for(role), poolclass=NullPool, future=True)
    return engine, engine.connect()


# --- Role attributes ------------------------------------------------------


def test_application_logins_hold_no_cluster_authority(admin):
    rows = admin.execute(
        text(
            "select rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
            "from pg_roles where rolname = any(:names)"
        ),
        {"names": list(LOGIN_ROLES)},
    ).all()

    assert {row.rolname for row in rows} == set(LOGIN_ROLES)
    for row in rows:
        assert row.rolsuper is False, row.rolname
        assert row.rolcreaterole is False, row.rolname
        assert row.rolcreatedb is False, row.rolname
        assert row.rolbypassrls is False, row.rolname


def test_command_owner_roles_cannot_log_in(admin):
    rows = admin.execute(
        text(
            "select rolname, rolcanlogin from pg_roles where rolname = any(:names)"
        ),
        {"names": list(OWNER_ROLES)},
    ).all()

    assert {row.rolname for row in rows} == set(OWNER_ROLES)
    assert all(row.rolcanlogin is False for row in rows)


def test_no_application_login_is_a_member_of_a_command_owner(admin):
    members = admin.execute(
        text(
            """
            select owner.rolname as owner, member.rolname as member
            from pg_auth_members m
            join pg_roles owner on owner.oid = m.roleid
            join pg_roles member on member.oid = m.member
            where owner.rolname = any(:owners) and member.rolname = any(:logins)
            """
        ),
        {"owners": list(OWNER_ROLES), "logins": list(LOGIN_ROLES)},
    ).all()

    assert members == []


# --- Execution privileges -------------------------------------------------


def test_no_authority_bearing_command_is_executable_by_public(admin):
    public_grants = admin.execute(
        text(
            """
            select p.proname, array_to_string(p.proacl, ', ') as acl
            from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'public' and p.prosecdef
            """
        )
    ).all()

    assert public_grants, "expected SECURITY DEFINER commands to exist"
    for row in public_grants:
        # A NULL ACL is PostgreSQL's default, which grants EXECUTE to PUBLIC;
        # an entry starting with '=' is an explicit PUBLIC grant.
        assert row.acl is not None, f"{row.proname} still has the default PUBLIC grant"
        assert not row.acl.startswith("="), f"{row.proname} grants EXECUTE to PUBLIC"
        assert ", =" not in row.acl, f"{row.proname} grants EXECUTE to PUBLIC"


def test_human_decision_commands_are_callable_only_by_the_web_capability(admin):
    for command in ("record_human_fact_decision", "record_subject_alias_decision"):
        granted = admin.execute(
            text(
                """
                select r.rolname
                from pg_proc p
                join pg_namespace n on n.oid = p.pronamespace
                cross join unnest(array['corridor_web','corridor_worker',
                                        'corridor_legacy_dev']) as r(rolname)
                where n.nspname = 'public' and p.proname = :name
                  and has_function_privilege(r.rolname, p.oid, 'execute')
                """
            ),
            {"name": command},
        ).scalars().all()

        assert granted == ["corridor_web"], f"{command}: {granted}"


def test_machine_policy_commands_are_callable_only_by_the_worker_capability(admin):
    granted = admin.execute(
        text(
            """
            select r.rolname
            from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            cross join unnest(array['corridor_web','corridor_worker',
                                    'corridor_legacy_dev']) as r(rolname)
            where n.nspname = 'public'
              and p.proname = 'include_structured_cell_fact_decision'
              and has_function_privilege(r.rolname, p.oid, 'execute')
            """
        )
    ).scalars().all()

    assert granted == ["corridor_worker"]


def test_public_cannot_create_objects_in_the_public_schema(admin):
    creatable = admin.execute(
        text("select has_schema_privilege('public', 'public', 'create')")
    ).scalar_one()

    assert creatable is False


# --- Refusals proved against the real runtime logins ----------------------


@pytest.mark.parametrize("table", ACCEPTED_TABLES)
def test_a_runtime_capability_cannot_write_accepted_authority(runtime, table):
    with pytest.raises(ProgrammingError) as refused:
        runtime.execute(text(f"insert into {table} default values"))

    assert "permission denied" in str(refused.value)


@pytest.mark.parametrize("table", LEGACY_ACCEPTED_TABLES)
def test_a_runtime_capability_cannot_write_legacy_accepted_tables(runtime, table):
    with pytest.raises(ProgrammingError) as refused:
        runtime.execute(text(f"insert into {table} default values"))

    assert "permission denied" in str(refused.value)


def test_a_runtime_capability_cannot_set_role_into_a_command_owner(runtime):
    for owner in OWNER_ROLES:
        with pytest.raises(ProgrammingError) as refused:
            runtime.execute(text(f"set role {owner}"))
        runtime.rollback()

        assert "permission denied to set role" in str(refused.value)


def test_a_runtime_capability_cannot_disable_a_guarding_trigger(runtime):
    with pytest.raises(ProgrammingError) as refused:
        runtime.execute(text("alter table facts disable trigger all"))

    assert "must be owner of table facts" in str(refused.value)


def test_a_runtime_capability_cannot_call_the_other_command_family(runtime):
    wrong_family = (
        "select include_structured_cell_fact_decision(1,1,'k','t','i','p')"
        if runtime.role == "corridor_web"
        else "select record_human_fact_decision(1,1,'k','t','c','d','p','i',null)"
    )

    with pytest.raises(ProgrammingError) as refused:
        runtime.execute(text(wrong_family))

    assert "permission denied for function" in str(refused.value)


def test_the_legacy_development_login_writes_legacy_tables_and_nothing_accepted(admin):
    for table in LEGACY_ACCEPTED_TABLES:
        assert admin.execute(
            text("select has_table_privilege('corridor_legacy_dev', :t, 'insert')"),
            {"t": table},
        ).scalar_one() is True, table

    for table in ACCEPTED_TABLES:
        assert admin.execute(
            text("select has_table_privilege('corridor_legacy_dev', :t, 'insert')"),
            {"t": table},
        ).scalar_one() is False, table
