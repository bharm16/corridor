"""#531 Project authorization is a data partition, not a where clause.

ADR-0083 corrected ADR-0079: one database per customer does not make the
project a database boundary, so "projects remain authorization and
data-partition boundaries inside the customer database".  Until now that
boundary was ``_authorize`` in ``web/app.py`` plus a ``project_id ==`` in
every reader.  That is an application filter: one query written without the
predicate reads another project's rows and nothing anywhere refuses.

The partition is therefore moved into PostgreSQL.  The four project-scoped
spine relations the product reads carry row-level security, and the web
capability sees only the rows of the projects its *declared partition*
names.  A declared partition is not something the application can assert:
``open_project_partition`` proves an active roster entry for the principal
before it declares one, and the declaration is sealed with a secret that
lives in a table no runtime login can read.  A login that sets the setting
by hand produces a scope whose seal does not verify, and an unverified scope
is the empty scope.  So a forgotten ``where`` clause now returns nothing
instead of another customer project's rows.

Scope is transaction-local (``set_config(..., true)``), so a pooled
connection cannot carry one request's partition into the next, and a
rollback takes the partition with it.

The worker capability is deliberately *not* partitioned: a background run
carries no person's authorization to enforce, its isolation boundary is the
customer database (ADR-0079), and every command-line entry point in
``corridor`` reads whichever project it was pointed at.  The three command
roles are unpartitioned for a different reason: each already proves project
scope on every typed reference it touches, and partitioning them would break
the very commands that enforce scope.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.roles import (
    RUNTIME_LOGINS,
    SOURCE_APPEND_ROLE,
)


PARTITIONED_TABLES = (
    "source_segments",
    "facts",
    "extracted_proposals",
    "proposed_deltas",
)

# Every role that must see the whole customer database: the worker capability,
# the three command-owner roles, and the opt-in legacy development login where
# a deployment created one.
UNPARTITIONED_ROLES = (
    "corridor_worker",
    "corridor_source_append",
    "corridor_fact_decision_writer",
    "corridor_statement_retirement",
    "corridor_legacy_dev",
)

PARTITION_COMMANDS = {
    "current_project_partition": "()",
    "seal_project_partition": "(text)",
    "open_project_partition": "(text, bigint)",
    "open_member_project_partition": "(text)",
    "close_project_partition": "()",
}

PROJECT_PARTITION_SCHEMA = """
create table public.project_partition_secrets (
    id smallint not null,
    secret text not null,
    constraint pk_project_partition_secrets primary key (id),
    constraint ck_project_partition_secrets_singleton check (id = 1),
    constraint ck_project_partition_secrets_secret check (length(secret) >= 32)
);

comment on table public.project_partition_secrets is
    'The seal key for a declared project partition (#531). No runtime login '
    'holds any privilege on this table: a capability that could read it could '
    'forge a partition for a project nobody granted it.';

-- The schema owner carries default privileges that hand every new table to the
-- runtime logins (`alter default privileges ... grant`), so this table has to
-- take them back explicitly. Creating it grants nothing on purpose; inheriting
-- a blanket grant would make the seal readable and every partition forgeable.
do $$
declare
    v_roles text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ('corridor_web', 'corridor_worker', 'corridor_legacy_dev');
    if v_roles is not null then
        execute format(
            'revoke all on public.project_partition_secrets from %s', v_roles
        );
    end if;
end $$;

insert into public.project_partition_secrets (id, secret)
values (1, encode(sha256((gen_random_uuid()::text || clock_timestamp()::text)::bytea), 'hex'));
"""

PROJECT_PARTITION_SCHEMA_DOWN = """
drop table if exists public.project_partition_secrets;
"""

# The seal is derived, never stored per session, so declaring a partition
# writes nothing and costs no row.  ``p_scope`` is the canonical
# comma-separated ascending id list; the empty string is the empty partition,
# which is what an offboarded person's connection gets.
SEAL_PROJECT_PARTITION = """
create function public.seal_project_partition(p_scope text)
returns void
language plpgsql
security definer
as $$
declare
    v_secret text;
begin
    select secret into strict v_secret
      from public.project_partition_secrets
     where id = 1;
    perform set_config('corridor.project_partition', p_scope, true);
    perform set_config(
        'corridor.project_partition_seal',
        encode(sha256((v_secret || ':' || p_scope)::bytea), 'hex'),
        true
    );
end;
$$;
"""

# Null and the empty array both refuse every row.  Returning null for an
# unverified seal rather than raising keeps the policy cheap and keeps the
# failure uniform: an unset partition and a forged one are the same partition.
CURRENT_PROJECT_PARTITION = """
create function public.current_project_partition()
returns bigint[]
language plpgsql
stable
security definer
as $$
declare
    v_scope text := current_setting('corridor.project_partition', true);
    v_seal text := current_setting('corridor.project_partition_seal', true);
    v_secret text;
begin
    if v_scope is null then
        return null;
    end if;
    select secret into strict v_secret
      from public.project_partition_secrets
     where id = 1;
    if v_seal is distinct from
        encode(sha256((v_secret || ':' || v_scope)::bytea), 'hex')
    then
        return null;
    end if;
    if v_scope = '' then
        return array[]::bigint[];
    end if;
    return string_to_array(v_scope, ',')::bigint[];
end;
$$;
"""

OPEN_PROJECT_PARTITION = """
create function public.open_project_partition(
    p_principal_subject text,
    p_project_id bigint
)
returns bigint
language plpgsql
security definer
as $$
begin
    if not exists (
        select 1
          from public.project_roster_entries
         where project_id = p_project_id
           and principal_subject = p_principal_subject
           and active
    ) then
        raise exception
            'principal % holds no active membership of project %',
            p_principal_subject, p_project_id
            using errcode = '42501';
    end if;
    perform public.seal_project_partition(p_project_id::text);
    return p_project_id;
end;
$$;
"""

# The cross-project reading (#537) needs a partition too, and the honest one is
# every project this person is currently on.  A person with no active
# membership left declares the empty partition, which is exactly what
# offboarding should leave behind: a live connection that can still be
# authenticated but can read no project's rows.
OPEN_MEMBER_PROJECT_PARTITION = """
create function public.open_member_project_partition(p_principal_subject text)
returns bigint[]
language plpgsql
security definer
as $$
declare
    v_ids bigint[];
begin
    select coalesce(array_agg(project_id order by project_id), array[]::bigint[])
      into v_ids
      from public.project_roster_entries
     where principal_subject = p_principal_subject
       and active;
    perform public.seal_project_partition(array_to_string(v_ids, ','));
    return v_ids;
end;
$$;
"""

CLOSE_PROJECT_PARTITION = """
create function public.close_project_partition()
returns void
language plpgsql
security definer
as $$
begin
    perform set_config('corridor.project_partition', '', true);
    perform set_config('corridor.project_partition_seal', '', true);
end;
$$;
"""

# ``seal_project_partition`` is the one command that declares a partition
# without proving anything, so it is never granted: only the three commands
# above call it, and they run as its owner.
PARTITION_COMMANDS_GRANTED = (
    "current_project_partition",
    "open_project_partition",
    "open_member_project_partition",
    "close_project_partition",
)

_PARTITIONED_TABLES_SQL = ", ".join(f"'{table}'" for table in PARTITIONED_TABLES)
_UNPARTITIONED_ROLES_SQL = ", ".join(f"'{role}'" for role in UNPARTITIONED_ROLES)

PROJECT_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array[{_PARTITIONED_TABLES_SQL}] loop
        execute format(
            'alter table public.%I enable row level security', v_table
        );
        execute format(
            'create policy %I on public.%I for all to corridor_web '
            'using (project_id = any(public.current_project_partition())) '
            'with check (project_id = any(public.current_project_partition()))',
            'p_' || v_table || '_project_partition', v_table
        );
        if v_roles is not null then
            execute format(
                'create policy %I on public.%I for all to %s '
                'using (true) with check (true)',
                'p_' || v_table || '_unpartitioned', v_table, v_roles
            );
        end if;
    end loop;
end $$;
"""

PROJECT_PARTITION_POLICIES_DOWN = f"""
do $$
declare
    v_table text;
begin
    foreach v_table in array array[{_PARTITIONED_TABLES_SQL}] loop
        execute format(
            'drop policy if exists %I on public.%I',
            'p_' || v_table || '_project_partition', v_table
        );
        execute format(
            'drop policy if exists %I on public.%I',
            'p_' || v_table || '_unpartitioned', v_table
        );
        execute format(
            'alter table public.%I disable row level security', v_table
        );
    end loop;
end $$;
"""


def create_or_replace(body: str) -> str:
    """A released function body, re-issued rather than dropped and recreated.

    A downgrade that hands back an earlier body must not drop the function
    first: dropping it takes the owner and the grants that were set on it. Each
    source constant holds exactly one ``create function``, which this refuses to
    assume, so the single substitution cannot land on the wrong statement.
    """

    if body.count("create function") != 1:
        raise ValueError("a re-issued body must hold exactly one create function")
    return body.replace("create function", "create or replace function", 1)


def upgrade(op) -> None:
    # Last, because the policies it creates reference the spine tables every
    # block above builds, and the commands it creates read the roster.
    op.execute(PROJECT_PARTITION_SCHEMA)
    op.execute(f"grant select on public.project_partition_secrets to {SOURCE_APPEND_ROLE}")
    op.execute(
        f"grant select on public.project_roster_entries to {SOURCE_APPEND_ROLE}"
    )
    for body in (
        SEAL_PROJECT_PARTITION,
        CURRENT_PROJECT_PARTITION,
        OPEN_PROJECT_PARTITION,
        OPEN_MEMBER_PROJECT_PARTITION,
        CLOSE_PROJECT_PARTITION,
    ):
        op.execute(body)
    for name, signature in PARTITION_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {SOURCE_APPEND_ROLE}"
        )
        # PostgreSQL grants EXECUTE to PUBLIC on every new function (#545), so
        # each command takes PUBLIC back before anything is granted at all.
        op.execute(f"revoke all on function public.{name}{signature} from public")
    for name in PARTITION_COMMANDS_GRANTED:
        op.execute(
            f"grant execute on function public.{name}{PARTITION_COMMANDS[name]} "
            f"to {RUNTIME_LOGINS}"
        )
    op.execute(PROJECT_PARTITION_POLICIES)


def downgrade(op) -> None:
    # First, because the upgrade added it last. Nothing is lost: the partition
    # holds no data of its own, and the seal it drops is regenerated whenever
    # the block is applied again.
    op.execute(PROJECT_PARTITION_POLICIES_DOWN)
    for name, signature in PARTITION_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    op.execute(
        f"revoke select on public.project_roster_entries from {SOURCE_APPEND_ROLE}"
    )
    op.execute(PROJECT_PARTITION_SCHEMA_DOWN)
