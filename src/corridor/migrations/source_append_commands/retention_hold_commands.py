"""A retention hold is placed and lifted through commands, never a raw write (#956).

Folded into this transition for the same window reason as the blocks around
it: ``corridor.migrations.policy`` allows one unreleased transition and this is
it.

The baseline granted ``corridor_web`` and ``corridor_worker`` full DML on
``retention_holds``. #949 then made a hold the issuer of the storage-deletion
permit and ordered a hold against a running sign-in batch, and recorded three
gaps it left open (its own three closing questions). Two of them live here.

**The worker must not be able to remove the restriction (#956 B).** A hold
suspends deletion; the worker's job is to *observe* holds and execute the
deletion a hold does not forbid, so it needs ``SELECT`` on the table and
nothing else. Leaving it ``INSERT``/``UPDATE``/``DELETE`` is what left a
hand-written ``UPDATE retention_holds SET lifted_at = now()`` -- a "lift any
hold" -- inside the worker's own authority. This revision revokes every write
on ``retention_holds`` from both runtime logins and replaces them with two
``SECURITY DEFINER`` commands owned by a dedicated least-privilege role,
``corridor_retention_hold``:

- ``place_retention_hold`` takes the ``retention-hold-ordering`` boundary
  (ADR-0102) as its first act, so the acknowledgement its caller prints is
  earned rather than assumed, validates the actor and reason, and appends one
  hold or returns the active one.
- ``lift_retention_hold`` records the separate attributable act that resumes
  retention work.

Both are granted to ``corridor_web`` alone -- placing and lifting a hold are
attributable human acts, not machine maintenance -- so the worker cannot call
either. The command owner is a non-login role, mirroring the source-append and
record-decision owners, so executing a command is never a login credential and
the schema owner's rights are never lent to the caller.

**An object-store deletion outcome can be uncertain (#956 D).** A processing
artifact's content is the object in the store, and an external delete is not
undone by a database rollback. ``processing_artifacts`` gains
``deletion_uncertain_at``: when the store cannot confirm a delete, retention
records the outcome as uncertain until reconciled rather than pretending it was
deleted or rolled back, and reconciliation resolves it later against the store.

The downgrade drops the commands, hands the raw writes back to the runtime
logins, and drops the column. It leaves the ``corridor_retention_hold`` role in
place, because the role is cluster-global and may serve another customer
database in the same cluster -- the same reason ``legacy_history`` leaves
``corridor_history_operations`` behind.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS

# The dedicated non-login command owner. Migrations inline every role name in
# SQL text (a revision is replayed released history), and
# ``tests/test_vocabulary_owners.py`` pairs this migration-only copy with the
# owned set rather than asserting there is only one.
RETENTION_HOLD_ROLE = "corridor_retention_hold"

# The one environment-level ordering boundary a hold and a deletion batch both
# pass through (ADR-0102). The literal matches ``retention.HOLD_ORDERING_LOCK``
# and ``retention.take_hold_ordering_lock``; the bigint key is the same
# ``hashtextextended`` idiom, so a command and a batch contend on one lock.
HOLD_ORDERING_LOCK = "retention-hold-ordering"


PLACE_RETENTION_HOLD = f"""
create function public.place_retention_hold(
    p_project_id bigint,
    p_reason text,
    p_principal text
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing_id bigint;
            new_id bigint;
        begin
            perform pg_advisory_xact_lock(
                hashtextextended('{HOLD_ORDERING_LOCK}', 0)
            );
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'a retention hold names the human who placed it'
                    using errcode = '23514';
            end if;
            if p_reason is null or length(btrim(p_reason)) = 0 then
                raise exception 'a retention hold requires a reason'
                    using errcode = '23514';
            end if;
            select id into existing_id from retention_holds
             where project_id = p_project_id and lifted_at is null
             limit 1;
            if found then
                return existing_id;
            end if;
            insert into retention_holds (project_id, reason, placed_by)
            values (p_project_id, btrim(p_reason), p_principal)
            returning id into new_id;
            return new_id;
        end; $$;
"""


LIFT_RETENTION_HOLD = """
create function public.lift_retention_hold(
    p_hold_id bigint,
    p_principal text,
    p_lifted_at timestamp with time zone
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'lifting a retention hold names the human who lifted it'
                    using errcode = '23514';
            end if;
            select id, lifted_at into existing
              from retention_holds where id = p_hold_id for update;
            if not found then
                raise exception 'retention hold does not exist'
                    using errcode = '23514';
            end if;
            if existing.lifted_at is null then
                update retention_holds
                   set lifted_by = p_principal,
                       lifted_at = coalesce(p_lifted_at, now())
                 where id = p_hold_id;
            end if;
            return p_hold_id;
        end; $$;
"""


COMMANDS = {
    "place_retention_hold": "(bigint, text, text)",
    "lift_retention_hold": "(bigint, text, timestamp with time zone)",
}


def _ensure_role(op) -> None:
    """Converge on the exact winner of a cluster-global CREATE ROLE race.

    Mirrors ``legacy_history._ensure_operations_role``: a role is a
    cluster-level object, so two customer databases migrating at once race on
    the name, and the loser must read the catalog before treating the collision
    as success.
    """

    connection = op.get_bind()
    exists = text(
        f"select exists(select 1 from pg_roles where rolname = '{RETENTION_HOLD_ROLE}')"
    )
    if not connection.scalar(exists):
        try:
            with connection.begin_nested():
                connection.exec_driver_sql(
                    f"create role {RETENTION_HOLD_ROLE} nologin noinherit "
                    "nosuperuser nocreatedb nocreaterole nobypassrls noreplication"
                )
        except DBAPIError as exc:
            original = exc.orig
            code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
            raced = code == "42710" or (
                code == "23505" and constraint == "pg_authid_rolname_index"
            )
            if not raced or not connection.scalar(exists):
                raise
    if connection.scalar(
        text(
            f"select exists(select 1 from pg_roles where rolname = '{RETENTION_HOLD_ROLE}'"
            " and (rolsuper or rolcreaterole or rolcreatedb or rolcanlogin"
            " or rolbypassrls or rolreplication))"
        )
    ):
        raise RuntimeError(
            "the retention-hold command owner must be a non-login least-privilege role"
        )


def upgrade(op) -> None:
    # After every feature block and before the sibling transitions: it alters
    # `processing_artifacts` (baseline) and `retention_holds` (baseline), both
    # created well before here, and nothing later in the revision names either.
    _ensure_role(op)

    # The command owner reads and writes the hold table and nothing else.
    op.execute(f"grant usage on schema public to {RETENTION_HOLD_ROLE}")
    op.execute(
        f"grant select, insert, update on public.retention_holds to {RETENTION_HOLD_ROLE}"
    )
    op.execute(
        "grant usage, select on sequence public.retention_holds_id_seq "
        f"to {RETENTION_HOLD_ROLE}"
    )

    op.execute(PLACE_RETENTION_HOLD)
    op.execute(LIFT_RETENTION_HOLD)
    for name, signature in COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {RETENTION_HOLD_ROLE}"
        )
        # PostgreSQL grants EXECUTE to PUBLIC on every new function, and a
        # default-privilege revoke does not persist (#545); each command takes
        # PUBLIC back itself and grants only the human capability.
        op.execute(f"revoke all on function public.{name}{signature} from public")
        op.execute(
            f"grant execute on function public.{name}{signature} to corridor_web"
        )

    # The runtime logins keep only SELECT: they read holds to refuse deletion,
    # and can no longer write one -- neither placing nor the "lift any hold"
    # #949 recorded as still inside the worker's authority.
    op.execute(
        f"revoke insert, update, delete, truncate on public.retention_holds "
        f"from {RUNTIME_LOGINS}"
    )

    # An object-store deletion whose acknowledgement is lost is uncertain until
    # reconciled (#956 D). The runtime logins already hold UPDATE on
    # `processing_artifacts`, so the column needs no new grant.
    op.execute(
        "alter table public.processing_artifacts "
        "add column deletion_uncertain_at timestamp with time zone"
    )


def downgrade(op) -> None:
    # Before the sibling transitions unwind, mirroring the upgrade. The role is
    # left in place: it is cluster-global and may serve another customer
    # database, the same reason `legacy_history` leaves its operations role.
    op.execute(
        "alter table public.processing_artifacts drop column deletion_uncertain_at"
    )
    for name, signature in COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    op.execute(
        f"grant insert, update, delete on public.retention_holds to {RUNTIME_LOGINS}"
    )
    op.execute(
        f"revoke all on public.retention_holds from {RETENTION_HOLD_ROLE}"
    )
    op.execute(
        "revoke all on sequence public.retention_holds_id_seq "
        f"from {RETENTION_HOLD_ROLE}"
    )
    op.execute(f"revoke usage on schema public from {RETENTION_HOLD_ROLE}")
