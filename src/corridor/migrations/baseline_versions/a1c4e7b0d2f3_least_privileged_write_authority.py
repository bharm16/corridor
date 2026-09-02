"""Least-privileged database write authority (#492).

Revision ID: a1c4e7b0d2f3
Revises: 8e9f0a1b2c34

"Only the record-writing seam writes accepted authority" was convention and
module structure, not a boundary the database enforced.  One ``corridor``
superuser owned the schema, ran the migrations, and served the application, so
the running process could write any table; the ``SECURITY DEFINER`` commands
were executable by ``PUBLIC``, so any login could call the human-decision
command and pass whatever principal it liked.

This revision makes the boundary real.  Application logins are separate from
the migration credential, hold no membership in the NOLOGIN roles that own the
authority-bearing commands, and cannot ``SET ROLE`` into them.  A human command
is granted to the web capability only, a machine-policy command to the worker
capability only.  Legacy accepted tables are writable only by a
legacy-development credential that a customer deployment does not create.

Passwords come from the environment so a deployment sets its own; the defaults
exist only to keep a local clone and CI bootable.
"""

from __future__ import annotations

import os
import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError


revision = "a1c4e7b0d2f3"
down_revision = "8e9f0a1b2c34"
branch_labels = None
depends_on = None


# NOLOGIN roles that own authority-bearing commands.  No application login is
# a member of any of them.
OWNER_ROLES = (
    "corridor_source_append",
    "corridor_fact_decision_writer",
    "corridor_statement_retirement",
)

# Runtime application logins, and the environment variable that sets each
# password.  Both exist in every environment.
RUNTIME_LOGIN_ROLES = {
    "corridor_web": "CORRIDOR_WEB_DB_PASSWORD",
    "corridor_worker": "CORRIDOR_WORKER_DB_PASSWORD",
}

# The legacy-development login writes the frozen legacy accepted tables.  It
# is created only where a deployment asks for it, so a customer environment
# has no credential that can write them at all.  Local clones and CI opt in
# through CORRIDOR_LEGACY_DEV_LOGIN.
LEGACY_DEV_ROLE = "corridor_legacy_dev"
LEGACY_DEV_PASSWORD_VARIABLE = "CORRIDOR_LEGACY_DEV_DB_PASSWORD"
LEGACY_DEV_ENABLED_VARIABLE = "CORRIDOR_LEGACY_DEV_LOGIN"


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

# Accepted authority: written only through the record-decision commands.
ACCEPTED_TABLES = (
    "project_record_revisions",
    "fact_decisions",
    "subject_resolution_decisions",
)

# Legacy accepted tables (ADR-0081 freeze).  A customer or staging credential
# never writes these directly; only the legacy-development login does.
LEGACY_ACCEPTED_TABLES = (
    "dependencies",
    "dependency_events",
    "work_decisions",
    "operative_support",
    "dispute_history_resolutions",
    "dispute_settlements",
)

# A human being decides; the web capability alone may call these.
HUMAN_DECISION_COMMANDS = (
    "record_human_fact_decision",
    "record_subject_alias_decision",
)

# A released policy decides; the worker capability alone may call these.
MACHINE_POLICY_COMMANDS = ("include_structured_cell_fact_decision",)


def _password(variable: str, role: str) -> str:
    return os.environ.get(variable) or role


# Two sessions racing on pg_authid raise this; a second session losing the
# create race raises the duplicate. Nothing else is retried.
_ROLE_RACE = ("tuple concurrently updated", "already exists")
_ROLE_RETRIES = 10


def _apply_role_ddl(connection, statement: str) -> None:
    """Run one cluster-role statement, retrying only a catalog race.

    Roles live in the cluster, not in one database, so every parallel test
    worker migrating its own database runs this same setup concurrently.  Each
    attempt takes a savepoint, because a failed statement would otherwise
    abort the migration's transaction and make a retry impossible.
    """

    for attempt in range(_ROLE_RETRIES):
        savepoint = connection.begin_nested()
        try:
            connection.exec_driver_sql(statement)
        except DBAPIError as error:
            savepoint.rollback()
            raced = any(reason in str(error) for reason in _ROLE_RACE)
            if not raced or attempt == _ROLE_RETRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
        else:
            savepoint.commit()
            return


def _quote(connection, function: str, value: str) -> str:
    """Let PostgreSQL quote an identifier or literal for us.

    A password arrives from the environment.  Escaping it in Python would put
    caller-supplied text inside a dollar-quoted block, where a password
    containing ``$$`` closes the block early and the rest of it executes as
    DDL.
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
        sa.text(
            f"select 1 from pg_roles where rolname = :role and ({predicate})"
        ),
        {"role": role},
    ).first() is not None


def upgrade() -> None:
    """Create the role matrix and replace PUBLIC execution with grants."""

    connection = op.get_bind()
    runtime_logins = ", ".join(
        _quote(connection, "quote_ident", role) for role in _login_roles()
    )

    for role in OWNER_ROLES:
        identifier = _quote(connection, "quote_ident", role)
        if _role_missing(connection, role):
            _apply_role_ddl(
                connection, f"create role {identifier} nologin noinherit"
            )
        # An owner role never logs in and never inherits, even if an earlier
        # revision created it differently.
        if _role_attributes_differ(
            connection,
            role,
            "rolcanlogin or rolinherit or rolsuper or rolcreatedb "
            "or rolcreaterole",
        ):
            _apply_role_ddl(
                connection,
                f"alter role {identifier} nologin noinherit nosuperuser "
                f"nocreatedb nocreaterole",
            )

    for role, variable in _login_roles().items():
        identifier = _quote(connection, "quote_ident", role)
        if _role_missing(connection, role):
            # The password is set once, at creation.  Re-setting it on every
            # migration would rewrite the catalog row each time and reopen the
            # race this block is careful about; a deployment rotates
            # credentials out of band.
            password = _quote(
                connection, "quote_literal", _password(variable, role)
            )
            _apply_role_ddl(
                connection,
                f"create role {identifier} login password {password} "
                f"nosuperuser nocreatedb nocreaterole noinherit nobypassrls",
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

    # No application login may borrow an owner role's authority.
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

    # The public schema stops being a place anyone can create objects in.
    op.execute("revoke create on schema public from public")
    op.execute(f"grant usage on schema public to {runtime_logins}")

    # Every application login reads the record.
    op.execute(
        f"grant select on all tables in schema public to {runtime_logins}"
    )
    op.execute(
        f"grant usage, select on all sequences in schema public to "
        f"{runtime_logins}"
    )

    # Ordinary working tables stay writable by the runtime capabilities; the
    # accepted and legacy accepted tables are then taken back below.
    op.execute(
        f"grant insert, update, delete on all tables in schema public "
        f"to {runtime_logins}"
    )
    for table in ACCEPTED_TABLES + LEGACY_ACCEPTED_TABLES:
        op.execute(
            f"revoke insert, update, delete, truncate on {table} "
            f"from {runtime_logins}"
        )
    # Legacy projects still need a write path in local and test environments,
    # and only there.
    if _legacy_dev_requested():
        legacy = _quote(connection, "quote_ident", LEGACY_DEV_ROLE)
        for table in LEGACY_ACCEPTED_TABLES:
            op.execute(f"grant insert, update, delete on {table} to {legacy}")

    # Authority-bearing commands stop being callable by PUBLIC.
    op.execute(
        """
        do $$
        declare command record;
        begin
            for command in
                select p.oid::regprocedure as signature
                from pg_proc p
                join pg_namespace n on n.oid = p.pronamespace
                where n.nspname = 'public' and p.prosecdef
            loop
                execute format(
                    'revoke all on function %s from public', command.signature
                );
            end loop;
        end $$;
        """
    )
    for command in HUMAN_DECISION_COMMANDS:
        op.execute(_grant_execute(command, "corridor_web"))
    for command in MACHINE_POLICY_COMMANDS:
        op.execute(_grant_execute(command, "corridor_worker"))

    # Grants above are a point-in-time snapshot.  Without these defaults a
    # table added by a later migration (#518 and #519 add Proposed Delta and
    # Resolve Delta tables) would grant the runtime capabilities nothing, and
    # the application would fail on a table it is supposed to read.
    op.execute(
        f"alter default privileges in schema public "
        f"grant select on tables to {runtime_logins}"
    )
    op.execute(
        f"alter default privileges in schema public "
        f"grant insert, update, delete on tables to {runtime_logins}"
    )
    op.execute(
        f"alter default privileges in schema public "
        f"grant usage, select on sequences to {runtime_logins}"
    )
    # PostgreSQL 16 does not persist a default-privilege revoke of the
    # built-in PUBLIC EXECUTE on functions: the statement succeeds, writes no
    # pg_default_acl row, and a later function is still world-executable.
    # Verified directly against 16 rather than assumed.  The boundary for a
    # future command is therefore held by a test that fails when any SECURITY
    # DEFINER function carries a PUBLIC grantee
    # (tests/test_database_authority.py), not by a default that silently does
    # nothing.  A new authority-bearing command must revoke PUBLIC itself.


def _grant_execute(command: str, role: str) -> str:
    """Grant EXECUTE on every overload of one command, by identity."""

    return f"""
        do $$
        declare signature text;
        begin
            for signature in
                select p.oid::regprocedure::text
                from pg_proc p
                join pg_namespace n on n.oid = p.pronamespace
                where n.nspname = 'public' and p.proname = '{command}'
            loop
                execute format(
                    'grant execute on function %s to {role}', signature
                );
            end loop;
        end $$;
    """


def downgrade() -> None:
    """Write authority is a safety boundary; no path reopens it."""

    raise RuntimeError(
        "least-privileged write authority migration downgrade is unsupported"
    )
