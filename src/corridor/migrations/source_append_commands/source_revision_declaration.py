"""What a coordinator declared about one delivery, retained (#825).

Folded into this transition for the same window reason as the blocks above:
``corridor.migrations.policy`` allows one unreleased transition and this is it.

``later_revision.capture_later_revision`` reads a delivered workbook under the
registered mapping and proposes its differences from the accepted record.  Two
of the facts that decide what it may propose are not in the file:
``is_complete_enumerative_source`` says whether the workbook enumerates the
customer's whole population, and ``row_accounting_sealed`` says whether the
reading accounted for all of it.  Both were keyword arguments, which meant the
only way to answer them was to be the developer writing the call.  ADR-0076
makes an apparent removal lawful only from a complete enumerative source
revision, so the answer is load-bearing on the accepted record; a fact that
load-bearing belongs in a relation with the person who declared it beside it.

**One row per delivery, not per Document.**  A delivery is what arrived
(ADR-0089); the Document is what a confirmation went on to register from it.
The declaration is about the arrival: the same bytes can arrive twice under two
different declared revisions, and a delivery whose registration was refused was
still declared.  ``uq_source_revision_declaration_delivery`` therefore keys the
relation the way ``source_delivery_confirmations`` is keyed, and the composite
foreign key names the delivery with its project so a declaration recorded in one
project cannot reach another customer's delivery (#675).

**Append-only, like every other receipt in this family.**  A coordinator who
declared the wrong revision is making a new attributable act against the
delivery that carries the correction; the record of what was declared, and when,
and by whom, is never edited.  The immutability trigger is the same shape the
confirmation relation uses, for the same reason.

**``answer_sources_json`` is retained, not derived.**  Each answer records
whether it came from what the project registered, from the transport's own
external version, or from the person.  The rule that computes a default can
change with the next release; a receipt that cannot say where an answer came
from cannot show that completeness was never inferred from a previous delivery
or from a similar filename, which is the whole point of asking.

What was considered and rejected:

- **Two Boolean columns on ``source_deliveries``.**  The delivery ledger is the
  transport's record of what arrived, written by the service that took
  delivery; a human declaration written into it would make one immutable row
  carry two authorships.  It also has no room for the family, the revision
  identity or the relationship, which are the other three facts confirmation
  establishes.
- **An ``audit_log`` entry.**  The audit log is where an act is attributed; it
  is not queryable state, and the processing pass has to *read* this
  declaration to route a delivery at all.
- **Columns on ``documents``.**  A Document is registered by a confirmation
  that may never happen, and the declaration has to exist before it.

**Grants and partition.**  The relation is project-scoped, so it answers the
partition the way the record tables do (#657): the policy name matches
``p_%_project_partition`` and it is recorded in
``access.PARTITIONED_RELATIONS``.  Both runtime logins read and append it --
``corridor_web`` because the coordinator declares it on the confirmation
screen, ``corridor_worker`` because the processing pass reads it to route the
delivery -- and neither may edit or erase one.

The downgrade drops the relation whole rather than opening it to raw writes,
because it is born in this transition: there is no predecessor shape for a
declaration to be carried back into.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS

DECLARATION_TABLE = "source_revision_declarations"

SOURCE_REVISION_DECLARATION_SCHEMA = f"""
create table public.{DECLARATION_TABLE} (
    id bigserial primary key,
    delivery_id bigint not null,
    project_id bigint not null references public.projects (id),
    source_family text not null,
    revision_identity text not null,
    completeness varchar(32) not null,
    revision_relationship varchar(32) not null,
    related_revision_identity text,
    uses_registered_mapping boolean not null,
    answer_sources_json jsonb not null default '{{}}'::jsonb,
    declared_by_principal text not null,
    declared_at timestamp with time zone not null default now(),
    constraint uq_source_revision_declaration_delivery unique (delivery_id),
    constraint fk_source_revision_declaration_delivery
        foreign key (delivery_id, project_id)
        references public.source_deliveries (id, project_id),
    constraint ck_source_revision_declaration_principal
        check (length(btrim(declared_by_principal)) > 0),
    constraint ck_source_revision_declaration_family
        check (length(btrim(source_family)) > 0),
    constraint ck_source_revision_declaration_revision
        check (length(btrim(revision_identity)) > 0),
    constraint ck_source_revision_declaration_completeness
        check (completeness in ('complete_enumeration', 'partial_export')),
    constraint ck_source_revision_declaration_relationship
        check (revision_relationship in
               ('replaces', 'supplements', 'additional_rendition')),
    constraint ck_source_revision_declaration_rendition_names_its_revision
        check (revision_relationship <> 'additional_rendition'
               or length(btrim(coalesce(related_revision_identity, ''))) > 0)
);

create index ix_source_revision_declarations_project_id
    on public.{DECLARATION_TABLE} (project_id);

create function public.reject_{DECLARATION_TABLE}_mutation() returns trigger
language plpgsql set search_path = pg_catalog as $$
begin
    raise exception 'a source revision declaration is immutable: what a person declared about a delivery, and when, is never edited';
end $$;
revoke all on function public.reject_{DECLARATION_TABLE}_mutation() from public;
create trigger {DECLARATION_TABLE}_are_immutable
    before update or delete on public.{DECLARATION_TABLE}
    for each row execute function public.reject_{DECLARATION_TABLE}_mutation();
create trigger {DECLARATION_TABLE}_reject_truncate
    before truncate on public.{DECLARATION_TABLE}
    for each statement execute function public.reject_{DECLARATION_TABLE}_mutation();
"""

SOURCE_REVISION_DECLARATION_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text := '{DECLARATION_TABLE}';
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

SOURCE_REVISION_DECLARATION_SCHEMA_DOWN = f"""
drop table public.{DECLARATION_TABLE};

drop function public.reject_{DECLARATION_TABLE}_mutation();
"""


def upgrade(op) -> None:
    # After the product upload delivery and before the sibling transitions: it
    # names `source_deliveries` and its project composite key, both of which
    # earlier blocks in this revision established, and creates one relation
    # nothing later in the revision names.
    op.execute(SOURCE_REVISION_DECLARATION_SCHEMA)
    # A new table arrives carrying the schema owner's default privileges, which
    # hand every runtime login full access. The same terms the confirmation
    # relation was granted: both logins read and append, neither edits or
    # erases.
    op.execute(f"revoke all on public.{DECLARATION_TABLE} from {RUNTIME_LOGINS}")
    op.execute(
        f"grant select, insert on public.{DECLARATION_TABLE} to {RUNTIME_LOGINS}"
    )
    op.execute(
        f"grant usage, select on sequence public.{DECLARATION_TABLE}_id_seq "
        f"to {RUNTIME_LOGINS}"
    )
    op.execute(SOURCE_REVISION_DECLARATION_PARTITION_POLICIES)


def downgrade(op) -> None:
    # Before the product upload delivery unwinds the ledger this block's
    # composite foreign key names, mirroring the upgrade's order.
    op.execute(
        f"do $$ begin "
        f"if exists (select 1 from pg_tables where schemaname = 'public' "
        f"and tablename = '{DECLARATION_TABLE}') then "
        f"revoke select, insert on public.{DECLARATION_TABLE} "
        f"from {RUNTIME_LOGINS}; "
        f"end if; end $$;"
    )
    op.execute(SOURCE_REVISION_DECLARATION_SCHEMA_DOWN)
