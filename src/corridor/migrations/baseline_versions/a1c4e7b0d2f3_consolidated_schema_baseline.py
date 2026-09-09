"""Build the whole Corridor schema from one consolidated baseline.

Revision ID: a1c4e7b0d2f3
Revises: None

#423 consolidated 112 development revisions into a bounded window, and the
window grew straight back: by #548 the executable chain carried a builder, a
marker, and twenty-one further revisions, because the guard was a test
listing permitted filenames rather than an assertion about the graph.

This revision collapses the chain again, and keeps the identifier the
previous head already carried. The one database that stands at that head is
therefore already stamped correctly and does nothing; a blank database builds
the whole schema here instead of replaying twenty-one development steps.

The cluster's database heads were inventoried before any revision was
retired: exactly one database stood at the previous head, and every other sat
at a pre-#423 revision already outside the executable graph. The retired
revisions' exact bytes are preserved in ``migrations/versions`` rather than
deleted.

Two files do the work. The schema is a dump of the chain-built database.
``pg_dump`` expands ``col IN (…)`` into an explicit comparison, though, and
PostgreSQL stores that expansion differently on the way back, so a
dump-built schema diverges from the chain-built one on twenty-nine check
constraints. Only the ``IN`` form stores the canonical expression, so the
normalisation file re-creates exactly those twenty-nine with it. The schema
fingerprint is identical either way, which the baseline test asserts.

Roles come first because they are cluster-level objects a schema dump cannot
carry, and the dump assigns ownership and grants to them. The
legacy-development login stays opt-in (#492): a customer deployment creates
no credential that can write the frozen legacy accepted tables.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError


revision = "a1c4e7b0d2f3"
down_revision = None
branch_labels = None
depends_on = None


OWNER_ROLES = (
    "corridor_source_append",
    "corridor_fact_decision_writer",
    "corridor_statement_retirement",
)
RUNTIME_LOGIN_ROLES = {
    "corridor_web": "CORRIDOR_WEB_DB_PASSWORD",
    "corridor_worker": "CORRIDOR_WORKER_DB_PASSWORD",
}
LEGACY_DEV_ROLE = "corridor_legacy_dev"
LEGACY_DEV_PASSWORD_VARIABLE = "CORRIDOR_LEGACY_DEV_DB_PASSWORD"
LEGACY_DEV_ENABLED_VARIABLE = "CORRIDOR_LEGACY_DEV_LOGIN"

LEGACY_ACCEPTED_TABLES = (
    "dependencies",
    "dependency_events",
    "work_decisions",
    "operative_support",
    "dispute_history_resolutions",
    "dispute_settlements",
)

# Global roles can be created by migrations in different databases. A loser
# sees duplicate_object, or the pg_authid role-name unique index if its lookup
# preceded the other transaction's commit. Retry only those catalog races.
_ROLE_RETRIES = 10


def _legacy_dev_requested() -> bool:
    return os.environ.get(LEGACY_DEV_ENABLED_VARIABLE, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _login_roles() -> dict[str, str]:
    roles = dict(RUNTIME_LOGIN_ROLES)
    if _legacy_dev_requested():
        roles[LEGACY_DEV_ROLE] = LEGACY_DEV_PASSWORD_VARIABLE
    return roles


def _apply_role_ddl(connection, statement: str, *, creating_role: str | None = None) -> None:
    """Converge on a concurrently created role, then let callers harden it.

    Repeating CREATE after another migration commits can never succeed. Once
    the failed savepoint is rolled back, a fresh catalog read must establish
    that exact role before treating a create collision as success. Keep error
    classification on provider diagnostics, never password-bearing SQL text.
    """

    for attempt in range(_ROLE_RETRIES):
        savepoint = connection.begin_nested()
        try:
            connection.exec_driver_sql(statement)
        except DBAPIError as error:
            savepoint.rollback()
            original = error.orig
            code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
            create_collision = creating_role is not None and (
                code == "42710" or (code == "23505" and constraint == "pg_authid_rolname_index")
            )
            if create_collision and not _role_missing(connection, creating_role):
                return
            raced = create_collision or "tuple concurrently updated" in str(original)
            if not raced or attempt == _ROLE_RETRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
        else:
            savepoint.commit()
            return


def _quote(connection, function: str, value: str) -> str:
    """Let PostgreSQL quote an identifier or literal.

    A password arrives from the environment; escaping it in Python would put
    caller-supplied text inside a dollar-quoted block, where a password
    containing ``$$`` closes the block early and the rest executes as DDL.
    """

    return connection.execute(
        sa.text(f"select {function}(:value)"), {"value": value}
    ).scalar_one()


def _role_missing(connection, role: str) -> bool:
    return connection.execute(
        sa.text("select 1 from pg_roles where rolname = :role"), {"role": role}
    ).first() is None


def _role_attributes_differ(connection, role: str, predicate: str) -> bool:
    return connection.execute(
        sa.text(f"select 1 from pg_roles where rolname = :role and ({predicate})"),
        {"role": role},
    ).first() is not None


def _sql(name: str) -> str:
    return Path(__file__).with_name(name).read_text(encoding="utf-8")


def upgrade() -> None:
    """Create the roles, then the complete schema, on a blank database."""

    connection = op.get_bind()

    for role in OWNER_ROLES:
        identifier = _quote(connection, "quote_ident", role)
        if _role_missing(connection, role):
            _apply_role_ddl(connection, f"create role {identifier} nologin noinherit", creating_role=role)
        if _role_attributes_differ(
            connection,
            role,
            "rolcanlogin or rolinherit or rolsuper or rolcreatedb or rolcreaterole",
        ):
            _apply_role_ddl(
                connection,
                f"alter role {identifier} nologin noinherit nosuperuser "
                f"nocreatedb nocreaterole",
            )

    for role, variable in _login_roles().items():
        identifier = _quote(connection, "quote_ident", role)
        if _role_missing(connection, role):
            password = _quote(
                connection, "quote_literal", os.environ.get(variable) or role
            )
            _apply_role_ddl(
                connection,
                f"create role {identifier} login password {password} "
                f"nosuperuser nocreatedb nocreaterole noinherit nobypassrls",
                creating_role=role,
            )
        if _role_attributes_differ(
            connection,
            role,
            "not rolcanlogin or rolsuper or rolcreatedb or rolcreaterole "
            "or rolinherit or rolbypassrls",
        ):
            _apply_role_ddl(
                connection,
                f"alter role {identifier} login nosuperuser nocreatedb "
                f"nocreaterole noinherit nobypassrls",
            )

    for owner in OWNER_ROLES:
        for role in _login_roles():
            member = connection.execute(
                sa.text(
                    """
                    select 1 from pg_auth_members m
                    join pg_roles o on o.oid = m.roleid
                    join pg_roles k on k.oid = m.member
                    where o.rolname = :owner and k.rolname = :member
                    """
                ),
                {"owner": owner, "member": role},
            ).first()
            if member is not None:
                _apply_role_ddl(
                    connection,
                    f"revoke {_quote(connection, 'quote_ident', owner)} "
                    f"from {_quote(connection, 'quote_ident', role)}",
                )

    connection.exec_driver_sql(
        _sql("a1c4e7b0d2f3_consolidated_schema.sql").replace("%", "%%")
    )
    connection.exec_driver_sql(
        _sql("a1c4e7b0d2f3_normalize_constraints.sql").replace("%", "%%")
    )
    # pg_dump empties search_path and schema-qualifies every name, so the
    # grants below must qualify too or they resolve against nothing.
    connection.exec_driver_sql("set search_path = public")

    if _legacy_dev_requested():
        legacy = _quote(connection, "quote_ident", LEGACY_DEV_ROLE)
        op.execute(f"grant usage on schema public to {legacy}")
        op.execute(f"grant select on all tables in schema public to {legacy}")
        op.execute(
            f"grant usage, select on all sequences in schema public to {legacy}"
        )
        for table in LEGACY_ACCEPTED_TABLES:
            op.execute(
                f"grant insert, update, delete on public.{table} to {legacy}"
            )
        op.execute(
            f"alter default privileges in schema public "
            f"grant select on tables to {legacy}"
        )


def downgrade() -> None:
    """Downgrade across the consolidated baseline is unsupported."""

    raise RuntimeError("consolidated schema baseline downgrade is unsupported")
