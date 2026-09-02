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

from alembic import op


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

# Application logins, and the environment variable that sets each password.
LOGIN_ROLES = {
    "corridor_web": "CORRIDOR_WEB_DB_PASSWORD",
    "corridor_worker": "CORRIDOR_WORKER_DB_PASSWORD",
    "corridor_legacy_dev": "CORRIDOR_LEGACY_DEV_DB_PASSWORD",
}

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


def _run_role_setup(body: str) -> None:
    """Apply cluster-role DDL idempotently, retrying catalog collisions.

    Concurrent migrations of different databases share one ``pg_authid``.  The
    guards make the second session's work a no-op, and the retry covers the
    window between a guard's check and its DDL.
    """

    op.execute(
        f"""
        do $$
        declare attempts int := 0;
        begin
            loop
                begin
                    {body}
                    exit;
                exception when others then
                    attempts := attempts + 1;
                    if attempts >= 10 then
                        raise;
                    end if;
                    perform pg_sleep(0.05 * attempts);
                end;
            end loop;
        end $$;
        """
    )


def upgrade() -> None:
    """Create the role matrix and replace PUBLIC execution with grants."""

    # Roles live in the cluster, not in one database, so every test worker
    # migrating its own database runs this same block concurrently.  Do the
    # work only when it is actually needed, and retry the narrow window where
    # two sessions still collide on pg_authid ("tuple concurrently updated").
    for role in OWNER_ROLES:
        _run_role_setup(
            f"""
            if not exists (select 1 from pg_roles where rolname = '{role}') then
                create role {role} nologin noinherit;
            end if;
            if exists (
                select 1 from pg_roles where rolname = '{role}'
                  and (rolcanlogin or rolinherit or rolsuper
                       or rolcreatedb or rolcreaterole)
            ) then
                alter role {role} nologin noinherit nosuperuser
                    nocreatedb nocreaterole;
            end if;
            """
        )

    for role, variable in LOGIN_ROLES.items():
        # The password is set once, at creation.  Re-setting it on every
        # migration would rewrite the catalog row every time and reintroduce
        # the collision this block exists to avoid; a deployment rotates
        # credentials out of band.
        password = _password(variable, role).replace("'", "''")
        _run_role_setup(
            f"""
            if not exists (select 1 from pg_roles where rolname = '{role}') then
                create role {role} login password '{password}'
                    nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
            end if;
            if exists (
                select 1 from pg_roles where rolname = '{role}'
                  and (not rolcanlogin or rolsuper or rolcreatedb
                       or rolcreaterole or rolinherit or rolbypassrls)
            ) then
                alter role {role} login nosuperuser nocreatedb nocreaterole
                    noinherit nobypassrls;
            end if;
            """
        )

    # No application login may borrow an owner role's authority.
    for owner in OWNER_ROLES:
        for role in LOGIN_ROLES:
            _run_role_setup(
                f"""
                if exists (
                    select 1 from pg_auth_members m
                    join pg_roles o on o.oid = m.roleid
                    join pg_roles k on k.oid = m.member
                    where o.rolname = '{owner}' and k.rolname = '{role}'
                ) then
                    revoke {owner} from {role};
                end if;
                """
            )

    # The public schema stops being a place anyone can create objects in.
    op.execute("revoke create on schema public from public")
    op.execute("grant usage on schema public to " + ", ".join(LOGIN_ROLES))

    # Every application login reads the record.
    op.execute(
        "grant select on all tables in schema public to "
        + ", ".join(LOGIN_ROLES)
    )
    op.execute(
        "grant usage, select on all sequences in schema public to "
        + ", ".join(LOGIN_ROLES)
    )

    # Ordinary working tables stay writable by the runtime capabilities; the
    # accepted and legacy accepted tables are then taken back below.
    op.execute(
        "grant insert, update, delete on all tables in schema public "
        "to corridor_web, corridor_worker, corridor_legacy_dev"
    )
    for table in ACCEPTED_TABLES + LEGACY_ACCEPTED_TABLES:
        op.execute(
            f"revoke insert, update, delete, truncate on {table} "
            f"from corridor_web, corridor_worker, corridor_legacy_dev"
        )
    # Legacy projects still need a write path in local and test environments,
    # and only there: a customer deployment does not create this role.
    for table in LEGACY_ACCEPTED_TABLES:
        op.execute(
            f"grant insert, update, delete on {table} to corridor_legacy_dev"
        )

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

    # A later command inherits the boundary instead of PUBLIC execution.
    op.execute(
        "alter default privileges in schema public "
        "revoke execute on functions from public"
    )


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
