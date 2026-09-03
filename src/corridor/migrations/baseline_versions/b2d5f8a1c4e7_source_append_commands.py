"""Source appends become commands the source-append role owns (#492).

Revision ID: b2d5f8a1c4e7
Revises: a1c4e7b0d2f3

The baseline created ``corridor_source_append`` and gave it nothing to own:
the runtime capabilities still held raw ``INSERT`` on ``source_segments``,
``facts``, and the Extracted Proposal tables, so the application could write a
segment whose digest did not match its words, a Fact whose source lay in
another project, or a proposal grouping Facts from another run, and the
database would accept every one of them.

This revision moves those appends behind four narrow ``SECURITY DEFINER``
commands.  Each enforces what the table constraints cannot: project scope of
every typed reference, the digest of every exact text it stores or cites,
locator identity, and idempotent replay.  The runtime capabilities keep
``SELECT`` on the source tables, lose every write on them, and receive
``EXECUTE`` on the commands alone.  The same test that holds the accepted
boundary (``tests/test_database_authority.py``) proves the append boundary
against the real logins.

The legacy ``candidates`` table stays where it is: it is the frozen legacy
Extracted Proposal representation (ADR-0081), not the spine.  Support
assessments do not exist yet; #530 extends this matrix when it adds them.
"""

from __future__ import annotations

from alembic import op


revision = "b2d5f8a1c4e7"
down_revision = "a1c4e7b0d2f3"
branch_labels = None
depends_on = None


SOURCE_APPEND_ROLE = "corridor_source_append"
RUNTIME_LOGINS = "corridor_web, corridor_worker"

# Tables the application may now append to only through a command.
SOURCE_TABLES = (
    "source_segments",
    "facts",
    "fact_sources",
    "fact_applies_to",
    "fact_closure_results",
    "fact_closure_sources",
    "fact_statement_timings",
    "extracted_proposals",
    "extracted_proposal_facts",
    "source_fact_append_receipts",
)

# Tables the commands read to prove a typed reference lies in scope.
REFERENCED_TABLES = (
    "documents",
    "dependency_events",
    "dependencies",
    "candidates",
    "extraction_runs",
)

COMMANDS = {
    "append_source_segments": "(bigint, bigint, bigint, jsonb)",
    "append_fact": (
        "(bigint, bigint, bigint, character varying, character varying, text, "
        "text, date, bigint, bigint, character varying, character varying, "
        "character varying, jsonb, jsonb)"
    ),
    "append_extracted_proposal": (
        "(bigint, bigint, bigint, bigint, character varying, text, jsonb, bigint[])"
    ),
    "append_source_fact_receipt": (
        "(bigint, bigint, bigint, character varying, character varying)"
    ),
}


APPEND_SOURCE_SEGMENTS = """
create function public.append_source_segments(
    p_project_id bigint,
    p_document_id bigint,
    p_statement_id bigint,
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
            if (p_document_id is null) = (p_statement_id is null) then
                raise exception 'source segments belong to one document or one statement'
                    using errcode = '23514';
            end if;
            if p_document_id is not null and not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'source segment document is outside its project'
                    using errcode = '23514';
            end if;
            if p_statement_id is not null and not exists (
                select 1 from dependency_events
                 where id = p_statement_id and project_id = p_project_id
            ) then
                raise exception 'source segment statement is outside its project'
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
                       and statement_id = p_statement_id;
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
                    project_id, document_id, statement_id, kind, exact_text,
                    content_sha256, ordinal, sheet_name, cell_range, page_no,
                    start_offset, end_offset
                ) values (
                    p_project_id, p_document_id, p_statement_id, segment_kind, exact,
                    digest, (item ->> 'ordinal')::integer, item ->> 'sheet_name',
                    item ->> 'cell_range', (item ->> 'page_no')::integer,
                    (item ->> 'start_offset')::integer, (item ->> 'end_offset')::integer
                ) returning id into segment_id;
                appended := appended || segment_id;
            end loop;
            return appended;
        end; $$;
"""


APPEND_FACT = """
create function public.append_fact(
    p_project_id bigint,
    p_document_id bigint,
    p_extraction_run_id bigint,
    p_fact_type character varying,
    p_subject_kind character varying,
    p_subject_key text,
    p_text_value text,
    p_date_value date,
    p_external_org_value_id bigint,
    p_document_value_id bigint,
    p_transformation character varying,
    p_recorded_by character varying,
    p_content_sha256 character varying,
    p_sources jsonb,
    p_satellites jsonb
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            new_id bigint;
            link jsonb;
            segment record;
            member text;
            timing jsonb;
            slot integer;
        begin
            if p_content_sha256 is null or p_content_sha256 !~ '^[0-9a-f]{64}$' then
                raise exception 'appended Fact needs its content digest'
                    using errcode = '23514';
            end if;
            if p_document_id is not null and not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'Fact document is outside its project'
                    using errcode = '23514';
            end if;
            if p_extraction_run_id is not null and not exists (
                select 1 from extraction_runs
                 where id = p_extraction_run_id and document_id = p_document_id
            ) then
                raise exception 'Fact extraction run belongs to another document'
                    using errcode = '23514';
            end if;
            if p_document_value_id is not null and not exists (
                select 1 from documents
                 where id = p_document_value_id and project_id = p_project_id
            ) then
                raise exception 'Fact document value is outside its project'
                    using errcode = '23514';
            end if;
            if p_sources is null or jsonb_typeof(p_sources) <> 'array' then
                raise exception 'Fact sources must be a list'
                    using errcode = '23514';
            end if;
            if p_satellites is not null and jsonb_typeof(p_satellites) <> 'object' then
                raise exception 'Fact satellites must be an object'
                    using errcode = '23514';
            end if;

            -- Replay: the digest covers every value, reference, and source
            -- link, so an identical digest is the same Fact appended again.
            select id, project_id into existing from facts
             where content_sha256 = p_content_sha256;
            if found then
                if existing.project_id <> p_project_id then
                    raise exception 'Fact digest is already bound in another project'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;

            insert into facts (
                project_id, document_id, extraction_run_id, fact_type,
                subject_kind, subject_key, text_value, date_value,
                date_range_start, date_range_end, external_org_value_id,
                document_value_id, transformation, recorded_by, content_sha256
            ) values (
                p_project_id, p_document_id, p_extraction_run_id, p_fact_type,
                p_subject_kind, p_subject_key, p_text_value, p_date_value,
                null, null, p_external_org_value_id,
                p_document_value_id, p_transformation, p_recorded_by, p_content_sha256
            ) returning id into new_id;

            for link in select value from jsonb_array_elements(p_sources) loop
                select id, project_id, document_id, exact_text, content_sha256
                  into segment from source_segments
                 where id = (link ->> 'source_segment_id')::bigint;
                if not found or segment.project_id <> p_project_id then
                    raise exception 'Fact source segment is outside its project'
                        using errcode = '23514';
                end if;
                if p_document_id is not null
                    and segment.document_id is distinct from p_document_id then
                    raise exception 'Fact source belongs to another rendition'
                        using errcode = '23514';
                end if;
                if encode(sha256(convert_to(segment.exact_text, 'UTF8')), 'hex')
                    <> segment.content_sha256 then
                    raise exception 'Fact source segment digest does not match its text'
                        using errcode = '23514';
                end if;
                insert into fact_sources (
                    project_id, document_id, fact_id, source_segment_id, role, ordinal
                ) values (
                    p_project_id, p_document_id, new_id, segment.id,
                    link ->> 'role', coalesce((link ->> 'ordinal')::integer, 1)
                );
            end loop;

            if p_satellites ? 'applies_to' then
                slot := 0;
                for member in
                    select value from jsonb_array_elements_text(p_satellites -> 'applies_to')
                loop
                    slot := slot + 1;
                    if not exists (
                        select 1 from dependencies
                         where id = member::bigint and project_id = p_project_id
                    ) then
                        raise exception 'Applies To member is outside its project'
                            using errcode = '23514';
                    end if;
                    insert into fact_applies_to (
                        project_id, fact_id, dependency_id, ordinal
                    ) values (p_project_id, new_id, member::bigint, slot);
                end loop;
            end if;

            if p_satellites ? 'closure' then
                if (p_satellites -> 'closure' ->> 'successor_dependency_id') is not null
                    and not exists (
                        select 1 from dependencies
                         where id = (p_satellites -> 'closure' ->> 'successor_dependency_id')::bigint
                           and project_id = p_project_id
                    ) then
                    raise exception 'closure successor is outside its project'
                        using errcode = '23514';
                end if;
                insert into fact_closure_results (
                    project_id, fact_id, closure_kind, successor_dependency_id
                ) values (
                    p_project_id, new_id,
                    p_satellites -> 'closure' ->> 'closure_kind',
                    (p_satellites -> 'closure' ->> 'successor_dependency_id')::bigint
                );
                slot := 0;
                for member in
                    select value from jsonb_array_elements_text(
                        p_satellites -> 'closure' -> 'governing_source_segment_ids'
                    )
                loop
                    slot := slot + 1;
                    select id, project_id, document_id into segment
                      from source_segments where id = member::bigint;
                    if not found or segment.project_id <> p_project_id
                        or segment.document_id is distinct from p_document_id then
                        raise exception 'closure governing source crosses rendition'
                            using errcode = '23514';
                    end if;
                    insert into fact_closure_sources (
                        project_id, document_id, fact_id, source_segment_id, ordinal
                    ) values (p_project_id, p_document_id, new_id, segment.id, slot);
                end loop;
            end if;

            if p_satellites ? 'timings' then
                for timing in
                    select value from jsonb_array_elements(p_satellites -> 'timings')
                loop
                    insert into fact_statement_timings (
                        project_id, fact_id, timing_role, text, precision,
                        start_date, end_date
                    ) values (
                        p_project_id, new_id, timing ->> 'role', timing ->> 'text',
                        timing ->> 'precision', (timing ->> 'start_date')::date,
                        (timing ->> 'end_date')::date
                    );
                end loop;
            end if;

            return new_id;
        end; $$;
"""


APPEND_EXTRACTED_PROPOSAL = """
create function public.append_extracted_proposal(
    p_project_id bigint,
    p_document_id bigint,
    p_extraction_run_id bigint,
    p_candidate_id bigint,
    p_kind character varying,
    p_subject_key text,
    p_candidate_metadata jsonb,
    p_fact_ids bigint[]
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            new_id bigint;
            member bigint;
            fact record;
            slot integer := 0;
        begin
            if not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'Extracted Proposal document is outside its project'
                    using errcode = '23514';
            end if;
            if not exists (
                select 1 from extraction_runs
                 where id = p_extraction_run_id and document_id = p_document_id
            ) then
                raise exception 'Extracted Proposal run belongs to another document'
                    using errcode = '23514';
            end if;
            if not exists (
                select 1 from candidates
                 where id = p_candidate_id and project_id = p_project_id
                   and source_document_id = p_document_id
            ) then
                raise exception 'Extracted Proposal Candidate is outside its rendition'
                    using errcode = '23514';
            end if;
            if p_fact_ids is null or cardinality(p_fact_ids) = 0 then
                raise exception 'Extracted Proposal needs at least one Fact'
                    using errcode = '23514';
            end if;
            select id, candidate_id into existing from extracted_proposals
             where extraction_run_id = p_extraction_run_id
               and subject_key = p_subject_key;
            if found then
                if existing.candidate_id <> p_candidate_id then
                    raise exception 'Extracted Proposal subject is already bound to another Candidate'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;
            insert into extracted_proposals (
                project_id, document_id, extraction_run_id, candidate_id, kind,
                subject_key, candidate_metadata_json
            ) values (
                p_project_id, p_document_id, p_extraction_run_id, p_candidate_id,
                p_kind, p_subject_key, p_candidate_metadata
            ) returning id into new_id;
            foreach member in array p_fact_ids loop
                slot := slot + 1;
                select project_id, document_id, extraction_run_id, subject_key
                  into fact from facts where id = member;
                if not found or fact.project_id <> p_project_id
                    or fact.document_id is distinct from p_document_id
                    or fact.extraction_run_id is distinct from p_extraction_run_id then
                    raise exception 'Extracted Proposal Fact belongs to another run'
                        using errcode = '23514';
                end if;
                if fact.subject_key <> p_subject_key then
                    raise exception 'Extracted Proposal Fact names another subject'
                        using errcode = '23514';
                end if;
                insert into extracted_proposal_facts (
                    project_id, document_id, extraction_run_id, proposal_id,
                    fact_id, ordinal
                ) values (
                    p_project_id, p_document_id, p_extraction_run_id, new_id,
                    member, slot
                );
            end loop;
            return new_id;
        end; $$;
"""


APPEND_SOURCE_FACT_RECEIPT = """
create function public.append_source_fact_receipt(
    p_project_id bigint,
    p_document_id bigint,
    p_extraction_run_id bigint,
    p_idempotency_key character varying,
    p_content_sha256 character varying
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            new_id bigint;
        begin
            if not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'source Fact append receipt document is outside its project'
                    using errcode = '23514';
            end if;
            if not exists (
                select 1 from extraction_runs
                 where id = p_extraction_run_id and document_id = p_document_id
            ) then
                raise exception 'source Fact append receipt run belongs to another document'
                    using errcode = '23514';
            end if;
            select id, content_sha256 into existing from source_fact_append_receipts
             where project_id = p_project_id and idempotency_key = p_idempotency_key;
            if found then
                if existing.content_sha256 <> p_content_sha256 then
                    raise exception 'source Fact append key is already bound to different content'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;
            insert into source_fact_append_receipts (
                project_id, document_id, extraction_run_id, idempotency_key,
                content_sha256
            ) values (
                p_project_id, p_document_id, p_extraction_run_id, p_idempotency_key,
                p_content_sha256
            ) returning id into new_id;
            return new_id;
        end; $$;
"""


def upgrade() -> None:
    """Create the append commands and take back the raw source-table writes."""

    op.execute(f"grant usage on schema public to {SOURCE_APPEND_ROLE}")
    for table in REFERENCED_TABLES:
        op.execute(f"grant select on public.{table} to {SOURCE_APPEND_ROLE}")
    for table in SOURCE_TABLES:
        op.execute(f"grant select, insert on public.{table} to {SOURCE_APPEND_ROLE}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {SOURCE_APPEND_ROLE}"
        )

    for body in (
        APPEND_SOURCE_SEGMENTS,
        APPEND_FACT,
        APPEND_EXTRACTED_PROPOSAL,
        APPEND_SOURCE_FACT_RECEIPT,
    ):
        op.execute(body)
    for name, signature in COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {SOURCE_APPEND_ROLE}"
        )
        # PostgreSQL grants EXECUTE to PUBLIC on every new function, and a
        # default-privilege revoke does not persist (#545); each command
        # takes PUBLIC back itself.
        op.execute(f"revoke all on function public.{name}{signature} from public")
        op.execute(
            f"grant execute on function public.{name}{signature} to {RUNTIME_LOGINS}"
        )

    for table in SOURCE_TABLES:
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )


def downgrade() -> None:
    """Drop the commands and hand the raw source-table writes back."""

    for table in SOURCE_TABLES:
        op.execute(
            f"grant insert, update, delete on public.{table} to {RUNTIME_LOGINS}"
        )
    for name, signature in COMMANDS.items():
        op.execute(f"drop function public.{name}{signature}")
    for table in SOURCE_TABLES:
        op.execute(
            f"revoke usage, select on sequence public.{table}_id_seq "
            f"from {SOURCE_APPEND_ROLE}"
        )
        op.execute(f"revoke select, insert on public.{table} from {SOURCE_APPEND_ROLE}")
    for table in REFERENCED_TABLES:
        op.execute(f"revoke select on public.{table} from {SOURCE_APPEND_ROLE}")
    op.execute(f"revoke usage on schema public from {SOURCE_APPEND_ROLE}")
