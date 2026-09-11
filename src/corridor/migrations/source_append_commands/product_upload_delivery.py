"""A product upload joins the delivery family, and its confirmation (#823).

Folded into this transition for the same window reason as the blocks above.

The ``unified_delivery`` block made one ledger of both transports, and a
product upload still could not be written to it.  Three constraints stopped it
and only one of them was really in the way: ``ck_source_delivery_transport``
admits ``pull`` and ``push`` and an upload *is* a push — somebody hands
Corridor bytes it never asked for — but ``ck_source_delivery_push_credential``
required every push to name a ``push_intake_credentials`` row, and a signed-in
person holds no such row.  The transport did not authenticate that delivery;
the web session did.

**The constraint asks how, instead of assuming.**
``ck_source_delivery_authentication`` replaces it: a pull is bound by its
connector configuration and authenticates neither way, and a push names
exactly one of the two things that can admit it — the credential the transport
presented, or the person who handed the bytes over.  A push naming neither is
refused by the database rather than by whichever writer remembered to check.

What was considered and rejected, in the order the alternatives present
themselves:

- **Mint a push credential for the uploader.**  It needs no schema change at
  all, which is its whole appeal.  It also means issuing a live alias token or
  webhook secret bound to the project for every person who uploads a file: a
  real door into the customer's project, created so that a row could be
  written.  ``ck_push_intake_credential_channel`` is therefore left exactly as
  it is, admitting ``project_alias``, ``shared_mailbox`` and ``webhook`` and no
  upload channel, and this block does not touch it.
- **Add a third transport.**  ADR-0089 describes pull and push and an upload is
  neither a new kind of arrival nor a new identity rule; widening the transport
  enum would be a domain amendment smuggled in as a schema edit.
- **Record the upload as a pull.**  It would state that a connector
  configuration fetched the file on a cursor, which is the second definition of
  one identity ADR-0089 exists to remove.

**Confirmation is its own relation.**  ``source_delivery_confirmations``
records one person admitting one stored delivery to processing.  The two acts
were conflated before because for a machine push they happen together, and for
an upload they do not: ``stored`` means Corridor holds the exact bytes, from
the moment they arrive and whether or not anybody has decided they should be
read, and a person looking at an upload preview is standing in the gap between
the two.  Without the relation an upload staged and abandoned was
indistinguishable from one whose registration failed, and issue coverage read
both as an unread source with no receipt.  One row per delivery, so replaying
a confirmation converges on the act already recorded instead of attributing
the same admission twice; append-only, like every other receipt here, because
a later correction is an act against the Document and never an edit of who
admitted what and when.  The delivery is named with its project through the
composite key ``uq_source_deliveries_row`` already provides, so a confirmation
recorded in one project cannot name another customer's delivery (#675).

**Grants and partition.**  The relation is project-scoped, so it answers the
partition the way the record tables do (#657): the policy name matches
``p_%_project_partition`` and it is recorded in
``access.PARTITIONED_RELATIONS``.  Both runtime logins read and append it —
``corridor_web`` because the confirmation is a web act, ``corridor_worker``
because the captures that register a delivered revision run there — and
neither may edit or erase one, on the same terms the delivery ledger itself
was granted.

The downgrade refuses while a human-authenticated delivery exists.  The
predecessor requires every push to name a credential, so carrying such a row
back would mean inventing one; the same discipline as the blocks above, which
state what cannot be represented and stop rather than dropping the rows that
do not fit.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS

CONFIRMATION_TABLE = "source_delivery_confirmations"

PRODUCT_UPLOAD_DELIVERY_SCHEMA = f"""
alter table public.source_deliveries
    add column delivered_by_principal text;

alter table public.source_deliveries
    drop constraint ck_source_delivery_push_credential;

alter table public.source_deliveries
    add constraint ck_source_delivery_authentication
        check (case transport
                 when 'pull' then credential_id is null
                                  and delivered_by_principal is null
                 when 'push' then (credential_id is not null)
                                  <> (delivered_by_principal is not null)
               end),
    add constraint ck_source_delivery_principal
        check (delivered_by_principal is null
               or length(btrim(delivered_by_principal)) > 0);

create table public.{CONFIRMATION_TABLE} (
    id bigserial primary key,
    delivery_id bigint not null,
    project_id bigint not null references public.projects (id),
    confirmed_by_principal text not null,
    confirmed_at timestamp with time zone not null default now(),
    constraint uq_source_delivery_confirmation_delivery unique (delivery_id),
    constraint fk_source_delivery_confirmation_delivery
        foreign key (delivery_id, project_id)
        references public.source_deliveries (id, project_id),
    constraint ck_source_delivery_confirmation_principal
        check (length(btrim(confirmed_by_principal)) > 0)
);

create index ix_source_delivery_confirmations_project_id
    on public.{CONFIRMATION_TABLE} (project_id);

create function public.reject_{CONFIRMATION_TABLE}_mutation() returns trigger
language plpgsql set search_path = pg_catalog as $$
begin
    raise exception 'a delivery confirmation is immutable: who admitted a delivery, and when, is never edited';
end $$;
revoke all on function public.reject_{CONFIRMATION_TABLE}_mutation() from public;
create trigger {CONFIRMATION_TABLE}_are_immutable
    before update or delete on public.{CONFIRMATION_TABLE}
    for each row execute function public.reject_{CONFIRMATION_TABLE}_mutation();
create trigger {CONFIRMATION_TABLE}_reject_truncate
    before truncate on public.{CONFIRMATION_TABLE}
    for each statement execute function public.reject_{CONFIRMATION_TABLE}_mutation();
"""

PRODUCT_UPLOAD_DELIVERY_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text := '{CONFIRMATION_TABLE}';
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
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
end $$;
"""

PRODUCT_UPLOAD_DELIVERY_SCHEMA_DOWN = f"""
drop table public.{CONFIRMATION_TABLE};

drop function public.reject_{CONFIRMATION_TABLE}_mutation();

alter table public.source_deliveries
    drop constraint ck_source_delivery_principal,
    drop constraint ck_source_delivery_authentication;

alter table public.source_deliveries
    add constraint ck_source_delivery_push_credential
        check ((transport = 'push') = (credential_id is not null));

alter table public.source_deliveries drop column delivered_by_principal;
"""


def _refuse_unrepresentable_authentication_downgrade(bind) -> None:
    """Refuse rather than invent a credential for a person who uploaded a file.

    The predecessor requires every pushed delivery to name a
    ``push_intake_credentials`` row, and the whole point of this block is that
    a signed-in person holds none.  Carrying such a delivery back would mean
    minting one, which is the alternative this block rejected; dropping it
    would lose a delivery the customer actually made.
    """

    blocked = bind.execute(
        sa.text(
            "select count(*) from source_deliveries "
            " where delivered_by_principal is not null"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#823 downgrade refuses: {blocked} delivery row(s) were "
            "authenticated by a person rather than a machine credential, and "
            "the predecessor ledger cannot represent them. Nothing is merged "
            "or dropped here."
        )


def upgrade(op) -> None:
    # After the spend authorization and before the sibling transitions: it
    # re-states one constraint the `unified_delivery` block established and
    # creates one relation nothing later in the revision names.
    op.execute(PRODUCT_UPLOAD_DELIVERY_SCHEMA)
    # A new table arrives carrying the schema owner's default privileges, which
    # hand every runtime login full access. The same terms the delivery ledger
    # itself was granted: both logins read and append, neither edits or erases.
    op.execute(f"revoke all on public.{CONFIRMATION_TABLE} from {RUNTIME_LOGINS}")
    op.execute(
        f"grant select, insert on public.{CONFIRMATION_TABLE} to {RUNTIME_LOGINS}"
    )
    op.execute(
        f"grant usage, select on sequence public.{CONFIRMATION_TABLE}_id_seq "
        f"to {RUNTIME_LOGINS}"
    )
    op.execute(PRODUCT_UPLOAD_DELIVERY_PARTITION_POLICIES)


def downgrade(op) -> None:
    # First among the feature reversals, mirroring the upgrade's last feature
    # block, and before `unified_delivery` unwinds the family this block
    # constrains.
    _refuse_unrepresentable_authentication_downgrade(op.get_bind())
    op.execute(
        f"do $$ begin "
        f"if exists (select 1 from pg_tables where schemaname = 'public' "
        f"and tablename = '{CONFIRMATION_TABLE}') then "
        f"revoke select, insert on public.{CONFIRMATION_TABLE} "
        f"from {RUNTIME_LOGINS}; "
        f"end if; end $$;"
    )
    op.execute(PRODUCT_UPLOAD_DELIVERY_SCHEMA_DOWN)
