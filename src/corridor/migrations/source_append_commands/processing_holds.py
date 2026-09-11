"""A hold says which processing stage it prohibits (#919).

Folded into this transition for the same window reason as the blocks around
it: ``corridor.migrations.policy`` allows one unreleased transition and this
is it.

``document_quarantines`` was ``(document_id, reason, created_at)``: one
free-text column, one row per document, and nothing on the row saying what the
hold actually forbids.  The one machine-readable gate over it refused *all*
rich processing for *any* row, while every hold a production path wrote was an
extraction restriction over a document that had already been read.  The gate
and the writers pointed opposite ways at the same row, and neither could be
believed.

**The maintainer's ruling of 2026-09-11 sets two processing boundaries.**  A
hold either prohibits *document reading* -- no ordinary rich parsing,
rendering, OCR or downstream semantic extraction -- or it prohibits *semantic
extraction* only, in which case authorized, bounded reading and Source Segment
creation may proceed while interpretation and proposal generation stay blocked.
A schedule workbook can be read into cells while its unsupported scheduling
relationships remain unmodeled; that is not a claim that the schedule is
semantically supported.

**One document may be held for several independent reasons at once.**  The
predecessor's primary key was ``document_id``, so the only way to record a
second reason was to overwrite the first -- which is how a mapping repair could
silently clear a safety restriction.  The key becomes the row's own
``id``, a partial unique index holds one *open* hold per
``(document_id, reason_code)``, and the effective permission is the
intersection of every open hold: reading is permitted only when no open hold
prohibits reading, and extraction only when no open hold prohibits either.

**Why a hold is never edited.**  ``enforce_document_hold_write`` refuses every
delete, every truncate, and every update except the one attributable release:
``released_at``, ``released_by_authority``, ``released_by`` and
``release_evidence`` are written together, once, and nothing else on the row
may move.  Clearing a restriction therefore cites the evidence that removes its
cause and leaves the original reason legible; it cannot be done by rewriting
why the source was held, and releasing one reason cannot touch another row.

**No ``project_id`` column.**  Every hold names a ``documents`` row, that row
names the project, and #824's partition policy on this relation already reads
the project through it.  A second copy of the project would be a fact the
database could contradict.

Classifying the rows that already exist
---------------------------------------

The investigation that opened #919 established what *today's* writers do.  It
does not establish the origin of every row in every retained database, so the
migration does not guess, and it never reads the free-text ``reason`` looking
for a convenient phrase.  Three outcomes, in this order, each from a retained
row rather than from prose:

1. **A reading restriction, preserved as the stronger one.**  The delivery that
   carried this document was itself dispositioned ``quarantined`` by intake, so
   the retained evidence establishes a security restriction.  Checked first, so
   a document that also has extraction evidence keeps the stronger reading
   prohibition.
2. **Extraction-only, from retained producer evidence.**  The document has an
   Extraction Run recorded ``quarantined``.  Only a reader that had already
   opened and read the document can record that run, so the hold it belongs to
   cannot have been prohibiting reading.  The run id is written into
   ``evidence``, so the classification is reproducible from the same rows.
3. **Unclassified, and therefore restrictive.**  Neither kind of evidence
   exists, so the origin and scope of the hold cannot be established.  It
   prohibits document reading until an attributable classification is recorded
   (``corridor.processing_holds.classify_hold``).  That is a conservative
   default, not a finding that the file is dangerous, and ``reason_code`` says
   exactly that.

Outcome 3 is deliberately more restrictive than the predecessor's behavior for
one population: a ``schedule`` registered but never handed to an extractor
leaves no producer evidence at all, so it falls here rather than being relaxed
because today's common writer happens to use the row for schedules.  Every hold
written after this transition states its own stage, so the unclassified
population is closed rather than growing.

The downgrade refuses rather than lying.  The supported predecessor holds one
row per document and has nowhere to put a release, so a database carrying two
holds on one document, or any released hold, cannot cross back: collapsing them
would either lose a restriction or resurrect a released one.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS

HOLD_TABLE = "document_quarantines"

# The two processing boundaries the ruling sets, and the four authorities that
# may be recorded against a hold. `migration` is recorded by the block below
# and is not a runtime authority: a hold this migration classified is lifted by
# an attributable act, never by another migration.
PROHIBITED_STAGES_SQL = "'document_reading', 'semantic_extraction'"
HOLD_AUTHORITIES_SQL = (
    "'intake_security', 'processing_rule', 'technical_operations', 'migration'"
)

MIGRATION_RULE = "migration:b2d5f8a1c4e7"


HOLD_COLUMNS = f"""
alter table public.{HOLD_TABLE}
    drop constraint {HOLD_TABLE}_pkey;
alter table public.{HOLD_TABLE}
    add column id bigserial;
alter table public.{HOLD_TABLE}
    add column prohibited_stage character varying(32),
    add column reason_code character varying(64),
    add column imposed_by_authority character varying(32),
    add column imposed_by character varying(128),
    add column evidence text,
    add column released_at timestamp with time zone,
    add column released_by_authority character varying(32),
    add column released_by character varying(128),
    add column release_evidence text;
alter table public.{HOLD_TABLE}
    add constraint {HOLD_TABLE}_pkey primary key (id);
create index ix_{HOLD_TABLE}_document_id
    on public.{HOLD_TABLE} (document_id);
"""


# --- The three classification outcomes, in the order they are applied -------
#
# Each is one statement over retained rows. The evidence column names the row
# that decided it, so the same reading can be taken again later without
# re-running the migration.

CLASSIFY_SECURITY_RESTRICTION = f"""
update public.{HOLD_TABLE} as q
   set prohibited_stage = 'document_reading',
       reason_code = 'intake_security_finding',
       imposed_by_authority = 'migration',
       imposed_by = '{MIGRATION_RULE}:delivery_disposition_quarantined',
       evidence = 'source_deliveries.id=' || d.source_delivery_id
                  || ' disposition=quarantined'
  from public.documents as d
 where d.id = q.document_id
   and q.prohibited_stage is null
   and exists (
        select 1
          from public.source_deliveries as sd
         where sd.id = d.source_delivery_id
           and sd.disposition = 'quarantined'
   );
"""

CLASSIFY_EXTRACTION_ONLY = f"""
update public.{HOLD_TABLE} as q
   set prohibited_stage = 'semantic_extraction',
       reason_code = 'extraction_refused_by_rule',
       imposed_by_authority = 'migration',
       imposed_by = '{MIGRATION_RULE}:quarantined_extraction_run',
       evidence = 'extraction_runs.id=' || r.run_id || ' outcome=quarantined'
  from (
        select document_id, max(id) as run_id
          from public.extraction_runs
         where outcome = 'quarantined'
         group by document_id
  ) as r
 where r.document_id = q.document_id
   and q.prohibited_stage is null;
"""

CLASSIFY_UNCLASSIFIED = f"""
update public.{HOLD_TABLE}
   set prohibited_stage = 'document_reading',
       reason_code = 'unclassified_historical_hold',
       imposed_by_authority = 'migration',
       imposed_by = '{MIGRATION_RULE}:unclassified',
       evidence = 'no retained delivery disposition or extraction run '
                  || 'establishes what this hold prohibited'
 where prohibited_stage is null;
"""


HOLD_CONSTRAINTS = f"""
alter table public.{HOLD_TABLE}
    alter column prohibited_stage set not null,
    alter column reason_code set not null,
    alter column imposed_by_authority set not null,
    alter column imposed_by set not null,
    alter column evidence set not null;

alter table public.{HOLD_TABLE}
    add constraint ck_{HOLD_TABLE}_stage
        check (prohibited_stage in ({PROHIBITED_STAGES_SQL})),
    add constraint ck_{HOLD_TABLE}_authority
        check (imposed_by_authority in ({HOLD_AUTHORITIES_SQL})),
    add constraint ck_{HOLD_TABLE}_release_authority
        check (
            released_by_authority is null
            or released_by_authority in ({HOLD_AUTHORITIES_SQL})
        ),
    add constraint ck_{HOLD_TABLE}_reason_code
        check (length(btrim(reason_code)) > 0),
    add constraint ck_{HOLD_TABLE}_imposed_by
        check (length(btrim(imposed_by)) > 0),
    add constraint ck_{HOLD_TABLE}_evidence
        check (length(btrim(evidence)) > 0),
    add constraint ck_{HOLD_TABLE}_release
        check (
            (
                released_at is null
                and released_by_authority is null
                and released_by is null
                and release_evidence is null
            )
            or (
                released_at is not null
                and released_by_authority is not null
                and length(btrim(released_by)) > 0
                and length(btrim(release_evidence)) > 0
            )
        );

create unique index uq_{HOLD_TABLE}_open
    on public.{HOLD_TABLE} (document_id, reason_code)
    where released_at is null;
"""


# A hold is append-only, and its one permitted change is the attributable
# release. Written as a trigger rather than left to the writers, because the
# whole point of #919's ruling is that adding or repairing one restriction must
# not be able to clear another.
ENFORCE_HOLD_WRITE = """
create function public.enforce_document_hold_write() returns trigger
    language plpgsql
    as $$
        begin
            if tg_op = 'TRUNCATE' then
                raise exception 'document_hold:immutable a recorded processing hold is released with an attributable record, never truncated'
                    using errcode='23514';
            end if;
            if tg_op = 'DELETE' then
                raise exception 'document_hold:immutable a recorded processing hold is released with an attributable record, never deleted'
                    using errcode='23514';
            end if;
            if tg_op = 'UPDATE' then
                if new.id is distinct from old.id
                   or new.document_id is distinct from old.document_id
                   or new.prohibited_stage is distinct from old.prohibited_stage
                   or new.reason_code is distinct from old.reason_code
                   or new.reason is distinct from old.reason
                   or new.imposed_by_authority is distinct from old.imposed_by_authority
                   or new.imposed_by is distinct from old.imposed_by
                   or new.evidence is distinct from old.evidence
                   or new.created_at is distinct from old.created_at then
                    raise exception 'document_hold:immutable why a source is held is never overwritten; record a release and, where one applies, another hold'
                        using errcode='23514';
                end if;
                if old.released_at is not null then
                    raise exception 'document_hold:already_released a released hold is not released a second time'
                        using errcode='23514';
                end if;
                if new.released_at is null then
                    raise exception 'document_hold:no_release the only change a recorded hold accepts is its attributable release'
                        using errcode='23514';
                end if;
            end if;
            return new;
        end; $$;

create trigger trg_{table}_write
    before update or delete on public.{table}
    for each row execute function public.enforce_document_hold_write();
create trigger trg_{table}_truncate
    before truncate on public.{table}
    for each statement execute function public.enforce_document_hold_write();
""".replace("{table}", HOLD_TABLE)


HOLD_SCHEMA_DOWN = f"""
drop trigger if exists trg_{HOLD_TABLE}_truncate on public.{HOLD_TABLE};
drop trigger if exists trg_{HOLD_TABLE}_write on public.{HOLD_TABLE};
drop function if exists public.enforce_document_hold_write();
drop index if exists public.ix_{HOLD_TABLE}_document_id;
alter table public.{HOLD_TABLE}
    drop constraint {HOLD_TABLE}_pkey;
alter table public.{HOLD_TABLE}
    drop column id,
    drop column prohibited_stage,
    drop column reason_code,
    drop column imposed_by_authority,
    drop column imposed_by,
    drop column evidence,
    drop column released_at,
    drop column released_by_authority,
    drop column released_by,
    drop column release_evidence;
alter table public.{HOLD_TABLE}
    add constraint {HOLD_TABLE}_pkey primary key (document_id);
"""


def upgrade(op) -> None:
    # After every feature block and before the sibling transitions: the only
    # relation it alters is the baseline's, and the two it reads to classify
    # -- `documents` and `extraction_runs` from the baseline,
    # `source_deliveries` as `unified_delivery` left it -- are established well
    # before here. Nothing later in the revision names this table.
    op.execute(HOLD_COLUMNS)
    op.execute(CLASSIFY_SECURITY_RESTRICTION)
    op.execute(CLASSIFY_EXTRACTION_ONLY)
    op.execute(CLASSIFY_UNCLASSIFIED)
    op.execute(HOLD_CONSTRAINTS)
    op.execute(ENFORCE_HOLD_WRITE)
    # The runtime capabilities keep the select and the insert the register and
    # the writers need, and lose the two privileges the trigger now refuses
    # anyway, so the grant and the guard say the same thing. The new surrogate
    # key needs its sequence.
    op.execute(
        f"revoke delete, truncate on public.{HOLD_TABLE} from {RUNTIME_LOGINS}"
    )
    op.execute(
        f"grant usage, select on sequence public.{HOLD_TABLE}_id_seq "
        f"to {RUNTIME_LOGINS}"
    )


def downgrade(op) -> None:
    # Before the sibling transitions unwind, mirroring the upgrade. The
    # supported predecessor keys this relation by `document_id` and carries no
    # release columns, so two open reasons on one document and any released
    # hold are both unrepresentable: collapsing the first would lose a
    # restriction, and carrying the second back would resurrect one that an
    # attributable act had already removed.
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            f"select exists (select 1 from public.{HOLD_TABLE} "
            "group by document_id having count(*) > 1)"
        )
    ):
        raise RuntimeError(
            "a source held for more than one reason cannot be represented by "
            "the supported predecessor, which holds one reason per document"
        )
    if bind.scalar(
        sa.text(
            f"select exists (select 1 from public.{HOLD_TABLE} "
            "where released_at is not null)"
        )
    ):
        raise RuntimeError(
            "a released processing hold cannot be represented by the "
            "supported predecessor, which has nowhere to record a release"
        )
    op.execute(HOLD_SCHEMA_DOWN)
    op.execute(f"grant delete on public.{HOLD_TABLE} to {RUNTIME_LOGINS}")
