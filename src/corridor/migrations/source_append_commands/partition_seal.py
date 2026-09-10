"""#676 A seal binds a scope to the transaction that earned it.

#531 sealed the effective scope and #657 sealed the declaration, and both
seals covered only the value they protected.  A seal that covers nothing but
its own payload proves the payload was once issued; it does not prove it was
issued *here*.  `corridor_web` may read both settings with `current_setting`
— no privilege stops it, and none should — so it could keep the genuine pair
and replay it with `set_config` in a later transaction on the same pooled
connection.  Verification recomputed the same digest over the same scope and
agreed.  No membership was re-proved and no `security definer` command ran.

That is the whole offboarding guarantee, undone: a principal whose roster
entry was deactivated could re-declare any scope the connection legitimately
held earlier in its life, and read that project's rows again.

Both seals therefore now cover PostgreSQL's own top-level transaction id.
The sealing side calls `pg_current_xact_id()`, which assigns one if the
transaction has none and returns the *top-level* id even from inside a
savepoint — which is what leaves #654's savepoint contract exactly as it
was, since a subtransaction that aborts does not give the top-level id back.
The verifying side calls `pg_current_xact_id_if_assigned()` and **fails
closed**: no id assigned, or an id that differs, is not a scope.  A replayed
pair meets a different transaction id, or none at all, and the recomputed
digest cannot match.

`transaction_timestamp()` was rejected: it is a clock reading, not an
identity, and two transactions can share one.  A caller-writable GUC holding
the id was rejected for the reason the defect exists: verification has to ask
PostgreSQL, not the caller.  `pgcrypto`'s HMAC is the cleaner MAC primitive
and the secret-and-SHA-256 construction is kept anyway, because a new
extension dependency is not needed to close a replay hole.

The material is domain-separated so a scope digest can never be read as a
declaration digest, and carries the database and the session login so a seal
is not portable between them.  Only the last field is free-form caller text,
so the concatenation stays unambiguous:

    scope:v2       | current_database | session_user | xid8 | scope
    declaration:v2 | current_database | session_user | xid8 | declaration

**The accepted cost.** Opening a project partition now assigns a real
transaction id even for an otherwise read-only request, which PostgreSQL
documents and which is the price of binding a seal to a transaction at all.
It is recorded here and in `corridor.access` rather than avoided, because
every way of avoiding it weakens the seal back to something replayable.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.partition_declaration import (
    CURRENT_PARTITION_DECLARATION,
    SEAL_PARTITION_DECLARATION,
)
from corridor.migrations.source_append_commands.project_partition import (
    create_or_replace,
    CURRENT_PROJECT_PARTITION,
    SEAL_PROJECT_PARTITION,
)


SEAL_PROJECT_PARTITION_676 = """
create or replace function public.seal_project_partition(p_scope text)
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
        encode(
            sha256(
                (
                    v_secret
                    || ':scope:v2:' || current_database()::text
                    || ':' || session_user::text
                    || ':' || pg_current_xact_id()::text
                    || ':' || p_scope
                )::bytea
            ),
            'hex'
        ),
        true
    );
end;
$$;
"""

# The two fail-closed checks are deliberately separate statements. Folding the
# missing transaction id into the digest comparison would make the recomputed
# digest null, and `null is distinct from null` is false — so a connection that
# set the scope and left the seal setting untouched would verify. The absence
# of an id is its own refusal.
CURRENT_PROJECT_PARTITION_676 = """
create or replace function public.current_project_partition()
returns bigint[]
language plpgsql
stable
security definer
as $$
declare
    v_scope text := current_setting('corridor.project_partition', true);
    v_seal text := current_setting('corridor.project_partition_seal', true);
    v_xid xid8 := pg_current_xact_id_if_assigned();
    v_secret text;
begin
    if v_scope is null then
        return null;
    end if;
    if v_xid is null then
        return null;
    end if;
    select secret into strict v_secret
      from public.project_partition_secrets
     where id = 1;
    if v_seal is distinct from
        encode(
            sha256(
                (
                    v_secret
                    || ':scope:v2:' || current_database()::text
                    || ':' || session_user::text
                    || ':' || v_xid::text
                    || ':' || v_scope
                )::bytea
            ),
            'hex'
        )
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

SEAL_PARTITION_DECLARATION_676 = """
create or replace function public.seal_partition_declaration(p_declaration text)
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
    perform set_config(
        'corridor.project_partition_declaration', p_declaration, true
    );
    perform set_config(
        'corridor.project_partition_declaration_seal',
        encode(
            sha256(
                (
                    v_secret
                    || ':declaration:v2:' || current_database()::text
                    || ':' || session_user::text
                    || ':' || pg_current_xact_id()::text
                    || ':' || p_declaration
                )::bytea
            ),
            'hex'
        ),
        true
    );
end;
$$;
"""

# A declaration with no transaction id behind it is a replayed declaration, and
# the declaration's way of failing closed is to raise: an unverifiable
# declaration refuses every further declaration in the transaction (#657), so
# returning null here would instead hand the replayer a clean slate.
CURRENT_PARTITION_DECLARATION_676 = """
create or replace function public.current_partition_declaration()
returns text
language plpgsql
stable
security definer
as $$
declare
    v_declaration text := nullif(
        current_setting('corridor.project_partition_declaration', true), ''
    );
    v_seal text := current_setting(
        'corridor.project_partition_declaration_seal', true
    );
    v_xid xid8 := pg_current_xact_id_if_assigned();
    v_secret text;
begin
    if v_declaration is null then
        return null;
    end if;
    if v_xid is null then
        raise exception
            'the project-authorization scope declared on this transaction is '
            'not one the database sealed'
            using errcode = '25000';
    end if;
    select secret into strict v_secret
      from public.project_partition_secrets
     where id = 1;
    if v_seal is distinct from
        encode(
            sha256(
                (
                    v_secret
                    || ':declaration:v2:' || current_database()::text
                    || ':' || session_user::text
                    || ':' || v_xid::text
                    || ':' || v_declaration
                )::bytea
            ),
            'hex'
        )
    then
        raise exception
            'the project-authorization scope declared on this transaction is '
            'not one the database sealed'
            using errcode = '25000';
    end if;
    return v_declaration;
end;
$$;
"""

# The pre-#676 bodies, re-issued so the owners and the grants #531 and #657
# set survive the downgrade untouched. `create_or_replace` carries the guard
# that each source holds exactly one `create function`, so no substitution here
# can land on the wrong statement.
SEAL_PROJECT_PARTITION_676_DOWN = create_or_replace(SEAL_PROJECT_PARTITION)
CURRENT_PROJECT_PARTITION_676_DOWN = create_or_replace(CURRENT_PROJECT_PARTITION)
SEAL_PARTITION_DECLARATION_676_DOWN = create_or_replace(SEAL_PARTITION_DECLARATION)
CURRENT_PARTITION_DECLARATION_676_DOWN = create_or_replace(
    CURRENT_PARTITION_DECLARATION
)

SEAL_BODIES_676 = (
    SEAL_PROJECT_PARTITION_676,
    CURRENT_PROJECT_PARTITION_676,
    SEAL_PARTITION_DECLARATION_676,
    CURRENT_PARTITION_DECLARATION_676,
)

SEAL_BODIES_676_DOWN = (
    SEAL_PROJECT_PARTITION_676_DOWN,
    CURRENT_PROJECT_PARTITION_676_DOWN,
    SEAL_PARTITION_DECLARATION_676_DOWN,
    CURRENT_PARTITION_DECLARATION_676_DOWN,
)


def upgrade(op) -> None:
    # Last, because it replaces two commands #531 creates and two more #657
    # creates, and every one of them has to already exist. `create or replace`
    # keeps each function's owner and grants, so the four gain the transaction
    # binding and change nothing else about who may call them.
    for body in SEAL_BODIES_676:
        op.execute(body)


def downgrade(op) -> None:
    # First, because the upgrade added it last. The four go back to the bodies
    # #531 and #657 gave them, which seal and verify without the transaction
    # id. Nothing sealed survives a transaction, so no stored value is lost
    # and no seal issued under either construction outlives the downgrade.
    for body in SEAL_BODIES_676_DOWN:
        op.execute(body)
