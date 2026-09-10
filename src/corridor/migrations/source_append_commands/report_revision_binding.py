"""#602 Report Runs bound to the accepted revision (ADR-0075, ADR-0086).

A ``report_runs`` row and a ``scheduled_report_publications`` row each carry
a ``snapshot_json`` copy of the state the report was published against, and
nothing saying *which* accepted Project Record revision that state was.  A
copy with no reference beside it is a second source of truth: it can only be
compared with itself, it cannot be rebuilt, and when it drifts from the
record nothing in the schema notices.  #518, #599, #604 and #610 removed the
same shape elsewhere; this block removes it here.

``revision_id`` is the reference.  It is the accepted revision the reading
was taken against — the same watermark #488's weekly reading already states
its counts against, and the same one #534 freezes an issue on — so the two
surfaces name one revision rather than each holding a private copy.

The column is nullable, and deliberately so: historical rows were written
before the binding existed and this transition rewrites none of them.  What
a nullable column must not become is an optional binding, so a trigger
carries the rule the column cannot.  A new row for a project that has an
accepted revision must name one; only a project with no accepted revision at
all writes none, which is an explicit and checked absence rather than a
missing value.  Refusing every unbound insert outright was rejected for that
case alone: a legacy project whose accepted record is not on the spine has no
revision identity to name, and inventing one — a zero, the project id, the
newest revision of some other project — would be exactly the false reference
this block exists to prevent.

The composite foreign key does the other half.  ``project_record_revisions``
gains a ``(id, project_id)`` unique key, and each binding resolves against
it, so a run bound to another project's revision is unrepresentable rather
than merely unlikely.  The two guards are independent: a null binding is
refused only by the trigger, and a foreign binding only by the key.

``snapshot_json`` stays, demoted to a rebuildable compatibility cache and
labelled as one in the database itself.  Removing it or giving it an expiry
rule waits on the semantic-equivalence proof (#603): the legacy diff reads
it today, and dropping a cache before proving what rebuilds it is how a
weekly report starts reporting a change that did not happen.

**That demotion was wrong, and the #633 block at the end of this file
corrects it.**  #603 measured what a revision can rebuild and found three
things it cannot answer at all, so the column is the immutable Report
Reading payload of a dated occurrence rather than a cache of anything
(ADR-0092).  This paragraph is left as written because it is why the
``revision_id`` binding below has the shape it has; the column comment it
describes is replaced later in the same transition.
"""

from __future__ import annotations

import sqlalchemy as sa



REPORT_REVISION_BINDING_SCHEMA = """
alter table public.project_record_revisions
    add constraint uq_project_record_revisions_project_revision
    unique (id, project_id);

alter table public.report_runs add column revision_id bigint;
alter table public.report_runs
    add constraint fk_report_runs_revision
    foreign key (revision_id, project_id)
    references public.project_record_revisions (id, project_id);
create index ix_report_runs_revision_id
    on public.report_runs (revision_id);

alter table public.scheduled_report_publications add column revision_id bigint;
alter table public.scheduled_report_publications
    add constraint fk_scheduled_report_publications_revision
    foreign key (revision_id, project_id)
    references public.project_record_revisions (id, project_id);
create index ix_scheduled_report_publications_revision_id
    on public.scheduled_report_publications (revision_id);

comment on column public.report_runs.snapshot_json is
    'Rebuildable compatibility cache (#602). The authority is revision_id; '
    'this copy is retained only until #603 proves what rebuilds it.';
comment on column public.scheduled_report_publications.snapshot_json is
    'Rebuildable compatibility cache (#602). The authority is revision_id; '
    'this copy is retained only until #603 proves what rebuilds it.';

create function public.enforce_report_revision_binding()
    returns trigger
    language plpgsql
    as $$
        begin
            if tg_op = 'UPDATE'
               and old.revision_id is not null
               and new.revision_id is distinct from old.revision_id then
                raise exception 'the accepted revision a % was produced against is not rewritten', tg_table_name
                    using errcode='23514';
            end if;
            if tg_op = 'INSERT'
               and new.revision_id is null
               and exists (
                   select 1 from project_record_revisions
                    where project_id = new.project_id
               ) then
                raise exception 'a new % names the accepted Project Record revision it was produced against', tg_table_name
                    using errcode='23514';
            end if;
            return new;
        end; $$;

-- report_runs is an ordinary mutable relation, so the update arm has work to
-- do here.  scheduled_report_publications is already append-only by its own
-- trigger, so this one only has to guard the insert.
create trigger trg_report_runs_revision_binding
    before insert or update on public.report_runs
    for each row
    execute function public.enforce_report_revision_binding();
create trigger trg_scheduled_report_publications_revision_binding
    before insert on public.scheduled_report_publications
    for each row
    execute function public.enforce_report_revision_binding();
"""

REPORT_REVISION_BINDING_SCHEMA_DOWN = """
drop trigger if exists trg_scheduled_report_publications_revision_binding
    on public.scheduled_report_publications;
drop trigger if exists trg_report_runs_revision_binding on public.report_runs;
drop function if exists public.enforce_report_revision_binding() cascade;

comment on column public.scheduled_report_publications.snapshot_json is null;
comment on column public.report_runs.snapshot_json is null;

alter table public.scheduled_report_publications
    drop constraint if exists fk_scheduled_report_publications_revision;
drop index if exists public.ix_scheduled_report_publications_revision_id;
alter table public.scheduled_report_publications
    drop column if exists revision_id;

alter table public.report_runs
    drop constraint if exists fk_report_runs_revision;
drop index if exists public.ix_report_runs_revision_id;
alter table public.report_runs drop column if exists revision_id;

alter table public.project_record_revisions
    drop constraint if exists uq_project_record_revisions_project_revision;
"""

REPORT_REVISION_BOUND_TABLES = ("report_runs", "scheduled_report_publications")


def _refuse_unrepresentable_report_binding_downgrade(bind) -> None:
    """Refuse rather than drop a binding the snapshot-only shape cannot hold.

    The shape this downgrade restores is a ``snapshot_json`` copy and nothing
    that says which accepted revision it was taken against, which is the
    unreferenced copy #602 removes.  Dropping the column to get back there
    would recreate it silently, for every report already bound.  The same
    discipline as #599's and #610's downgrades: count what cannot be carried
    back, name it, and stop.
    """

    blocked = 0
    for table in REPORT_REVISION_BOUND_TABLES:
        bound = bind.execute(
            sa.text(
                "select count(*) from information_schema.columns "
                " where table_schema = 'public' and table_name = :table "
                "   and column_name = 'revision_id'"
            ),
            {"table": table},
        ).scalar_one()
        if not bound:
            continue
        blocked += bind.execute(
            sa.text(f"select count(*) from {table} where revision_id is not null")
        ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#602 downgrade refuses: {blocked} report reading(s) name the "
            "accepted Project Record revision they were produced against, and "
            "the snapshot-only shape cannot represent it. Nothing is dropped "
            "here."
        )


def upgrade(op) -> None:
    # Last, because both bindings resolve against a unique key this block adds
    # to project_record_revisions, and the trigger reads that table. The two
    # report relations keep the grants they already have: a column added to a
    # table the runtime may already insert into needs none of its own.
    op.execute(REPORT_REVISION_BINDING_SCHEMA)


def downgrade(op) -> None:
    # Next, because the upgrade added it last but one. Every binding already
    # recorded would go silently, so this refuses instead of dropping one.
    _refuse_unrepresentable_report_binding_downgrade(op.get_bind())
    op.execute(REPORT_REVISION_BINDING_SCHEMA_DOWN)
