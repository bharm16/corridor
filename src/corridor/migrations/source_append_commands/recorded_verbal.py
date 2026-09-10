"""Spine-native Recorded Verbal origin (#512, ADR-0081 stage 1).

The backfill reproduces each stored Fact digest before it replaces one, and
the recipe it reproduces them with stays frozen in the revision module and
is passed to ``upgrade`` as ``fact_digest``, together with the ``revision``
identity every receipt records.  A family module never imports the revision
that composes it: Alembic loads that file under a name of its own.

ADR-0074 gave the `recorded_verbal_statement` segment a `statement_id`
foreign key to `dependency_events`, so the evidence spine depended on a
legacy Project Record aggregate, and `facts.content_sha256` carried that
legacy id inside the Fact identity digest.  ADR-0081 stage 1 replaces both:
the attestation becomes its own row, the segment points at it, and the
legacy key survives only in `recorded_verbal_origin_statements` — a
temporary compatibility mapping that retires with the dual-write.

The transition is folded into this revision because the migration window
holds one unreleased transition and this is it (`migrations/policy.py`).
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.roles import (
    RUNTIME_LOGINS,
    SOURCE_APPEND_ROLE,
)


RECORDED_VERBAL_TABLES = (
    "recorded_verbal_origins",
    "recorded_verbal_origin_statements",
    "recorded_verbal_origin_backfill_receipts",
    "recorded_verbal_origin_fact_digests",
)

# The origin and its legacy mapping are appended by the source-append role's
# command.  The two backfill receipt tables are written once, by this
# migration, and are read-only to everyone afterwards.
RECORDED_VERBAL_APPEND_TABLES = (
    "recorded_verbal_origins",
    "recorded_verbal_origin_statements",
)

RECORDED_VERBAL_COMMANDS = {
    "append_recorded_verbal_origin": (
        "(bigint, text, timestamp with time zone, date, text, "
        "character varying, bigint, bigint)"
    ),
    "append_source_segments": "(bigint, bigint, bigint, jsonb)",
}

RECORDED_VERBAL_ORIGIN_SCHEMA = """
create table public.recorded_verbal_origins (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    recorded_by text not null,
    recorded_at timestamp with time zone not null,
    conversation_date date,
    exact_text text not null,
    content_sha256 character varying(64) not null,
    corrects_origin_id bigint references public.recorded_verbal_origins (id),
    created_at timestamp with time zone not null default now(),
    constraint uq_recorded_verbal_origins_project_id unique (project_id, id),
    constraint uq_recorded_verbal_origins_corrects unique (corrects_origin_id),
    constraint ck_recorded_verbal_origins_recorder
        check (length(trim(recorded_by)) > 0),
    constraint ck_recorded_verbal_origins_exact_text
        check (length(exact_text) > 0),
    constraint ck_recorded_verbal_origins_content_sha256
        check (content_sha256 ~ '^[0-9a-f]{64}$'),
    constraint ck_recorded_verbal_origins_corrects_other
        check (corrects_origin_id is null or corrects_origin_id <> id)
);
create index ix_recorded_verbal_origins_project_id
    on public.recorded_verbal_origins (project_id);

-- The one place a target relation still names a legacy statement.  Both
-- directions are unique, so a legacy statement can never acquire a second
-- origin and an origin can never be re-pointed at a second statement.
create table public.recorded_verbal_origin_statements (
    origin_id bigint primary key
        references public.recorded_verbal_origins (id),
    project_id bigint not null references public.projects (id),
    statement_id bigint not null references public.dependency_events (id),
    created_at timestamp with time zone not null default now(),
    constraint uq_recorded_verbal_origin_statements_statement
        unique (statement_id)
);
create index ix_recorded_verbal_origin_statements_project_id
    on public.recorded_verbal_origin_statements (project_id);

create table public.recorded_verbal_origin_backfill_receipts (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    origin_id bigint not null references public.recorded_verbal_origins (id),
    legacy_statement_id bigint not null,
    source_segment_id bigint not null references public.source_segments (id),
    fact_count integer not null,
    migration_revision character varying(32) not null,
    executed_by text not null,
    created_at timestamp with time zone not null default now(),
    constraint uq_recorded_verbal_backfill_receipts_origin unique (origin_id),
    constraint uq_recorded_verbal_backfill_receipts_statement
        unique (legacy_statement_id),
    constraint uq_recorded_verbal_backfill_receipts_segment
        unique (source_segment_id),
    constraint ck_recorded_verbal_backfill_receipts_executor
        check (length(trim(executed_by)) > 0),
    constraint ck_recorded_verbal_backfill_receipts_fact_count
        check (fact_count >= 0)
);
create index ix_recorded_verbal_origin_backfill_receipts_project_id
    on public.recorded_verbal_origin_backfill_receipts (project_id);

create table public.recorded_verbal_origin_fact_digests (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    receipt_id bigint not null
        references public.recorded_verbal_origin_backfill_receipts (id),
    fact_id bigint not null references public.facts (id),
    prior_content_sha256 character varying(64) not null,
    content_sha256 character varying(64) not null,
    constraint uq_recorded_verbal_origin_fact_digests_fact unique (fact_id),
    constraint ck_recorded_verbal_origin_fact_digests_change
        check (prior_content_sha256 ~ '^[0-9a-f]{64}$'
               and content_sha256 ~ '^[0-9a-f]{64}$'
               and prior_content_sha256 <> content_sha256)
);
create index ix_recorded_verbal_origin_fact_digests_project_id
    on public.recorded_verbal_origin_fact_digests (project_id);
create index ix_recorded_verbal_origin_fact_digests_receipt_id
    on public.recorded_verbal_origin_fact_digests (receipt_id);

alter table public.source_segments
    add column recorded_verbal_origin_id bigint;
"""

# Applied after the backfill has filled `recorded_verbal_origin_id`: the
# locator check and the one-segment-per-origin index move to the spine-native
# column, and the legacy key leaves the target table entirely.
RECORDED_VERBAL_SEGMENT_CUTOVER = """
alter table public.source_segments
    drop constraint ck_source_segments_locator;
alter table public.source_segments
    add constraint ck_source_segments_locator check (
        (kind = 'spreadsheet_cell' and document_id is not null
         and recorded_verbal_origin_id is null and length(sheet_name) > 0
         and cell_range ~ '^[A-Z]+[1-9][0-9]*$' and page_no is null
         and start_offset is null and end_offset is null)
        or (kind = 'prose_span' and document_id is not null
            and recorded_verbal_origin_id is null and sheet_name is null
            and cell_range is null and page_no > 0 and start_offset >= 0
            and end_offset > start_offset)
        or (kind = 'recorded_verbal_statement' and document_id is null
            and recorded_verbal_origin_id is not null and sheet_name is null
            and cell_range is null and page_no is null
            and start_offset is null and end_offset is null)
    );
drop index public.uq_source_segments_statement;
alter table public.source_segments drop column statement_id;
alter table public.source_segments
    add constraint fk_source_segments_recorded_verbal_origin_scope
    foreign key (project_id, recorded_verbal_origin_id)
    references public.recorded_verbal_origins (project_id, id);
create unique index uq_source_segments_recorded_verbal_origin
    on public.source_segments (recorded_verbal_origin_id)
    where kind = 'recorded_verbal_statement';
"""

RECORDED_VERBAL_SEGMENT_RESTORE_COLUMN = """
alter table public.source_segments
    add column statement_id bigint references public.dependency_events (id);
"""

RECORDED_VERBAL_SEGMENT_CUTOVER_DOWN = """
alter table public.source_segments
    drop constraint ck_source_segments_locator;
alter table public.source_segments
    add constraint ck_source_segments_locator check (
        (kind = 'spreadsheet_cell' and document_id is not null
         and statement_id is null and length(sheet_name) > 0
         and cell_range ~ '^[A-Z]+[1-9][0-9]*$' and page_no is null
         and start_offset is null and end_offset is null)
        or (kind = 'prose_span' and document_id is not null
            and statement_id is null and sheet_name is null
            and cell_range is null and page_no > 0 and start_offset >= 0
            and end_offset > start_offset)
        or (kind = 'recorded_verbal_statement' and document_id is null
            and statement_id is not null and sheet_name is null
            and cell_range is null and page_no is null
            and start_offset is null and end_offset is null)
    );
drop index public.uq_source_segments_recorded_verbal_origin;
alter table public.source_segments
    drop constraint fk_source_segments_recorded_verbal_origin_scope;
alter table public.source_segments drop column recorded_verbal_origin_id;
create unique index uq_source_segments_statement
    on public.source_segments (statement_id)
    where kind = 'recorded_verbal_statement';
"""

RECORDED_VERBAL_ORIGIN_SCHEMA_DOWN = """
drop table if exists public.recorded_verbal_origin_fact_digests;
drop table if exists public.recorded_verbal_origin_backfill_receipts;
drop table if exists public.recorded_verbal_origin_statements;
drop table if exists public.recorded_verbal_origins;
"""

APPEND_RECORDED_VERBAL_ORIGIN = """
create function public.append_recorded_verbal_origin(
    p_project_id bigint,
    p_recorded_by text,
    p_recorded_at timestamp with time zone,
    p_conversation_date date,
    p_exact_text text,
    p_content_sha256 character varying,
    p_corrects_origin_id bigint,
    p_legacy_statement_id bigint
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            origin_id bigint;
        begin
            if p_recorded_by is null or length(trim(p_recorded_by)) = 0 then
                raise exception 'a recorded verbal origin needs its recorder'
                    using errcode = '23514';
            end if;
            if p_recorded_at is null then
                raise exception 'a recorded verbal origin needs its recorded time'
                    using errcode = '23514';
            end if;
            if p_exact_text is null or length(p_exact_text) = 0 then
                raise exception 'a recorded verbal origin needs the exact words'
                    using errcode = '23514';
            end if;
            if p_content_sha256 is distinct from
                encode(sha256(convert_to(p_exact_text, 'UTF8')), 'hex') then
                raise exception 'recorded verbal origin digest does not match its exact words'
                    using errcode = '23514';
            end if;
            if p_corrects_origin_id is not null and not exists (
                select 1 from recorded_verbal_origins
                 where id = p_corrects_origin_id and project_id = p_project_id
            ) then
                raise exception 'corrected recorded verbal origin is outside its project'
                    using errcode = '23514';
            end if;
            if p_legacy_statement_id is not null then
                if not exists (
                    select 1 from dependency_events
                     where id = p_legacy_statement_id and project_id = p_project_id
                ) then
                    raise exception 'recorded verbal statement is outside its project'
                        using errcode = '23514';
                end if;
                -- The dual-write replays the same act; the legacy mapping is
                -- the idempotency key while stages 1 through 5 still write it.
                select origin.id as id,
                       origin.recorded_by as recorded_by,
                       origin.recorded_at as recorded_at,
                       origin.conversation_date as conversation_date,
                       origin.content_sha256 as content_sha256,
                       origin.corrects_origin_id as corrects_origin_id
                  into existing
                  from recorded_verbal_origin_statements mapping
                  join recorded_verbal_origins origin
                    on origin.id = mapping.origin_id
                 where mapping.statement_id = p_legacy_statement_id;
                if found then
                    if existing.recorded_by <> p_recorded_by
                        or existing.recorded_at is distinct from p_recorded_at
                        or existing.conversation_date is distinct from p_conversation_date
                        or existing.content_sha256 <> p_content_sha256
                        or existing.corrects_origin_id is distinct from p_corrects_origin_id then
                        raise exception 'recorded verbal origin is already bound to different content'
                            using errcode = '23514';
                    end if;
                    return existing.id;
                end if;
            end if;
            insert into recorded_verbal_origins (
                project_id, recorded_by, recorded_at, conversation_date,
                exact_text, content_sha256, corrects_origin_id
            ) values (
                p_project_id, p_recorded_by, p_recorded_at, p_conversation_date,
                p_exact_text, p_content_sha256, p_corrects_origin_id
            ) returning id into origin_id;
            if p_legacy_statement_id is not null then
                insert into recorded_verbal_origin_statements (
                    origin_id, project_id, statement_id
                ) values (origin_id, p_project_id, p_legacy_statement_id);
            end if;
            return origin_id;
        end; $$;
"""

# The #492 command re-issued over the spine-native origin.  PostgreSQL will
# not rename an input parameter through `create or replace`, so the third
# parameter changes identity by a drop and a create; the signature, the owner,
# and the grants are unchanged.
APPEND_SOURCE_SEGMENTS_OVER_ORIGIN = """
create function public.append_source_segments(
    p_project_id bigint,
    p_document_id bigint,
    p_recorded_verbal_origin_id bigint,
    p_segments jsonb
) returns bigint[]
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            item jsonb;
            segment_kind text;
            exact text;
            digest text;
            existing record;
            appended bigint[] := '{}';
            segment_id bigint;
        begin
            if (p_document_id is null) = (p_recorded_verbal_origin_id is null) then
                raise exception 'source segments belong to one document or one recorded verbal origin'
                    using errcode = '23514';
            end if;
            if p_document_id is not null and not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'source segment document is outside its project'
                    using errcode = '23514';
            end if;
            if p_recorded_verbal_origin_id is not null and not exists (
                select 1 from recorded_verbal_origins
                 where id = p_recorded_verbal_origin_id and project_id = p_project_id
            ) then
                raise exception 'source segment recorded verbal origin is outside its project'
                    using errcode = '23514';
            end if;
            if p_segments is null or jsonb_typeof(p_segments) <> 'array' then
                raise exception 'source segments must be a list'
                    using errcode = '23514';
            end if;
            for item in select value from jsonb_array_elements(p_segments) loop
                segment_kind := item ->> 'kind';
                exact := item ->> 'exact_text';
                digest := item ->> 'content_sha256';
                if exact is null or length(exact) = 0 then
                    raise exception 'source segment needs exact text'
                        using errcode = '23514';
                end if;
                if digest is distinct from
                    encode(sha256(convert_to(exact, 'UTF8')), 'hex') then
                    raise exception 'source segment digest does not match its exact text'
                        using errcode = '23514';
                end if;
                if segment_kind = 'spreadsheet_cell' then
                    select id, content_sha256, ordinal into existing
                      from source_segments
                     where document_id = p_document_id
                       and kind = 'spreadsheet_cell'
                       and sheet_name = item ->> 'sheet_name'
                       and cell_range = item ->> 'cell_range';
                elsif segment_kind = 'prose_span' then
                    select id, content_sha256, ordinal into existing
                      from source_segments
                     where document_id = p_document_id
                       and kind = 'prose_span'
                       and page_no = (item ->> 'page_no')::integer
                       and start_offset = (item ->> 'start_offset')::integer
                       and end_offset = (item ->> 'end_offset')::integer;
                elsif segment_kind = 'recorded_verbal_statement' then
                    select id, content_sha256, ordinal into existing
                      from source_segments
                     where kind = 'recorded_verbal_statement'
                       and recorded_verbal_origin_id = p_recorded_verbal_origin_id;
                else
                    raise exception 'unrecognized source segment kind'
                        using errcode = '23514';
                end if;
                if found then
                    if existing.content_sha256 <> digest
                        or existing.ordinal <> (item ->> 'ordinal')::integer then
                        raise exception 'source segment locator is already bound to different content'
                            using errcode = '23514';
                    end if;
                    appended := appended || existing.id;
                    continue;
                end if;
                insert into source_segments (
                    project_id, document_id, recorded_verbal_origin_id, kind,
                    exact_text, content_sha256, ordinal, sheet_name, cell_range,
                    page_no, start_offset, end_offset
                ) values (
                    p_project_id, p_document_id, p_recorded_verbal_origin_id,
                    segment_kind, exact, digest, (item ->> 'ordinal')::integer,
                    item ->> 'sheet_name', item ->> 'cell_range',
                    (item ->> 'page_no')::integer,
                    (item ->> 'start_offset')::integer,
                    (item ->> 'end_offset')::integer
                ) returning id into segment_id;
                appended := appended || segment_id;
            end loop;
            return appended;
        end; $$;
"""

# The executor of the one backfill run, recorded on every receipt so the
# migration is attributable and never mistaken for the recorder who attested.
RECORDED_VERBAL_BACKFILL_EXECUTOR = "migration:b2d5f8a1c4e7/512"

# `source_segments` and `facts` refuse every UPDATE at runtime, which is the
# point of an append-only spine.  A schema transition that moves an identity is
# the one thing that cannot be expressed as an append, so the two row guards are
# lifted for the length of the backfill and restored immediately, and every row
# the window touched is named in a receipt.
_APPEND_ONLY_GUARDS = (
    ("source_segments", "trg_source_segments_append_only"),
    ("facts", "trg_facts_append_only"),
)


def _with_append_only_guards_lifted(bind, work) -> None:
    for table, trigger in _APPEND_ONLY_GUARDS:
        bind.execute(sa.text(f"alter table public.{table} disable trigger {trigger}"))
    try:
        work(bind)
    finally:
        for table, trigger in _APPEND_ONLY_GUARDS:
            bind.execute(
                sa.text(f"alter table public.{table} enable trigger {trigger}")
            )

_MATERIALIZED_SOURCE_LINKS = {
    "statement_wording": ("value_source", "attribution_source"),
    "statement_timing": ("value_source",),
    "applies_to": ("value_source",),
}


def _structured_value(bind, fact_type: str, fact_id: int):
    """Rebuild the typed satellite exactly as the Fact's replay reads it."""

    if fact_type == "statement_wording":
        return None
    if fact_type == "applies_to":
        members = bind.execute(
            sa.text(
                "select dependency_id from fact_applies_to "
                "where fact_id = :fact_id order by ordinal"
            ),
            {"fact_id": fact_id},
        ).scalars().all()
        return {"dependency_ids": [int(member) for member in members]}
    rows = bind.execute(
        sa.text(
            "select timing_role, text, precision, start_date, end_date "
            "from fact_statement_timings where fact_id = :fact_id "
            "order by timing_role"
        ),
        {"fact_id": fact_id},
    ).all()
    return {
        "timings": [
            {
                "role": row.timing_role,
                "text": row.text,
                "precision": row.precision,
                "start_date": row.start_date.isoformat()
                if row.start_date is not None
                else None,
                "end_date": row.end_date.isoformat()
                if row.end_date is not None
                else None,
            }
            for row in rows
        ]
    }


def _backfill_recorded_verbal_origins(bind, fact_digest, revision) -> None:
    """Give every dual-written verbal one spine-native origin, or refuse.

    One legacy statement becomes exactly one origin, one compatibility
    mapping row, and one attributable receipt; every Fact the verbal already
    carried has its identity digest reproduced from the stored row before it
    is replaced, and both digests are recorded.  A row that cannot be
    reconciled aborts the whole transition rather than being guessed at.
    """

    segments = bind.execute(
        sa.text(
            "select segment.id as segment_id, segment.project_id as project_id, "
            "       segment.statement_id as statement_id, "
            "       statement.created_by as recorded_by, "
            "       statement.created_at as recorded_at, "
            "       statement.event_date as conversation_date, "
            "       segment.exact_text as exact_text, "
            "       segment.content_sha256 as content_sha256 "
            "  from source_segments segment "
            "  join dependency_events statement "
            "    on statement.id = segment.statement_id "
            " where segment.kind = 'recorded_verbal_statement' "
            " order by segment.id"
        )
    ).all()

    orphans = bind.execute(
        sa.text(
            "select count(*) from source_segments "
            " where kind = 'recorded_verbal_statement' and statement_id is null"
        )
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"#512 backfill refuses: {orphans} recorded verbal segment(s) name "
            "no legacy statement, so no origin can be reconciled for them"
        )

    migrated_facts = 0
    for segment in segments:
        if segment.recorded_by is None or not segment.recorded_by.strip():
            raise RuntimeError(
                f"#512 backfill refuses: legacy statement {segment.statement_id} "
                "names no recorder, so its attestation cannot be reconstructed"
            )
        origin_id = bind.execute(
            sa.text(
                "insert into recorded_verbal_origins ("
                "project_id, recorded_by, recorded_at, conversation_date, "
                "exact_text, content_sha256"
                ") values ("
                ":project_id, :recorded_by, :recorded_at, :conversation_date, "
                ":exact_text, :content_sha256"
                ") returning id"
            ),
            {
                "project_id": segment.project_id,
                "recorded_by": segment.recorded_by,
                "recorded_at": segment.recorded_at,
                "conversation_date": segment.conversation_date,
                "exact_text": segment.exact_text,
                "content_sha256": segment.content_sha256,
            },
        ).scalar_one()
        bind.execute(
            sa.text(
                "insert into recorded_verbal_origin_statements ("
                "origin_id, project_id, statement_id"
                ") values (:origin_id, :project_id, :statement_id)"
            ),
            {
                "origin_id": origin_id,
                "project_id": segment.project_id,
                "statement_id": segment.statement_id,
            },
        )
        bind.execute(
            sa.text(
                "update source_segments set recorded_verbal_origin_id = :origin_id "
                " where id = :segment_id"
            ),
            {"origin_id": origin_id, "segment_id": segment.segment_id},
        )

        facts = bind.execute(
            sa.text(
                "select fact.id as id, fact.fact_type as fact_type, "
                "       fact.subject_kind as subject_kind, "
                "       fact.subject_key as subject_key, "
                "       fact.text_value as text_value, "
                "       fact.date_value as date_value, "
                "       fact.external_org_value_id as external_org_value_id, "
                "       fact.content_sha256 as content_sha256 "
                "  from facts fact "
                "  join fact_sources source on source.fact_id = fact.id "
                " where source.source_segment_id = :segment_id "
                "   and source.role = 'value_source' "
                " order by fact.id"
            ),
            {"segment_id": segment.segment_id},
        ).all()

        receipt_id = bind.execute(
            sa.text(
                "insert into recorded_verbal_origin_backfill_receipts ("
                "project_id, origin_id, legacy_statement_id, source_segment_id, "
                "fact_count, migration_revision, executed_by"
                ") values ("
                ":project_id, :origin_id, :statement_id, :segment_id, "
                ":fact_count, :revision, :executed_by"
                ") returning id"
            ),
            {
                "project_id": segment.project_id,
                "origin_id": origin_id,
                "statement_id": segment.statement_id,
                "segment_id": segment.segment_id,
                "fact_count": len(facts),
                "revision": revision,
                "executed_by": RECORDED_VERBAL_BACKFILL_EXECUTOR,
            },
        ).scalar_one()

        for fact in facts:
            if fact.fact_type not in _MATERIALIZED_SOURCE_LINKS:
                raise RuntimeError(
                    f"#512 backfill refuses: Fact {fact.id} of type "
                    f"{fact.fact_type!r} takes its value from a recorded verbal "
                    "segment, and this transition knows no identity recipe for it"
                )
            structured = _structured_value(bind, fact.fact_type, fact.id)
            shared = {
                "fact_type": fact.fact_type,
                "subject_kind": fact.subject_kind,
                "subject_key": fact.subject_key,
                "text_value": fact.text_value,
                "date_value": (
                    fact.date_value.isoformat()
                    if fact.date_value is not None
                    else None
                ),
                "external_org_value_id": fact.external_org_value_id,
                "structured_value": structured,
                "source_segment_id": segment.segment_id,
            }
            reproduced = fact_digest(
                run_identity={"statement_id": segment.statement_id}, **shared
            )
            if reproduced != fact.content_sha256:
                raise RuntimeError(
                    f"#512 backfill refuses: Fact {fact.id} does not reproduce "
                    "its stored identity digest from its stored row, so its "
                    "spine-native digest cannot be derived"
                )
            replacement = fact_digest(
                run_identity={"recorded_verbal_origin_id": origin_id}, **shared
            )
            bind.execute(
                sa.text(
                    "update facts set content_sha256 = :digest where id = :fact_id"
                ),
                {"digest": replacement, "fact_id": fact.id},
            )
            bind.execute(
                sa.text(
                    "insert into recorded_verbal_origin_fact_digests ("
                    "project_id, receipt_id, fact_id, prior_content_sha256, "
                    "content_sha256"
                    ") values ("
                    ":project_id, :receipt_id, :fact_id, :prior, :digest)"
                ),
                {
                    "project_id": segment.project_id,
                    "receipt_id": receipt_id,
                    "fact_id": fact.id,
                    "prior": fact.content_sha256,
                    "digest": replacement,
                },
            )
            migrated_facts += 1

    counted = bind.execute(
        sa.text(
            "select (select count(*) from source_segments "
            "         where kind = 'recorded_verbal_statement') as segments, "
            "       (select count(*) from source_segments "
            "         where kind = 'recorded_verbal_statement' "
            "           and recorded_verbal_origin_id is not null) as pointed, "
            "       (select count(*) from recorded_verbal_origins) as origins, "
            "       (select count(*) from recorded_verbal_origin_statements) "
            "           as mappings, "
            "       (select count(*) from "
            "         recorded_verbal_origin_backfill_receipts) as receipts, "
            "       (select coalesce(sum(fact_count), 0) from "
            "         recorded_verbal_origin_backfill_receipts) as counted_facts, "
            "       (select count(*) from recorded_verbal_origin_fact_digests) "
            "           as digests"
        )
    ).one()
    expected = len(segments)
    if (
        counted.segments != expected
        or counted.pointed != expected
        or counted.origins != expected
        or counted.mappings != expected
        or counted.receipts != expected
        or counted.counted_facts != migrated_facts
        or counted.digests != migrated_facts
    ):
        raise RuntimeError(
            "#512 backfill refuses: the reconciliation is not one-to-one — "
            f"{expected} recorded verbal segment(s) and {migrated_facts} Fact(s) "
            f"produced {counted.pointed} pointed segment(s), {counted.origins} "
            f"origin(s), {counted.mappings} mapping(s), {counted.receipts} "
            f"receipt(s), {counted.counted_facts} counted Fact(s), and "
            f"{counted.digests} digest receipt(s)"
        )


def _restore_legacy_recorded_verbal_state(bind) -> None:
    """Put the legacy statement lineage and Fact identities back, or refuse.

    The downgrade is exact, not best effort: a verbal whose origin carries no
    legacy mapping has no statement to go back to, and a Fact identity is
    restored only from the receipt that recorded what the backfill replaced.
    """

    unmappable = bind.execute(
        sa.text(
            "select count(*) from source_segments segment "
            " where segment.kind = 'recorded_verbal_statement' "
            "   and not exists ("
            "       select 1 from recorded_verbal_origin_statements mapping "
            "        where mapping.origin_id = segment.recorded_verbal_origin_id)"
        )
    ).scalar_one()
    if unmappable:
        raise RuntimeError(
            f"#512 downgrade refuses: {unmappable} recorded verbal segment(s) "
            "have a spine-native origin with no legacy statement to return to"
        )
    bind.execute(
        sa.text(
            "update source_segments segment "
            "   set statement_id = mapping.statement_id "
            "  from recorded_verbal_origin_statements mapping "
            " where segment.recorded_verbal_origin_id = mapping.origin_id"
        )
    )
    bind.execute(
        sa.text(
            "update facts set content_sha256 = receipt.prior_content_sha256 "
            "  from recorded_verbal_origin_fact_digests receipt "
            " where facts.id = receipt.fact_id "
            "   and facts.content_sha256 = receipt.content_sha256"
        )
    )


def upgrade(op, fact_digest, revision) -> None:
    op.execute(RECORDED_VERBAL_ORIGIN_SCHEMA)
    # The backfill runs while `source_segments.statement_id` still exists: it
    # is the only place the legacy attestation can be read from.
    _with_append_only_guards_lifted(
        op.get_bind(),
        lambda bind: _backfill_recorded_verbal_origins(
            bind, fact_digest, revision
        ),
    )
    op.execute(RECORDED_VERBAL_SEGMENT_CUTOVER)
    for table in RECORDED_VERBAL_TABLES:
        # The origin, its legacy mapping, and the backfill receipts are read
        # by the application; only the command writes the first two, and only
        # this transition ever writes the receipts.
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
    for table in RECORDED_VERBAL_APPEND_TABLES:
        op.execute(f"grant select, insert on public.{table} to {SOURCE_APPEND_ROLE}")
    op.execute(
        "grant usage, select on sequence public.recorded_verbal_origins_id_seq "
        f"to {SOURCE_APPEND_ROLE}"
    )
    # The segment command's third parameter is now the spine-native origin.
    op.execute(
        "drop function public.append_source_segments(bigint, bigint, bigint, jsonb)"
    )
    op.execute(APPEND_RECORDED_VERBAL_ORIGIN)
    op.execute(APPEND_SOURCE_SEGMENTS_OVER_ORIGIN)
    for name, signature in RECORDED_VERBAL_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {SOURCE_APPEND_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        op.execute(
            f"grant execute on function public.{name}{signature} to {RUNTIME_LOGINS}"
        )


def downgrade(op) -> None:
    for name, signature in RECORDED_VERBAL_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    op.execute(RECORDED_VERBAL_SEGMENT_RESTORE_COLUMN)
    _with_append_only_guards_lifted(
        op.get_bind(), _restore_legacy_recorded_verbal_state
    )
    op.execute(RECORDED_VERBAL_SEGMENT_CUTOVER_DOWN)
    for table in RECORDED_VERBAL_TABLES:
        op.execute(f"revoke select on public.{table} from {RUNTIME_LOGINS}")
    for table in RECORDED_VERBAL_APPEND_TABLES:
        op.execute(f"revoke select, insert on public.{table} from {SOURCE_APPEND_ROLE}")
    op.execute(
        "revoke usage, select on sequence public.recorded_verbal_origins_id_seq "
        f"from {SOURCE_APPEND_ROLE}"
    )
    op.execute(RECORDED_VERBAL_ORIGIN_SCHEMA_DOWN)
