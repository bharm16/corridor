"""One delivery ledger for both transports (#599, ADR-0089).

Folded into this transition for the same window reason as the blocks above.

ADR-0083 declared the ``SourceEnvelope`` the one normalized ingress record
every channel produces, pulled or pushed.  The implementation did not come
out that way: the #511 block above persists the push half and #496 built the
pull half with no delivery record at all, so "did we take delivery of this
external version" was a database read on one transport and a re-listing of
the customer's own system on the other, and a delivery the intake gate (#490)
refused was recorded for push and lost for pull.  ADR-0089 calls that an
accidental implementation artifact rather than a domain distinction.

``push_deliveries`` therefore *becomes* the shared family rather than gaining
a sibling beside it: a second table would make the asymmetry permanent and
leave every reader — the intake operator's "what arrived this week", the
shadow comparison of #499, the disposition inventory of #514, the analytics
contract of #558 — to union two tables and reconcile two identity rules.  The
rename carries every existing row and every foreign key that names it, which
is what makes the reconciliation exact rather than a copy somebody has to
check.  It is checked anyway, below.

The identity gains the disposition and nothing else.  A delivery may be
stored and later found duplicate, or refused and never stored, and each of
those is one outcome of the same delivery; what may not happen is the same
outcome of the same delivery twice.  ``uq_push_delivery_idempotency`` is
retired rather than widened: #457's own finding was that a key computed in
Python and never checked against its row constrains only the writers that
remember to compute it, and the structural key is what does the work.  The
key itself stays, and the trigger that re-derives ADR-0083's two digests from
the row's own columns stays with it, now under the family's name.

``connector_checkpoint_advances`` takes the cursor off the Due Work receipt.
#488 kept it there deliberately — this file's own predecessor rejected a
checkpoint table because the window was closed — and mitigated the
consequence by retaining those receipts for 3650 days.  Retention is not
identity: a receipt sweep, a retention-policy change, a disposition under
ADR-0080, or an ordinary cleanup would reset a live connector's external
cursor or land it on a stale token.  The advance relation is append-only, the
current checkpoint is derived from its newest row, and deleting a receipt can
no longer move anything.

``connector_checkpoint_advance_deliveries`` is where ADR-0089's checkpoint
rule is enforced rather than remembered.  An advance may cover a ``stored``
or ``duplicate`` delivery freely; it may cover a ``terminally_refused`` or
``quarantined`` one only because the digest and the refusal evidence are
durably here, which is what makes advancing past bytes nobody will ever store
safe; and it may never cover a ``transient_failure``, because a scanner that
timed out, an object store that rejected a write, or a provider that returned
a 500 has said nothing about the delivery, and advancing past it drops a
source revision silently.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


UNIFIED_DELIVERY_SCHEMA = """
alter table public.push_deliveries rename to source_deliveries;

alter sequence public.push_deliveries_id_seq rename to source_deliveries_id_seq;

alter index public.ix_push_deliveries_project_id
    rename to ix_source_deliveries_project_id;

alter table public.source_deliveries
    rename constraint push_deliveries_pkey to source_deliveries_pkey;

alter table public.source_deliveries
    rename constraint push_deliveries_credential_id_fkey
    to source_deliveries_credential_id_fkey;

alter table public.source_deliveries
    rename constraint push_deliveries_project_id_fkey
    to source_deliveries_project_id_fkey;

alter table public.source_deliveries
    rename constraint ck_push_delivery_content_sha256
    to ck_source_delivery_content_sha256;

alter table public.source_deliveries
    rename constraint ck_push_delivery_identity to ck_source_delivery_identity;

alter table public.source_deliveries
    rename constraint ck_push_delivery_idempotency
    to ck_source_delivery_idempotency;

alter table public.source_deliveries
    drop constraint uq_push_delivery_idempotency;

alter table public.source_deliveries
    drop constraint uq_push_deliveries_envelope;

alter table public.source_deliveries
    add column transport character varying(8) not null default 'push',
    add column configuration_identity text not null default '',
    add column configuration_version text not null default '',
    add column service_identity text not null default '',
    add column run_identity text not null default '',
    add column disposition character varying(24) not null default 'stored',
    add column refusal_reason text;

-- Every existing row is a pushed delivery whose bytes were taken, bound by
-- the credential that admitted it, and recorded by this transition rather
-- than by a run that ever existed.  Nothing is invented that the row does not
-- already say.
update public.source_deliveries
   set configuration_identity = 'credential:' || credential_id,
       service_identity = 'corridor.push_intake',
       run_identity = 'migration:b2d5f8a1c4e7';

alter table public.source_deliveries
    alter column transport drop default,
    alter column configuration_identity drop default,
    alter column service_identity drop default,
    alter column run_identity drop default,
    alter column disposition drop default;

alter table public.source_deliveries alter column credential_id drop not null;

alter table public.source_deliveries
    add constraint uq_source_deliveries_observation
    unique (project_id, delivery_identity, content_sha256, disposition);

alter table public.source_deliveries
    add constraint ck_source_delivery_transport
        check (transport in ('pull', 'push')),
    add constraint ck_source_delivery_disposition
        check (disposition in ('stored', 'duplicate', 'quarantined',
                               'terminally_refused', 'transient_failure')),
    add constraint ck_source_delivery_push_credential
        check ((transport = 'push') = (credential_id is not null)),
    add constraint ck_source_delivery_configuration
        check (length(btrim(configuration_identity)) > 0),
    add constraint ck_source_delivery_run
        check (length(btrim(service_identity)) > 0
               and length(btrim(run_identity)) > 0),
    add constraint ck_source_delivery_refusal_reason
        check ((disposition in ('stored', 'duplicate'))
               = (refusal_reason is null));

drop trigger trg_push_deliveries_identity on public.source_deliveries;

drop function public.enforce_push_delivery_identity();

create function public.enforce_source_delivery_identity() returns trigger
    language plpgsql
    as $$
        declare
            bound_slug text;
            derived_identity text;
            derived_key text;
        begin
            select slug into bound_slug from projects where id = new.project_id;
            if bound_slug is null then
                raise exception 'source_delivery:unbound_delivery a delivery names a project that does not exist'
                    using errcode='23514';
            end if;
            derived_identity := encode(sha256(convert_to(concat_ws(':',
                new.customer, bound_slug, new.channel,
                new.external_identity, new.external_version), 'UTF8')), 'hex');
            derived_key := encode(sha256(convert_to(concat_ws(':',
                derived_identity, new.content_sha256), 'UTF8')), 'hex');
            if new.delivery_identity is distinct from derived_identity then
                raise exception 'source_delivery:delivery_identity a delivery identity is derived from the binding and the transport, never supplied'
                    using errcode='23514';
            end if;
            if new.idempotency_key is distinct from derived_key then
                raise exception 'source_delivery:idempotency_key a delivery idempotency key is derived from its identity and its bytes, never supplied'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_source_deliveries_identity
    before insert on public.source_deliveries
    for each row execute function public.enforce_source_delivery_identity();

create table public.connector_checkpoint_advances (
    id bigserial primary key,
    project_id bigint not null references public.projects(id),
    schedule_id bigint not null references public.due_work_schedules(id),
    configuration_identity text not null,
    configuration_version text not null default '',
    channel character varying(32) not null,
    checkpoint_token text not null,
    service_identity text not null,
    run_identity text not null,
    advanced_at timestamp with time zone not null default now(),
    constraint uq_connector_checkpoint_advance
        unique (schedule_id, run_identity, checkpoint_token),
    constraint ck_connector_checkpoint_token
        check (length(btrim(checkpoint_token)) > 0),
    constraint ck_connector_checkpoint_run
        check (length(btrim(service_identity)) > 0
               and length(btrim(run_identity)) > 0)
);

create index ix_connector_checkpoint_advances_project_id
    on public.connector_checkpoint_advances (project_id);

create index ix_connector_checkpoint_advances_schedule_id
    on public.connector_checkpoint_advances (schedule_id);

create table public.connector_checkpoint_advance_deliveries (
    id bigserial primary key,
    advance_id bigint not null
        references public.connector_checkpoint_advances(id),
    delivery_id bigint not null references public.source_deliveries(id),
    constraint uq_connector_checkpoint_advance_delivery
        unique (advance_id, delivery_id)
);

create index ix_connector_checkpoint_advance_deliveries_advance_id
    on public.connector_checkpoint_advance_deliveries (advance_id);

create function public.enforce_checkpoint_advance_coverage() returns trigger
    language plpgsql
    as $$
        declare
            covered public.source_deliveries%rowtype;
        begin
            select * into covered from source_deliveries
             where id = new.delivery_id;
            if covered.disposition = 'transient_failure' then
                raise exception 'source_delivery:transient_failure a checkpoint never advances past a delivery one attempt failed to take'
                    using errcode='23514';
            end if;
            if covered.disposition in ('terminally_refused', 'quarantined')
               and coalesce(btrim(covered.refusal_reason), '') = '' then
                raise exception 'source_delivery:missing_refusal_evidence a refused delivery is advanced past only on its recorded evidence'
                    using errcode='23514';
            end if;
            if covered.disposition = 'quarantined'
               and coalesce(btrim(covered.bytes_reference), '') = '' then
                raise exception 'source_delivery:missing_quarantine_reference a quarantined delivery is advanced past only where its bytes are held'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_connector_checkpoint_advance_coverage
    before insert on public.connector_checkpoint_advance_deliveries
    for each row execute function public.enforce_checkpoint_advance_coverage();
"""

UNIFIED_DELIVERY_SCHEMA_DOWN = """
drop trigger if exists trg_connector_checkpoint_advance_coverage
    on public.connector_checkpoint_advance_deliveries;

drop function if exists public.enforce_checkpoint_advance_coverage() cascade;

drop table if exists public.connector_checkpoint_advance_deliveries;

drop table if exists public.connector_checkpoint_advances;

alter table public.source_deliveries
    drop constraint if exists ck_source_delivery_refusal_reason,
    drop constraint if exists ck_source_delivery_run,
    drop constraint if exists ck_source_delivery_configuration,
    drop constraint if exists ck_source_delivery_push_credential,
    drop constraint if exists ck_source_delivery_disposition,
    drop constraint if exists ck_source_delivery_transport,
    drop constraint if exists uq_source_deliveries_observation;

alter table public.source_deliveries
    drop column if exists refusal_reason,
    drop column if exists disposition,
    drop column if exists run_identity,
    drop column if exists service_identity,
    drop column if exists configuration_version,
    drop column if exists configuration_identity,
    drop column if exists transport;

alter table public.source_deliveries alter column credential_id set not null;

alter table public.source_deliveries
    rename constraint ck_source_delivery_idempotency
    to ck_push_delivery_idempotency;

alter table public.source_deliveries
    rename constraint ck_source_delivery_identity to ck_push_delivery_identity;

alter table public.source_deliveries
    rename constraint ck_source_delivery_content_sha256
    to ck_push_delivery_content_sha256;

alter table public.source_deliveries
    rename constraint source_deliveries_project_id_fkey
    to push_deliveries_project_id_fkey;

alter table public.source_deliveries
    rename constraint source_deliveries_credential_id_fkey
    to push_deliveries_credential_id_fkey;

alter table public.source_deliveries
    rename constraint source_deliveries_pkey to push_deliveries_pkey;

alter index public.ix_source_deliveries_project_id
    rename to ix_push_deliveries_project_id;

alter sequence public.source_deliveries_id_seq rename to push_deliveries_id_seq;

alter table public.source_deliveries rename to push_deliveries;

-- The guard is renamed rather than rebuilt: it re-derives ADR-0083's digests
-- from the row's own columns and that is unchanged by this block, so the
-- #457 block below finds exactly the trigger and function it created.
alter function public.enforce_source_delivery_identity()
    rename to enforce_push_delivery_identity;

alter trigger trg_source_deliveries_identity on public.push_deliveries
    rename to trg_push_deliveries_identity;

alter table public.push_deliveries
    add constraint uq_push_delivery_idempotency unique (idempotency_key);

alter table public.push_deliveries
    add constraint uq_push_deliveries_envelope
    unique (project_id, delivery_identity, content_sha256);
"""

# The tables the runtime capabilities append to and never rewrite, on the same
# terms the #511 block set for the delivery ledger it renames: a delivery, an
# advance, and the coverage that makes the advance safe are records of what
# happened, so no runtime capability may edit or erase one.
UNIFIED_DELIVERY_TABLES = (
    "connector_checkpoint_advances",
    "connector_checkpoint_advance_deliveries",
)


def _refuse_unrepresentable_delivery_downgrade(bind) -> None:
    """Refuse rather than lose a delivery the push-only shape cannot hold.

    The unified family records pulled deliveries and refused ones; the push
    ledger this downgrade restores can express neither, and dropping the rows
    that do not fit is exactly the loss ADR-0089 set out to remove.  The same
    discipline as #512's downgrade: state what cannot be carried back, and stop.
    """

    blocked = bind.execute(
        sa.text(
            "select count(*) from source_deliveries "
            " where transport <> 'push' or disposition <> 'stored'"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#599 downgrade refuses: {blocked} delivery row(s) are pulled, "
            "refused, or failed, and the push-only ledger cannot represent "
            "them. Nothing is merged or dropped here."
        )


def _push_delivery_snapshot(bind) -> tuple[tuple, ...]:
    """Every push delivery's identity, read before the family is established."""

    return tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                "select id, project_id, delivery_identity, idempotency_key, "
                "       content_sha256, credential_id "
                "  from push_deliveries order by id"
            )
        ).all()
    )


def _reconcile_unified_delivery_family(bind, snapshot) -> None:
    """Prove the renamed family carries every push delivery exactly, or refuse.

    The rename is exact by construction, which is why it was chosen over a copy
    — but "by construction" is the claim and not the proof, and both #512's
    backfill and #457's detections established that a transition states what it
    expects and aborts when the database disagrees.  Nothing is merged or
    dropped here: a mismatch means the unified family cannot be established over
    these rows, and which row is the record is a question only a person can
    answer.
    """

    migrated = tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                "select id, project_id, delivery_identity, idempotency_key, "
                "       content_sha256, credential_id "
                "  from source_deliveries order by id"
            )
        ).all()
    )
    if migrated != snapshot:
        raise RuntimeError(
            "#599 delivery-family migration refuses: the unified family does "
            f"not reproduce the {len(snapshot)} push delivery row(s) it was "
            f"established over — {len(migrated)} row(s) came back and their "
            "identities do not match. Nothing is merged or dropped here: "
            "reconcile the deliveries as an attributable act first."
        )
    unmigrated = bind.execute(
        sa.text(
            "select count(*) from source_deliveries "
            " where transport <> 'push' or disposition <> 'stored' "
            "    or credential_id is null "
            "    or length(btrim(configuration_identity)) = 0"
        )
    ).scalar_one()
    if unmigrated:
        raise RuntimeError(
            f"#599 delivery-family migration refuses: {unmigrated} existing "
            "delivery row(s) did not take the pushed, stored, credential-bound "
            "identity every one of them already had."
        )


def upgrade(op) -> None:
    # After #457, because it renames and re-keys the family that block just
    # constrained, and because the identity it establishes is the one #457
    # proved these rows already satisfy.
    delivery_snapshot = _push_delivery_snapshot(op.get_bind())
    op.execute(UNIFIED_DELIVERY_SCHEMA)
    _reconcile_unified_delivery_family(op.get_bind(), delivery_snapshot)
    for table in UNIFIED_DELIVERY_TABLES:
        # The same terms the #511 block set for the delivery ledger this block
        # renames: an advance and the coverage that makes it safe are records
        # of what happened, so a runtime capability appends one and can never
        # edit or erase one.
        op.execute(f"grant select, insert on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
        op.execute(
            f"revoke update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )


def downgrade(op) -> None:
    # Next again, third from the end of the upgrade: the family goes back to
    # being the push half alone, and the #457 block below then unwinds the
    # constraints it re-establishes on the restored name.
    _refuse_unrepresentable_delivery_downgrade(op.get_bind())
    for table in UNIFIED_DELIVERY_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select, insert on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(UNIFIED_DELIVERY_SCHEMA_DOWN)
