"""The database refuses writes the application is not authorized to make.

#492 replaced "only the record-writing seam writes accepted authority" as a
convention with a boundary PostgreSQL enforces.  These tests connect as the
real runtime logins rather than as the test database owner, because a proof
that runs as the owner proves nothing: the owner can do everything.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from corridor.config import settings
from corridor.models import Document, FactDecision, Project, SourceSegment


# The configured URL, never a private copy of it: AGENTS.md records that a
# Homebrew PostgreSQL 14 answers on 5432 with the same credentials, so a test
# that hardcodes its own fallback can pass against the wrong server.
ADMIN_URL = settings.database_url

OWNER_ROLES = (
    "corridor_source_append",
    "corridor_fact_decision_writer",
    "corridor_statement_retirement",
)
RUNTIME_LOGIN_ROLES = ("corridor_web", "corridor_worker")

# The legacy-development login is created only where a deployment asks for it,
# so a customer environment holds no credential that can write the frozen
# legacy accepted tables at all.
LEGACY_DEV_ROLE = "corridor_legacy_dev"
LEGACY_DEV_ENABLED = os.environ.get(
    "CORRIDOR_LEGACY_DEV_LOGIN", ""
).strip().lower() in {"1", "true", "yes"}
LOGIN_ROLES = RUNTIME_LOGIN_ROLES + (
    (LEGACY_DEV_ROLE,) if LEGACY_DEV_ENABLED else ()
)

# The passwords the migration gives each login, resolved the same way it
# resolves them, so a deployment that sets its own does not break this proof.
LOGIN_PASSWORDS = {
    "corridor_web": os.environ.get("CORRIDOR_WEB_DB_PASSWORD") or "corridor_web",
    "corridor_worker": os.environ.get("CORRIDOR_WORKER_DB_PASSWORD")
    or "corridor_worker",
    LEGACY_DEV_ROLE: os.environ.get("CORRIDOR_LEGACY_DEV_DB_PASSWORD")
    or LEGACY_DEV_ROLE,
}
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
# The spine's source tables: appended only through the commands the
# source-append role owns, never written raw by a runtime capability.
SOURCE_TABLES = (
    "source_segments",
    "facts",
    "fact_sources",
    "fact_applies_to",
    "fact_closure_results",
    "fact_closure_sources",
    "fact_statement_timings",
    "extracted_proposals",
    "extracted_proposal_facts",
    "source_fact_append_receipts",
)
SOURCE_APPEND_COMMANDS = (
    "append_source_segments",
    "append_fact",
    "append_extracted_proposal",
    "append_source_fact_receipt",
)


def _url_for(role: str) -> str:
    """The runtime URL for one capability, on the database under test."""

    return (
        make_url(ADMIN_URL)
        .set(username=role, password=LOGIN_PASSWORDS[role])
        .render_as_string(hide_password=False)
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
        yield request.param, connection
    engine.dispose()


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
                cross join unnest(cast(:logins as text[])) as r(rolname)
                where n.nspname = 'public' and p.proname = :name
                  and has_function_privilege(r.rolname, p.oid, 'execute')
                """
            ),
            {"name": command, "logins": list(LOGIN_ROLES)},
        ).scalars().all()

        assert granted == ["corridor_web"], f"{command}: {granted}"


def test_machine_policy_commands_are_callable_only_by_the_worker_capability(admin):
    granted = admin.execute(
        text(
            """
            select r.rolname
            from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            cross join unnest(cast(:logins as text[])) as r(rolname)
            where n.nspname = 'public'
              and p.proname = 'include_structured_cell_fact_decision'
              and has_function_privilege(r.rolname, p.oid, 'execute')
            """
        ),
        {"logins": list(LOGIN_ROLES)},
    ).scalars().all()

    assert granted == ["corridor_worker"]


def test_public_cannot_create_objects_in_the_public_schema(admin):
    creatable = admin.execute(
        text("select has_schema_privilege('public', 'public', 'create')")
    ).scalar_one()

    assert creatable is False


# --- The application's own bindings ---------------------------------------


def test_the_application_bindings_do_not_carry_the_schema_owner(admin):
    """The web and worker engines connect as capability logins (#492).

    The architecture test proves no module imports the owner binding; this
    proves the bindings those modules do import resolve to logins the database
    refuses accepted-authority writes to.
    """

    from corridor.db import WEB_DATABASE_URL, WORKER_DATABASE_URL

    owner = make_url(ADMIN_URL).username
    for label, url in (
        ("web", WEB_DATABASE_URL),
        ("worker", WORKER_DATABASE_URL),
    ):
        login = make_url(url).username
        assert login != owner, f"{label} binding connects as the schema owner"

        attributes = admin.execute(
            text(
                "select rolsuper, rolcreaterole, rolcreatedb "
                "from pg_roles where rolname = :login"
            ),
            {"login": login},
        ).one()
        assert attributes.rolsuper is False, label
        assert attributes.rolcreaterole is False, label
        assert attributes.rolcreatedb is False, label

        writes_accepted = admin.execute(
            text(
                "select has_table_privilege(:login, 'fact_decisions', 'insert')"
            ),
            {"login": login},
        ).scalar_one()
        assert writes_accepted is False, f"{label} can write accepted authority"


# --- Refusals proved against the real runtime logins ----------------------


@pytest.mark.parametrize("table", ACCEPTED_TABLES)
def test_a_runtime_capability_cannot_write_accepted_authority(runtime, table):
    _role, connection = runtime

    with pytest.raises(ProgrammingError) as refused:
        connection.execute(text(f"insert into {table} default values"))

    assert "permission denied" in str(refused.value)


@pytest.mark.parametrize("table", LEGACY_ACCEPTED_TABLES)
def test_a_runtime_capability_cannot_write_legacy_accepted_tables(runtime, table):
    _role, connection = runtime

    with pytest.raises(ProgrammingError) as refused:
        connection.execute(text(f"insert into {table} default values"))

    assert "permission denied" in str(refused.value)


def test_a_runtime_capability_cannot_write_accepted_authority_through_the_orm(
    runtime,
):
    """The refusal is the database's, so the ORM cannot route around it.

    Every other refusal here is raw SQL.  This one goes through the mapped
    class and a Session, which is how the application actually writes, so a
    future ORM write path cannot quietly hold authority the raw-SQL proof
    denies.
    """

    _role, connection = runtime
    with Session(bind=connection) as session:
        session.add(
            FactDecision(
                project_id=1,
                fact_id=1,
                subject_key="authority-probe",
                fact_type="stationing",
                revision_id=1,
            )
        )

        with pytest.raises(ProgrammingError) as refused:
            session.flush()

    assert "permission denied for table fact_decisions" in str(refused.value)


def test_a_runtime_capability_cannot_set_role_into_a_command_owner(runtime):
    _role, connection = runtime

    for owner in OWNER_ROLES:
        with pytest.raises(ProgrammingError) as refused:
            connection.execute(text(f"set role {owner}"))
        connection.rollback()

        assert "permission denied to set role" in str(refused.value)


def test_a_runtime_capability_cannot_disable_a_guarding_trigger(runtime):
    _role, connection = runtime

    with pytest.raises(ProgrammingError) as refused:
        connection.execute(text("alter table facts disable trigger all"))

    assert "must be owner of table facts" in str(refused.value)


def test_a_runtime_capability_cannot_call_the_other_command_family(runtime):
    role, connection = runtime
    wrong_family = (
        "select include_structured_cell_fact_decision(1,1,'k','t','i','p')"
        if role == "corridor_web"
        else "select record_human_fact_decision(1,1,'k','t','c','d','p','i',null)"
    )

    with pytest.raises(ProgrammingError) as refused:
        connection.execute(text(wrong_family))

    assert "permission denied for function" in str(refused.value)


@pytest.mark.skipif(
    not LEGACY_DEV_ENABLED,
    reason="the legacy-development login is created only where it is asked for",
)
def test_the_legacy_development_login_writes_legacy_tables_and_nothing_accepted(admin):
    for table in LEGACY_ACCEPTED_TABLES:
        assert admin.execute(
            text("select has_table_privilege(:role, :t, 'insert')"),
            {"role": LEGACY_DEV_ROLE, "t": table},
        ).scalar_one() is True, table

    for table in ACCEPTED_TABLES:
        assert admin.execute(
            text("select has_table_privilege(:role, :t, 'insert')"),
            {"role": LEGACY_DEV_ROLE, "t": table},
        ).scalar_one() is False, table


# --- Source appends go through commands, not raw table grants -------------


def test_source_append_commands_are_owned_by_the_source_append_role(admin):
    rows = admin.execute(
        text(
            """
            select p.proname, o.rolname as owner
            from pg_proc p
            join pg_namespace n on n.oid = p.pronamespace
            join pg_roles o on o.oid = p.proowner
            where n.nspname = 'public' and p.proname = any(:names)
            """
        ),
        {"names": list(SOURCE_APPEND_COMMANDS)},
    ).all()

    assert {row.proname for row in rows} == set(SOURCE_APPEND_COMMANDS)
    assert all(row.owner == "corridor_source_append" for row in rows), rows


def test_source_append_commands_are_callable_by_both_runtime_capabilities_only(admin):
    """Web appends verbal statements; the worker appends extractions.

    Neither the legacy-development login nor anything else holds the command.
    """

    for command in SOURCE_APPEND_COMMANDS:
        granted = admin.execute(
            text(
                """
                select r.rolname
                from pg_proc p
                join pg_namespace n on n.oid = p.pronamespace
                cross join unnest(cast(:logins as text[])) as r(rolname)
                where n.nspname = 'public' and p.proname = :name
                  and has_function_privilege(r.rolname, p.oid, 'execute')
                order by r.rolname
                """
            ),
            {"name": command, "logins": list(LOGIN_ROLES)},
        ).scalars().all()

        assert granted == ["corridor_web", "corridor_worker"], f"{command}: {granted}"


@pytest.mark.parametrize("table", SOURCE_TABLES)
def test_a_runtime_capability_cannot_write_a_source_table_directly(runtime, table):
    _role, connection = runtime

    with pytest.raises(ProgrammingError) as refused:
        connection.execute(text(f"insert into {table} default values"))

    assert "permission denied" in str(refused.value)


def test_a_runtime_capability_cannot_append_a_source_segment_through_the_orm(runtime):
    """The mapped class is how the appenders used to write; the database refuses it."""

    _role, connection = runtime
    with Session(bind=connection) as session:
        session.add(
            SourceSegment(
                project_id=1,
                document_id=1,
                kind="spreadsheet_cell",
                exact_text="probe",
                content_sha256="0" * 64,
                ordinal=1,
                sheet_name="Sheet",
                cell_range="A1",
            )
        )

        with pytest.raises(ProgrammingError) as refused:
            session.flush()

    assert "permission denied for table source_segments" in str(refused.value)


def test_a_runtime_capability_appends_source_segments_only_through_the_command(
    runtime_database,
):
    """The positive half of the boundary, as the worker login on committed rows.

    The refusals above prove the door is shut; this proves the command is the
    door: the same login that cannot insert a row appends one through
    ``append_source_segments`` and reads it back.
    """

    with runtime_database.session_factory.begin() as owner:
        project = Project(slug="append-boundary", name="Append Boundary", is_synthetic=True)
        owner.add(project)
        owner.flush()
        document = Document(
            project_id=project.id,
            sha256="1" * 64,
            filename="boundary.xlsx",
            doc_type="matrix",
        )
        owner.add(document)
        owner.flush()
        project_id, document_id = project.id, document.id

    worker_url = (
        make_url(ADMIN_URL)
        .set(
            database=runtime_database.name,
            username="corridor_worker",
            password=LOGIN_PASSWORDS["corridor_worker"],
        )
        .render_as_string(hide_password=False)
    )
    engine = create_engine(worker_url, poolclass=NullPool, future=True)
    try:
        with engine.begin() as worker:
            appended = worker.execute(
                text(
                    "select append_source_segments(:project_id, :document_id, null, "
                    "cast(:segments as jsonb))"
                ),
                {
                    "project_id": project_id,
                    "document_id": document_id,
                    "segments": json.dumps(
                        [
                            {
                                "kind": "spreadsheet_cell",
                                "exact_text": "UC-1",
                                "content_sha256": sha256(b"UC-1").hexdigest(),
                                "ordinal": 1,
                                "sheet_name": "Conflicts",
                                "cell_range": "A2",
                            }
                        ]
                    ),
                },
            ).scalar_one()
            stored = worker.execute(
                text(
                    "select project_id, document_id, kind, exact_text, sheet_name, "
                    "cell_range, ordinal from source_segments where id = :id"
                ),
                {"id": appended[0]},
            ).one()
            assert tuple(stored) == (
                project_id, document_id, "spreadsheet_cell", "UC-1", "Conflicts", "A2", 1
            )

        with engine.connect() as worker:
            with pytest.raises(ProgrammingError) as refused:
                worker.execute(
                    text(
                        "insert into source_segments (project_id, document_id, kind, "
                        "exact_text, content_sha256, ordinal, sheet_name, cell_range) "
                        "values (:project_id, :document_id, 'spreadsheet_cell', 'UC-2', "
                        ":digest, 2, 'Conflicts', 'A3')"
                    ),
                    {
                        "project_id": project_id,
                        "document_id": document_id,
                        "digest": sha256(b"UC-2").hexdigest(),
                    },
                )
            assert "permission denied for table source_segments" in str(refused.value)
    finally:
        engine.dispose()
