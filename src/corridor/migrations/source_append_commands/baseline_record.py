"""Adopt Baseline (#509, ADR-0076, ADR-0083).

Adopt Baseline is the one bulk human act that turns the customer's own UCM
workbook or system export into the initial accepted Project Record.  It is
folded in here for the same reason as the two relations above: the migration
window holds one unreleased transition and this is it.

Three identities are deliberately three things, because conflating them is
how a later format change becomes an unattributed edit of accepted values:

  * ``project_baseline_sources`` is the **accepted data-baseline identity** —
    the exact bytes, digest, customer, source identity, adopted worksheet or
    record scope, importer identity and version, and the coordinator preview
    the named person actually adopted.  One row per project, so a project has
    one initial Adopt Baseline and a second is refused rather than silently
    overwriting the first.
  * ``project_baseline_source_rows`` is **source-row identity**, kept distinct
    from Project Record subject identity so two rows that repeat one Utility
    Conflict ID never collapse into one subject.  It also carries the external
    system identifiers and source URLs the workbook itself printed, so a later
    read-only deep link (#527, #528) resolves without re-reading the file.
  * ``project_baseline_formats`` is the **output-template identity** and the
    **field-mapping identity**, registered separately and replaceable on their
    own attributable act.  Registering a replacement supersedes its
    predecessor and writes no accepted value; it is not a second adoption.

The write path is ``adopt_project_record_baseline``: one ``SECURITY DEFINER``
command owned by the record-decision role that writes one Project Record
revision, one separately identified ``fact_decisions`` row per adopted Source
Fact, the three identities, and every source row — or nothing.  The
application runtime role reads all three tables and writes none of them, so a
partial commit is not something a caller can construct.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.operating_mode import (
    OPERATING_MODE_ROLE,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


BASELINE_RECORD_TABLES = (
    "project_baseline_sources",
    "project_baseline_source_rows",
    "project_baseline_formats",
)

BASELINE_RECORD_SCHEMA = """
create table public.project_baseline_sources (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    revision_id bigint not null references public.project_record_revisions (id),
    document_id bigint not null references public.documents (id),
    content_sha256 character varying(64) not null,
    byte_size bigint not null,
    filename text not null,
    -- The customer's own name for this exact revision, and whose record it is.
    source_identity character varying(160) not null,
    customer character varying(160) not null,
    source_kind character varying(32) not null,
    -- The adopted worksheet or record scope, the columns no canonical field
    -- names, the coordinator questions the adopting person saw, and the
    -- Corridor operations reading that resolved the workbook mechanics. Kept
    -- because an unknown column that is not retained is an unknown column that
    -- was silently discarded.
    worksheet_scope jsonb not null,
    unknown_columns jsonb not null,
    coordinator_questions jsonb not null,
    operations_summary jsonb not null,
    importer_identity character varying(128) not null,
    importer_version character varying(64) not null,
    preview_fingerprint character varying(64) not null,
    adopted_by_principal character varying(128) not null,
    idempotency_key character varying(160) not null,
    adopted_at timestamp with time zone not null default now(),
    constraint uq_project_baseline_sources_project unique (project_id),
    constraint uq_project_baseline_sources_project_id unique (project_id, id),
    constraint uq_project_baseline_sources_revision unique (revision_id),
    constraint ck_project_baseline_sources_digest check (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_project_baseline_sources_preview check (
        preview_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_project_baseline_sources_kind check (
        source_kind in ('ucm_workbook', 'system_export')
    ),
    constraint ck_project_baseline_sources_principal check (
        length(btrim(adopted_by_principal)) > 0
    ),
    constraint ck_project_baseline_sources_byte_size check (byte_size > 0)
);

create index ix_project_baseline_sources_project_id
    on public.project_baseline_sources (project_id);

create table public.project_baseline_source_rows (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    baseline_source_id bigint not null,
    source_row_key character varying(160) not null,
    sheet_name text not null,
    row_number integer not null,
    business_identity character varying(128),
    record_subject_key character varying(160),
    external_system_id character varying(160),
    source_url text,
    excluded boolean not null default false,
    exclusion_reason character varying(64),
    constraint fk_project_baseline_source_rows_source foreign key
        (project_id, baseline_source_id)
        references public.project_baseline_sources (project_id, id),
    constraint uq_project_baseline_source_rows_key unique
        (baseline_source_id, source_row_key),
    constraint uq_project_baseline_source_rows_subject unique
        (baseline_source_id, record_subject_key),
    constraint ck_project_baseline_source_rows_row_number check (row_number > 0),
    -- An adopted row resolves to exactly one Project Record subject; an
    -- excluded row resolves to none and says why. Neither is ever a row the
    -- adoption dropped without saying so.
    constraint ck_project_baseline_source_rows_exclusion check (
        (excluded and record_subject_key is null and exclusion_reason is not null)
        or (not excluded and record_subject_key is not null
            and exclusion_reason is null)
    )
);

create index ix_project_baseline_source_rows_project_id
    on public.project_baseline_source_rows (project_id);
create index ix_project_baseline_source_rows_source
    on public.project_baseline_source_rows (baseline_source_id);

create table public.project_baseline_formats (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    format_kind character varying(32) not null,
    format_identity character varying(160) not null,
    format_version character varying(64) not null,
    content_sha256 character varying(64) not null,
    registered_by_principal character varying(128) not null,
    idempotency_key character varying(160) not null,
    superseded_by bigint references public.project_baseline_formats (id)
        deferrable initially deferred,
    registered_at timestamp with time zone not null default now(),
    constraint uq_project_baseline_formats_key unique
        (project_id, idempotency_key),
    constraint uq_project_baseline_formats_superseded_by unique (superseded_by),
    constraint ck_project_baseline_formats_kind check (
        format_kind in ('output_template', 'field_mapping')
    ),
    constraint ck_project_baseline_formats_digest check (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_project_baseline_formats_principal check (
        length(btrim(registered_by_principal)) > 0
    )
);

create index ix_project_baseline_formats_project_id
    on public.project_baseline_formats (project_id);
create unique index uq_project_baseline_formats_effective
    on public.project_baseline_formats (project_id, format_kind)
    where superseded_by is null;

create function public.enforce_project_baseline_record_write() returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Adopt Baseline requires the typed adoption command'
                    using errcode='23514';
            end if;
            if tg_op <> 'INSERT' then
                raise exception 'The adopted baseline is append-only: a replacement is a new record change, never an edit'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create function public.enforce_project_baseline_format_write() returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Adopt Baseline requires the typed adoption command'
                    using errcode='23514';
            end if;
            if tg_op = 'INSERT' then
                return new;
            end if;
            if tg_op = 'UPDATE' then
                if new.id is distinct from old.id
                   or new.project_id is distinct from old.project_id
                   or new.format_kind is distinct from old.format_kind
                   or new.format_identity is distinct from old.format_identity
                   or new.format_version is distinct from old.format_version
                   or new.content_sha256 is distinct from old.content_sha256
                   or new.registered_by_principal
                       is distinct from old.registered_by_principal
                   or new.idempotency_key is distinct from old.idempotency_key
                   or new.registered_at is distinct from old.registered_at
                   or old.superseded_by is not null
                   or new.superseded_by is null then
                    raise exception 'A registered format may only become superseded once'
                        using errcode='23514';
                end if;
                return new;
            end if;
            raise exception 'The adopted baseline is append-only: a replacement is a new record change, never an edit'
                using errcode='23514';
        end; $$;

create trigger trg_project_baseline_sources_write
    before insert or update or delete on public.project_baseline_sources
    for each row execute function public.enforce_project_baseline_record_write();
create trigger trg_project_baseline_sources_truncate
    before truncate on public.project_baseline_sources
    for each statement
    execute function public.enforce_project_baseline_record_write();

create trigger trg_project_baseline_source_rows_write
    before insert or update or delete on public.project_baseline_source_rows
    for each row execute function public.enforce_project_baseline_record_write();
create trigger trg_project_baseline_source_rows_truncate
    before truncate on public.project_baseline_source_rows
    for each statement
    execute function public.enforce_project_baseline_record_write();

create trigger trg_project_baseline_formats_write
    before insert or update or delete on public.project_baseline_formats
    for each row execute function public.enforce_project_baseline_format_write();
create trigger trg_project_baseline_formats_truncate
    before truncate on public.project_baseline_formats
    for each statement
    execute function public.enforce_project_baseline_format_write();
"""

BASELINE_RECORD_SCHEMA_DOWN = """
drop table if exists public.project_baseline_source_rows cascade;
drop table if exists public.project_baseline_formats cascade;
drop table if exists public.project_baseline_sources cascade;
drop function if exists public.enforce_project_baseline_record_write() cascade;
drop function if exists public.enforce_project_baseline_format_write() cascade;
"""

ADOPT_PROJECT_RECORD_BASELINE = """
create function public.adopt_project_record_baseline(
    p_project_id bigint,
    p_principal character varying,
    p_idempotency_key character varying,
    p_baseline jsonb,
    p_rows jsonb,
    p_formats jsonb,
    p_fact_ids bigint[]
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing project_baseline_sources%ROWTYPE;
            adopted_document bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_source bigint;
            row_payload jsonb;
            format_payload jsonb;
            decided facts%ROWTYPE;
            fact_id bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'Adopt Baseline names the person adopting'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'Adopt Baseline needs an idempotency key'
                    using errcode='23514';
            end if;
            adopted_document := (p_baseline->>'document_id')::bigint;

            select * into existing from project_baseline_sources
             where project_id = p_project_id;
            if found then
                if existing.idempotency_key is distinct from p_idempotency_key
                   or existing.content_sha256
                       is distinct from (p_baseline->>'content_sha256')
                   or existing.preview_fingerprint
                       is distinct from (p_baseline->>'preview_fingerprint') then
                    raise exception 'project % already adopted a baseline; replacing it is a new record change, not another initial adoption', p_project_id
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing.revision_id,
                    'baseline_source_id', existing.id,
                    'created', false
                );
            end if;

            if not exists (
                select 1 from documents
                 where id = adopted_document and project_id = p_project_id
            ) then
                raise exception 'the adopted source belongs to another project'
                    using errcode='23514';
            end if;

            -- A nonempty accepted Project Record is never silently adopted
            -- over: it needs an explicit migration, a reconciliation, or a
            -- fresh customer environment.
            if exists (
                select 1 from fact_decisions
                 where project_id = p_project_id and superseded_by is null
            ) or exists (
                select 1 from dependencies where project_id = p_project_id
            ) then
                raise exception 'project % already holds an accepted Project Record; adopting a baseline over it needs an explicit migration or reconciliation, or a fresh environment', p_project_id
                    using errcode='23514';
            end if;

            if exists (
                select 1 from unnest(coalesce(p_fact_ids, '{}'::bigint[]))
                          as requested(id)
                 left join facts
                        on facts.id = requested.id
                       and facts.project_id = p_project_id
                       and facts.document_id = adopted_document
                 where facts.id is null
            ) then
                raise exception 'Adopt Baseline decides only Source Facts this project captured from the adopted source'
                    using errcode='23514';
            end if;

            select max(id) into predecessor_revision
              from project_record_revisions where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, 'adopt_baseline',
                p_principal, null, p_idempotency_key
            ) returning id into new_revision;

            insert into project_baseline_sources (
                project_id, revision_id, document_id, content_sha256, byte_size,
                filename, source_identity, customer, source_kind,
                worksheet_scope, unknown_columns, coordinator_questions,
                operations_summary, importer_identity, importer_version,
                preview_fingerprint, adopted_by_principal, idempotency_key
            ) values (
                p_project_id, new_revision, adopted_document,
                p_baseline->>'content_sha256',
                (p_baseline->>'byte_size')::bigint,
                p_baseline->>'filename', p_baseline->>'source_identity',
                p_baseline->>'customer', p_baseline->>'source_kind',
                p_baseline->'worksheet_scope', p_baseline->'unknown_columns',
                p_baseline->'coordinator_questions',
                p_baseline->'operations_summary',
                p_baseline->>'importer_identity',
                p_baseline->>'importer_version',
                p_baseline->>'preview_fingerprint', p_principal,
                p_idempotency_key
            ) returning id into new_source;

            for row_payload in
                select value from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb))
            loop
                insert into project_baseline_source_rows (
                    project_id, baseline_source_id, source_row_key, sheet_name,
                    row_number, business_identity, record_subject_key,
                    external_system_id, source_url, excluded, exclusion_reason
                ) values (
                    p_project_id, new_source, row_payload->>'source_row_key',
                    row_payload->>'sheet_name',
                    (row_payload->>'row_number')::integer,
                    row_payload->>'business_identity',
                    row_payload->>'record_subject_key',
                    row_payload->>'external_system_id',
                    row_payload->>'source_url',
                    coalesce((row_payload->>'excluded')::boolean, false),
                    row_payload->>'exclusion_reason'
                );
            end loop;

            for format_payload in
                select value
                  from jsonb_array_elements(coalesce(p_formats, '[]'::jsonb))
            loop
                insert into project_baseline_formats (
                    project_id, format_kind, format_identity, format_version,
                    content_sha256, registered_by_principal, idempotency_key
                ) values (
                    p_project_id, format_payload->>'format_kind',
                    format_payload->>'format_identity',
                    format_payload->>'format_version',
                    format_payload->>'content_sha256', p_principal,
                    p_idempotency_key || ':' || (format_payload->>'format_kind')
                );
            end loop;

            foreach fact_id in array coalesce(p_fact_ids, '{}'::bigint[])
            loop
                select * into decided from facts where id = fact_id;
                insert into fact_decisions (
                    project_id, fact_id, subject_key, fact_type, revision_id,
                    disposition, superseded_by
                ) values (
                    p_project_id, fact_id, decided.subject_key,
                    decided.fact_type, new_revision, 'include', null
                );
            end loop;

            return jsonb_build_object(
                'revision_id', new_revision,
                'baseline_source_id', new_source,
                'created', true
            );
        end; $$;
"""

ADOPT_PROJECT_RECORD_BASELINE_SIGNATURE = (
    "(bigint, character varying, character varying, jsonb, jsonb, jsonb, bigint[])"
)

REGISTER_BASELINE_FORMAT = """
create function public.register_baseline_format(
    p_project_id bigint,
    p_format_kind character varying,
    p_format_identity character varying,
    p_format_version character varying,
    p_content_sha256 character varying,
    p_principal character varying,
    p_idempotency_key character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing project_baseline_formats%ROWTYPE;
            superseded bigint;
            new_id bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'a format registration names the person registering it'
                    using errcode='23514';
            end if;
            select * into existing from project_baseline_formats
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if existing.format_kind is distinct from p_format_kind
                   or existing.content_sha256 is distinct from p_content_sha256 then
                    raise exception 'the format registration key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'format_id', existing.id, 'created', false
                );
            end if;
            if not exists (
                select 1 from project_baseline_sources
                 where project_id = p_project_id
            ) then
                raise exception 'a project registers an output template or field mapping only after it adopts its data baseline'
                    using errcode='23514';
            end if;
            select id into superseded from project_baseline_formats
             where project_id = p_project_id
               and format_kind = p_format_kind
               and superseded_by is null;
            new_id := nextval('project_baseline_formats_id_seq');
            if superseded is not null then
                update project_baseline_formats set superseded_by = new_id
                 where id = superseded;
            end if;
            insert into project_baseline_formats (
                id, project_id, format_kind, format_identity, format_version,
                content_sha256, registered_by_principal, idempotency_key,
                superseded_by
            ) values (
                new_id, p_project_id, p_format_kind, p_format_identity,
                p_format_version, p_content_sha256, p_principal,
                p_idempotency_key, null
            );
            return jsonb_build_object('format_id', new_id, 'created', true);
        end; $$;
"""

REGISTER_BASELINE_FORMAT_SIGNATURE = (
    "(bigint, character varying, character varying, character varying, "
    "character varying, character varying, character varying)"
)

BASELINE_RECORD_COMMANDS = {
    "adopt_project_record_baseline": ADOPT_PROJECT_RECORD_BASELINE_SIGNATURE,
    "register_baseline_format": REGISTER_BASELINE_FORMAT_SIGNATURE,
}


def upgrade(op) -> None:
    op.execute(BASELINE_RECORD_SCHEMA)
    op.execute(ADOPT_PROJECT_RECORD_BASELINE)
    op.execute(REGISTER_BASELINE_FORMAT)
    for table in BASELINE_RECORD_TABLES:
        # The application reads the adopted baseline and writes none of it;
        # a new table arrives with the schema's default privileges, so the
        # write half is taken back explicitly.
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
        op.execute(
            f"grant select, insert on public.{table} to {OPERATING_MODE_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {OPERATING_MODE_ROLE}"
        )
    # Supersession is the one change a registered format ever sees, and the
    # guard trigger holds it to that column, once.
    op.execute(
        "grant update (superseded_by) on public.project_baseline_formats "
        f"to {OPERATING_MODE_ROLE}"
    )
    for name, signature in BASELINE_RECORD_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to "
            f"{OPERATING_MODE_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        # Adopt Baseline and a later format registration are attributable human
        # acts, so they join the other decision commands on the web capability
        # alone (#509, ADR-0076).
        op.execute(
            f"grant execute on function public.{name}{signature} to corridor_web"
        )


def downgrade(op) -> None:
    for name, signature in BASELINE_RECORD_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    for table in BASELINE_RECORD_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(BASELINE_RECORD_SCHEMA_DOWN)
