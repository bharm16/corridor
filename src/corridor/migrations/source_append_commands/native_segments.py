"""Native PDF readings, their locators, and pipeline qualification (#736, #737).

#737: PDF Facts remain captured source values; no accepted-record rule changes.
Freeze the predecessor expression here. The historical schema bytes stay inert
and future model edits cannot silently change this supported transition.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.recorded_verbal import (
    APPEND_SOURCE_SEGMENTS_OVER_ORIGIN,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


FACTS_TYPED_VALUE_BEFORE_PDF = """(fact_type IN ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', 'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', 'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', 'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', 'offset_side', 'potential_conflict', 'data_source', 'marked_resolution')) AND text_value IS NOT NULL AND length(TRIM(BOTH FROM text_value)) > 0 AND date_value IS NULL AND date_range_start IS NULL AND date_range_end IS NULL AND (fact_type::text = 'external_org'::text OR external_org_value_id IS NULL) AND document_value_id IS NULL AND transformation::text = 'trim_cell_text_v1'::text OR (fact_type IN ('committed_date', 'action_due_date', 'need_date')) AND text_value IS NULL AND date_value IS NOT NULL AND date_range_start IS NULL AND date_range_end IS NULL AND external_org_value_id IS NULL AND document_value_id IS NULL AND transformation::text = 'iso_date_cell_v1'::text OR fact_type::text = 'applies_to'::text AND text_value IS NULL AND date_value IS NULL AND date_range_start IS NULL AND date_range_end IS NULL AND external_org_value_id IS NULL AND document_value_id IS NULL AND transformation::text = 'structured_reference_set_v1'::text OR fact_type::text = 'closure_result'::text AND text_value IS NULL AND date_value IS NULL AND date_range_start IS NULL AND date_range_end IS NULL AND external_org_value_id IS NULL AND document_value_id IS NULL AND transformation::text = 'typed_closure_result_v1'::text OR fact_type::text = 'statement_wording'::text AND text_value IS NOT NULL AND length(TRIM(BOTH FROM text_value)) > 0 AND date_value IS NULL AND date_range_start IS NULL AND date_range_end IS NULL AND external_org_value_id IS NULL AND document_value_id IS NULL AND transformation::text = 'exact_prose_span_v1'::text OR fact_type::text = 'statement_timing'::text AND text_value IS NULL AND date_value IS NULL AND date_range_start IS NULL AND date_range_end IS NULL AND external_org_value_id IS NULL AND document_value_id IS NULL AND transformation::text = 'typed_statement_timing_v1'::text OR fact_type::text = 'supporting_documentation_in_use'::text AND text_value IS NULL AND date_value IS NULL AND date_range_start IS NULL AND date_range_end IS NULL AND external_org_value_id IS NULL AND document_value_id IS NOT NULL AND transformation::text = 'supporting_document_revision_v1'::text"""
FACTS_TYPED_VALUE_WITH_PDF = FACTS_TYPED_VALUE_BEFORE_PDF.replace(
    "transformation::text = 'trim_cell_text_v1'::text",
    "(transformation in ('trim_cell_text_v1', 'collapse_pdf_whitespace_v1') "
    "or (fact_type = 'resolution_strategy' and transformation = 'pdf_marked_resolution_v1'))",
    1,
)


# #737: native receipts have their own fail-closed shape and completion checks.
NATIVE_ROW_ACCOUNTING_COMPLETENESS_BEFORE = '''
            not (
                outcome = 'completed'
                and prompt_version in (
                    'sheet_native_v2', 'matrix_tiered_v4',
                    'prose_interpretation_v1'
                )
            ) or (
                row_accounting_json is not null
                and (
                    (
                        row_accounting_json ->> 'schema_version' =
                            'matrix-row-accounting-v1'
                        and jsonb_array_length(
                            row_accounting_json -> 'unaccounted_rows'
                        ) = 0
                        and (row_accounting_json ->> 'accounted_row_count')::integer =
                            (row_accounting_json ->> 'detected_row_count')::integer
                        and (row_accounting_json ->> 'extracted_row_count')::integer =
                            candidate_count
                    ) or (
                        row_accounting_json ->> 'schema_version' =
                            'prose-segment-accounting-v1'
                        and (row_accounting_json ->> 'proposed_fact_count')::integer =
                            candidate_count
                    )
                )
            )
            '''
NATIVE_ROW_ACCOUNTING_COMPLETENESS = ('''
            case when prompt_version = 'matrix_structure_ids_v1'
            then (
            outcome <> 'completed' or (
                row_accounting_json is not null
                and row_accounting_json ->> 'schema_version' = 'native-matrix-row-accounting-v1'
                and jsonb_array_length(row_accounting_json -> 'unaccounted_rows') = 0
                and (row_accounting_json ->> 'accounted_row_count')::integer =
                    (row_accounting_json ->> 'detected_row_count')::integer
                and (row_accounting_json ->> 'extracted_row_count')::integer = candidate_count
            ) is true
            ) else (''' + NATIVE_ROW_ACCOUNTING_COMPLETENESS_BEFORE + ''') end
            ''')

NATIVE_ROW_ACCOUNTING_SHAPE_BEFORE = '''
            row_accounting_json is null or (
                jsonb_typeof(row_accounting_json) = 'object'
                and row_accounting_json ->> 'reader_version' = prompt_version
                and (
                    (
                        row_accounting_json ?& array[
                            'schema_version', 'reader_version', 'reader_path',
                            'detected_row_count', 'accounted_row_count',
                            'extracted_row_count', 'blank_row_count',
                            'skipped_row_count', 'unaccounted_rows', 'rows'
                        ]
                        and row_accounting_json ->> 'schema_version' =
                            'matrix-row-accounting-v1'
                        and jsonb_typeof(row_accounting_json -> 'rows') = 'array'
                        and jsonb_typeof(
                            row_accounting_json -> 'unaccounted_rows'
                        ) = 'array'
                        and row_accounting_json ->> 'detected_row_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'accounted_row_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'extracted_row_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'blank_row_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'skipped_row_count' ~ '^[0-9]+$'
                        and jsonb_array_length(row_accounting_json -> 'rows') =
                            (row_accounting_json ->> 'detected_row_count')::integer
                        and (row_accounting_json ->> 'accounted_row_count')::integer =
                            (row_accounting_json ->> 'extracted_row_count')::integer +
                            (row_accounting_json ->> 'blank_row_count')::integer +
                            (row_accounting_json ->> 'skipped_row_count')::integer
                        and jsonb_array_length(
                            row_accounting_json -> 'unaccounted_rows'
                        ) =
                            (row_accounting_json ->> 'detected_row_count')::integer -
                            (row_accounting_json ->> 'accounted_row_count')::integer
                    ) or (
                        row_accounting_json ?& array[
                            'schema_version', 'reader_version', 'reader_path',
                            'document_id', 'detected_segment_count',
                            'read_segment_count', 'proposed_fact_count',
                            'unread_segment_ids',
                            'proposed_subject_candidate_ids',
                            'unproposed_subject_candidate_ids'
                        ]
                        and row_accounting_json ->> 'schema_version' =
                            'prose-segment-accounting-v1'
                        and row_accounting_json ->> 'reader_path' =
                            'prose_interpretation'
                        and row_accounting_json ->> 'document_id' ~ '^[0-9]+$'
                        and (row_accounting_json ->> 'document_id')::bigint = document_id
                        and row_accounting_json ->> 'detected_segment_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'read_segment_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'proposed_fact_count' ~ '^[0-9]+$'
                        and jsonb_typeof(
                            row_accounting_json -> 'unread_segment_ids'
                        ) = 'array'
                        and jsonb_typeof(
                            row_accounting_json -> 'proposed_subject_candidate_ids'
                        ) = 'array'
                        and jsonb_typeof(
                            row_accounting_json -> 'unproposed_subject_candidate_ids'
                        ) = 'array'
                        and (row_accounting_json ->> 'read_segment_count')::integer +
                            jsonb_array_length(
                                row_accounting_json -> 'unread_segment_ids'
                            ) =
                            (row_accounting_json ->> 'detected_segment_count')::integer
                    )
                )
            )
            '''
NATIVE_ROW_ACCOUNTING_SHAPE = ('''
            case when prompt_version = 'matrix_structure_ids_v1' or row_accounting_json ->> 'schema_version' = 'native-matrix-row-accounting-v1'
            then (
            row_accounting_json is null or (
                jsonb_typeof(row_accounting_json) = 'object'
                and row_accounting_json ?& array[
                    'schema_version', 'reader_version', 'reader_path',
                    'detected_row_count', 'accounted_row_count',
                    'extracted_row_count', 'blank_row_count',
                    'skipped_row_count', 'unaccounted_rows', 'rows',
                    'native_mapping', 'field_materialization'
                ]
                and prompt_version = 'matrix_structure_ids_v1'
                and row_accounting_json ->> 'reader_version' = prompt_version
                and row_accounting_json ->> 'reader_path' = 'native_matrix_cells'
                and row_accounting_json ->> 'schema_version' = 'native-matrix-row-accounting-v1'
                and jsonb_typeof(row_accounting_json -> 'rows') = 'array'
                and jsonb_typeof(row_accounting_json -> 'unaccounted_rows') = 'array'
                and jsonb_typeof(row_accounting_json -> 'native_mapping') = 'object'
                and jsonb_typeof(row_accounting_json #> '{native_mapping,pages}') = 'array'
                and row_accounting_json #>> '{native_mapping,identity}' ~ '^[0-9a-f]{64}$'
                and row_accounting_json #>> '{native_mapping,reading_sha256}' ~ '^[0-9a-f]{64}$'
                and jsonb_typeof(row_accounting_json -> 'field_materialization') = 'array'
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.field_materialization[*] ? (
                        @.type() == "object" && @.row_id.type() == "string" && @.row_id != ""
                        && @.local_row_id.type() == "string" && @.local_row_id != ""
                        && @.page.type() == "number" && @.page > 0
                        && @.field.type() == "string" && @.field != ""
                        && (@.status == "materialized" || @.status == "refused" || @.status == "not_extracted")
                        && @.reason.type() == "string" && @.reason != ""
                        && @.value_source_ids.type() == "array" && @.value_source_ids.size() > 0
                        && @.context_source_ids.type() == "array"
                    )'
                )) = jsonb_array_length(row_accounting_json -> 'field_materialization')
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.native_mapping.pages[*].reading.rows[*].fields.keyvalue()'
                )) = jsonb_array_length(row_accounting_json -> 'field_materialization')
                and row_accounting_json ->> 'detected_row_count' ~ '^[0-9]+$'
                and row_accounting_json ->> 'accounted_row_count' ~ '^[0-9]+$'
                and row_accounting_json ->> 'extracted_row_count' ~ '^[0-9]+$'
                and row_accounting_json ->> 'blank_row_count' ~ '^[0-9]+$'
                and row_accounting_json ->> 'skipped_row_count' ~ '^[0-9]+$'
                and jsonb_array_length(row_accounting_json -> 'rows') =
                    (row_accounting_json ->> 'detected_row_count')::integer
                and (row_accounting_json ->> 'accounted_row_count')::integer =
                    (row_accounting_json ->> 'extracted_row_count')::integer +
                    (row_accounting_json ->> 'blank_row_count')::integer +
                    (row_accounting_json ->> 'skipped_row_count')::integer
                and jsonb_array_length(row_accounting_json -> 'unaccounted_rows') =
                    (row_accounting_json ->> 'detected_row_count')::integer -
                    (row_accounting_json ->> 'accounted_row_count')::integer
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.rows[*] ? (@.disposition == "extracted")'
                )) = (row_accounting_json ->> 'extracted_row_count')::integer
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.rows[*] ? (@.disposition == "blank")'
                )) = (row_accounting_json ->> 'blank_row_count')::integer
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.rows[*] ? (@.disposition == "skipped")'
                )) = (row_accounting_json ->> 'skipped_row_count')::integer
            ) is true
            ) else (''' + NATIVE_ROW_ACCOUNTING_SHAPE_BEFORE + ''') end
            ''')


# #736: reading-bound PDF locators; fold into the one unreleased transition.
NATIVE_SEGMENTS_SCHEMA = """alter table public.source_segments add column rendition_sha256 VARCHAR(64);
alter table public.source_segments add column reading_sha256 VARCHAR(64);
alter table public.source_segments add column reader_identity JSONB;
alter table public.source_segments add column location_json JSONB;
alter table public.source_segments add column span_stream VARCHAR(16);
alter table public.source_segments add column table_index INTEGER;
alter table public.source_segments add column cell_row INTEGER;
alter table public.source_segments add column cell_column INTEGER;
alter table public.source_segments add column row_span INTEGER;
alter table public.source_segments add column column_span INTEGER;
alter table public.source_segments add constraint ck_source_segments_native_complete check (kind not in ('pdf_span', 'pdf_cell') or (page_no is not null and ((kind = 'pdf_span' and span_stream is not null and start_offset is not null and end_offset is not null) or (kind = 'pdf_cell' and table_index is not null and cell_row is not null and cell_column is not null and row_span is not null and column_span is not null))));
alter table public.source_segments drop constraint ck_source_segments_kind;
alter table public.source_segments drop constraint ck_source_segments_locator;
alter table public.source_segments add constraint ck_source_segments_kind check (kind in ('spreadsheet_cell', 'prose_span', 'recorded_verbal_statement', 'pdf_span', 'pdf_cell'));
alter table public.source_segments add constraint ck_source_segments_locator check ((kind = 'spreadsheet_cell' and document_id is not null and recorded_verbal_origin_id is null and length(sheet_name) > 0 and cell_range ~ '^[A-Z]+[1-9][0-9]*$' and page_no is null and start_offset is null and end_offset is null) or (kind = 'prose_span' and document_id is not null and recorded_verbal_origin_id is null and sheet_name is null and cell_range is null and page_no > 0 and start_offset >= 0 and end_offset > start_offset) or (kind = 'pdf_span' and document_id is not null and recorded_verbal_origin_id is null and sheet_name is null and cell_range is null and page_no > 0 and start_offset >= 0 and end_offset > start_offset and span_stream in ('page', 'clipped') and table_index is null and cell_row is null and cell_column is null and row_span is null and column_span is null) or (kind = 'pdf_cell' and document_id is not null and recorded_verbal_origin_id is null and sheet_name is null and cell_range is null and page_no > 0 and start_offset is null and end_offset is null and span_stream is null and table_index >= 0 and cell_row >= 0 and cell_column >= 0 and row_span > 0 and column_span > 0) or (kind = 'recorded_verbal_statement' and document_id is null and recorded_verbal_origin_id is not null and sheet_name is null and cell_range is null and page_no is null and start_offset is null and end_offset is null));
alter table public.source_segments add constraint ck_source_segments_reading check ((kind in ('pdf_span', 'pdf_cell') and rendition_sha256 is not null and rendition_sha256 ~ '^[0-9a-f]{64}$' and reading_sha256 is not null and reading_sha256 ~ '^[0-9a-f]{64}$' and reader_identity is not null and jsonb_typeof(reader_identity) = 'object' and location_json is not null and jsonb_typeof(location_json) = 'object') or (kind not in ('pdf_span', 'pdf_cell') and rendition_sha256 is null and reading_sha256 is null and reader_identity is null and location_json is null and span_stream is null and table_index is null and cell_row is null and cell_column is null and row_span is null and column_span is null));
alter table public.source_segments drop constraint uq_source_segments_document_kind_ordinal;
CREATE UNIQUE INDEX uq_source_segments_document_kind_ordinal ON source_segments (document_id, kind, ordinal) WHERE reading_sha256 is null;
alter table public.source_segments drop constraint uq_source_segments_prose_locator;
CREATE UNIQUE INDEX uq_source_segments_prose_locator ON source_segments (document_id, kind, page_no, start_offset, end_offset) WHERE reading_sha256 is null;
alter table public.source_segments add constraint uq_source_segments_reading_ordinal unique (document_id, reading_sha256, kind, ordinal);
alter table public.source_segments add constraint uq_source_segments_native_span unique (document_id, reading_sha256, page_no, span_stream, start_offset, end_offset);
alter table public.source_segments add constraint uq_source_segments_pdf_cell unique (document_id, reading_sha256, page_no, table_index, cell_row, cell_column);
create or replace function public.enforce_prose_segment_non_overlap() returns trigger
language plpgsql as $$
begin
    if new.kind in ('prose_span', 'pdf_span') and exists (
        select 1 from source_segments existing
        where existing.document_id = new.document_id and existing.kind = new.kind
          and existing.reading_sha256 is not distinct from new.reading_sha256
          and existing.span_stream is not distinct from new.span_stream
          and existing.page_no = new.page_no
          and int4range(existing.start_offset, existing.end_offset, '[)')
              && int4range(new.start_offset, new.end_offset, '[)')
    ) then raise exception 'prose source segments cannot overlap'; end if;
    return new;
end; $$;
"""

NATIVE_SEGMENTS_SCHEMA_DOWN = """
alter table public.source_segments drop constraint ck_source_segments_native_complete;
alter table public.source_segments drop constraint ck_source_segments_reading;
alter table public.source_segments drop constraint ck_source_segments_kind;
alter table public.source_segments drop constraint ck_source_segments_locator;
alter table public.source_segments add constraint ck_source_segments_kind check (kind in ('spreadsheet_cell', 'prose_span', 'recorded_verbal_statement'));
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
alter table public.source_segments drop constraint uq_source_segments_reading_ordinal;
alter table public.source_segments drop constraint uq_source_segments_native_span;
alter table public.source_segments drop constraint uq_source_segments_pdf_cell;
drop index public.uq_source_segments_document_kind_ordinal;
drop index public.uq_source_segments_prose_locator;
alter table public.source_segments add constraint uq_source_segments_document_kind_ordinal unique(document_id,kind,ordinal);
alter table public.source_segments add constraint uq_source_segments_prose_locator unique(document_id,kind,page_no,start_offset,end_offset);
create or replace function public.enforce_prose_segment_non_overlap() returns trigger
language plpgsql as $$
begin
    if new.kind = 'prose_span' and exists (
        select 1 from source_segments existing where existing.document_id = new.document_id
        and existing.kind = 'prose_span' and existing.page_no = new.page_no
        and int4range(existing.start_offset, existing.end_offset, '[)')
            && int4range(new.start_offset, new.end_offset, '[)')
    ) then raise exception 'prose source segments cannot overlap'; end if;
    return new;
end; $$;
alter table public.source_segments drop column rendition_sha256;
alter table public.source_segments drop column reading_sha256;
alter table public.source_segments drop column reader_identity;
alter table public.source_segments drop column location_json;
alter table public.source_segments drop column span_stream;
alter table public.source_segments drop column table_index;
alter table public.source_segments drop column cell_row;
alter table public.source_segments drop column cell_column;
alter table public.source_segments drop column row_span;
alter table public.source_segments drop column column_span;
"""

APPEND_NATIVE_SOURCE_SEGMENTS = """
create or replace function public.append_source_segments(
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
            incoming source_segments%rowtype;
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
                incoming := jsonb_populate_record(null::source_segments, item);
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
                if segment_kind in ('pdf_span', 'pdf_cell') then
                    if incoming.rendition_sha256 is distinct from
                        (select sha256 from documents where id = p_document_id)
                        or incoming.reader_identity ->> 'scheme' is distinct from 'corridor.pdf-segments.v1'
                        or incoming.reader_identity #>> '{native_layer,engine}' is distinct from 'corridor-pdf-reader'
                        or jsonb_typeof(incoming.location_json -> 'glyphs') is distinct from 'array'
                    then
                        raise exception 'native source segment rendition or reader locator is invalid'
                            using errcode = '23514';
                    end if;
                    perform pg_advisory_xact_lock(hashtextextended(
                        'native-source:' || p_document_id::text || incoming.reading_sha256, 0));
                    if exists (select 1 from source_segments where document_id = p_document_id
                        and reading_sha256 = incoming.reading_sha256
                        and reader_identity is distinct from incoming.reader_identity) then
                        raise exception 'one native reading cannot carry different reader identities'
                            using errcode = '23514';
                    end if;
                    select * into existing from source_segments
                    where document_id = p_document_id and kind = segment_kind
                      and reading_sha256 = incoming.reading_sha256
                      and page_no = incoming.page_no
                      and (segment_kind = 'pdf_span' and span_stream = incoming.span_stream
                           and start_offset = incoming.start_offset and end_offset = incoming.end_offset
                           or segment_kind = 'pdf_cell' and table_index = incoming.table_index
                           and cell_row = incoming.cell_row and cell_column = incoming.cell_column);
                elsif segment_kind = 'spreadsheet_cell' then
                    select * into existing from source_segments
                    where document_id = p_document_id and kind = 'spreadsheet_cell'
                      and sheet_name = incoming.sheet_name and cell_range = incoming.cell_range;
                elsif segment_kind = 'prose_span' then
                    select * into existing from source_segments
                    where document_id = p_document_id and kind = 'prose_span'
                      and page_no = incoming.page_no and start_offset = incoming.start_offset
                      and end_offset = incoming.end_offset;
                elsif segment_kind = 'recorded_verbal_statement' then
                    select * into existing from source_segments
                    where kind = 'recorded_verbal_statement'
                      and recorded_verbal_origin_id = p_recorded_verbal_origin_id;
                else
                    raise exception 'unrecognized source segment kind' using errcode = '23514';
                end if;
                if found then
                    if row(existing.kind, existing.exact_text, existing.content_sha256, existing.ordinal, existing.sheet_name, existing.cell_range, existing.page_no, existing.start_offset, existing.end_offset, existing.rendition_sha256, existing.reading_sha256, existing.reader_identity, existing.location_json, existing.span_stream, existing.table_index, existing.cell_row, existing.cell_column, existing.row_span, existing.column_span)
                        is distinct from row(incoming.kind, incoming.exact_text, incoming.content_sha256, incoming.ordinal, incoming.sheet_name, incoming.cell_range, incoming.page_no, incoming.start_offset, incoming.end_offset, incoming.rendition_sha256, incoming.reading_sha256, incoming.reader_identity, incoming.location_json, incoming.span_stream, incoming.table_index, incoming.cell_row, incoming.cell_column, incoming.row_span, incoming.column_span) then
                        raise exception 'source segment locator is already bound to different content'
                            using errcode = '23514';
                    end if;
                    appended := appended || existing.id;
                    continue;
                end if;
                insert into source_segments (
                    project_id, document_id, recorded_verbal_origin_id, kind,
                    exact_text, content_sha256, ordinal, sheet_name, cell_range,
                    page_no, start_offset, end_offset,
                    rendition_sha256, reading_sha256, reader_identity, location_json, span_stream, table_index, cell_row, cell_column, row_span, column_span
                ) values (
                    p_project_id, p_document_id, p_recorded_verbal_origin_id,
                    segment_kind, exact, digest, (item ->> 'ordinal')::integer,
                    item ->> 'sheet_name', item ->> 'cell_range',
                    (item ->> 'page_no')::integer,
                    (item ->> 'start_offset')::integer,
                    (item ->> 'end_offset')::integer,
                    incoming.rendition_sha256, incoming.reading_sha256, incoming.reader_identity, incoming.location_json, incoming.span_stream, incoming.table_index, incoming.cell_row, incoming.cell_column, incoming.row_span, incoming.column_span
                ) returning id into segment_id;
                appended := appended || segment_id;
            end loop;
            return appended;
        end; $$;
"""


PIPELINE_TABLES = (
    "pipeline_qualification_policies", "pipeline_configurations", "pipeline_observations", "pipeline_comparisons",
    "pipeline_qualifications", "pipeline_acceptances", "pipeline_selections",
)

PIPELINE_SCHEMA = """
create table public.pipeline_qualification_policies (
    policy_sha256 varchar(64) primary key,
    scope_sha256 varchar(64) not null,
    policy_text text not null,
    actor text not null,
    created_at timestamptz not null default clock_timestamp(),
    check (policy_sha256 = encode(sha256(convert_to(policy_text, 'UTF8')), 'hex')),
    check (policy_text::jsonb ->> 'scope_sha256' = scope_sha256)
);
create table public.pipeline_configurations (
    configuration_sha256 varchar(64) primary key,
    configuration_text text not null,
    created_at timestamptz not null default now(),
    constraint ck_pipeline_configurations_digest check (
        configuration_sha256 = encode(sha256(convert_to(configuration_text, 'UTF8')), 'hex')
    )
);
create function public.validate_pipeline_receipt() returns trigger language plpgsql as $$
declare body jsonb; scope_text text;
begin
    if tg_op <> 'INSERT' then
        raise exception 'pipeline evidence and selections are append-only' using errcode='23514';
    end if;
    body := new.receipt_text::jsonb;
    if jsonb_typeof(body) is distinct from 'object'
       or new.receipt_sha256 <> encode(sha256(convert_to(new.receipt_text, 'UTF8')), 'hex')
       or (body ->> 'project_id')::bigint is distinct from new.project_id
       or body ->> 'configuration_sha256' is distinct from new.configuration_sha256
       or body ->> 'scope_sha256' is distinct from new.scope_sha256 then
        raise exception 'pipeline receipt identity or binding differs' using errcode='23514';
    end if;
    if tg_table_name in ('pipeline_observations', 'pipeline_qualifications', 'pipeline_acceptances', 'pipeline_selections') then
        scope_text := body ->> 'scope_text';
        if scope_text is null
           or encode(sha256(convert_to(scope_text, 'UTF8')), 'hex') is distinct from new.scope_sha256
           or jsonb_typeof(scope_text::jsonb) is distinct from 'object'
           or scope_text::jsonb is distinct from body -> 'scope' then
            raise exception 'pipeline parsed scope differs from its exact digest-bound bytes' using errcode='23514';
        end if;
    end if;
    if tg_table_name = 'pipeline_qualifications' then
        if body ->> 'status' is distinct from new.status
           or jsonb_typeof(body -> 'missing') is distinct from 'array'
           or jsonb_typeof(body -> 'failed') is distinct from 'array' then
            raise exception 'pipeline gate needs its status and typed missing/failed evidence arrays' using errcode='23514';
        end if;
        if exists (select 1 from jsonb_array_elements(body -> 'missing') member where jsonb_typeof(member) <> 'string')
           or exists (select 1 from jsonb_array_elements(body -> 'failed') member where jsonb_typeof(member) <> 'string')
           or (new.status = 'passed' and (body -> 'missing' is distinct from '[]'::jsonb
                                        or body -> 'failed' is distinct from '[]'::jsonb)) then
            raise exception 'passing pipeline gate cannot omit or retain unmet evidence' using errcode='23514';
        end if;
    elsif tg_table_name = 'pipeline_acceptances' then
        -- ADR-0095. An acceptance is a different claim from a gate, so it may
        -- not wear a gate's clothes: no status, no missing, no failed. What it
        -- must carry instead is the maintainer, his words, the evidence he
        -- read and the limits that evidence does not establish.
        if body ->> 'schema' is distinct from 'pipeline-acceptance-v1'
           or body ->> 'basis' is distinct from 'maintainer_acceptance'
           or body ?| array['status', 'missing', 'failed'] then
            raise exception 'a maintainer acceptance is never recorded as a qualification gate' using errcode='23514';
        end if;
        if body ->> 'implementation_revision' is distinct from new.implementation_revision
           or new.implementation_revision !~ '^[0-9a-f]{40}$'
           or body ->> 'actor' is distinct from new.actor
           or new.actor !~ '^[a-z][a-z0-9._-]{1,31}:[^[:space:]]+$'
           or lower(substring(new.actor from position(':' in new.actor) + 1)) in ('agent', 'demo', 'extractor', 'reviewer', 'system')
           or length(trim(coalesce(body ->> 'words', ''))) = 0
           or length(trim(coalesce(body ->> 'decision', ''))) = 0
           or coalesce(body ->> 'accepted_at', '') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
           or jsonb_typeof(body -> 'evidence') is distinct from 'array'
           or jsonb_array_length(body -> 'evidence') = 0
           or jsonb_typeof(body -> 'limits') is distinct from 'array'
           or jsonb_array_length(body -> 'limits') = 0
           or exists (select 1 from jsonb_array_elements(body -> 'limits') member
                      where jsonb_typeof(member) <> 'string' or length(trim(member #>> '{}')) = 0)
           or exists (select 1 from jsonb_array_elements(body -> 'evidence') member
                      where jsonb_typeof(member) <> 'object'
                         or length(trim(coalesce(member ->> 'name', ''))) = 0
                         or length(trim(coalesce(member ->> 'reference', ''))) = 0
                         or length(trim(coalesce(member ->> 'summary', ''))) = 0) then
            raise exception 'a maintainer acceptance needs its attributable principal, its own words, named reachable evidence and stated limits' using errcode='23514';
        end if;
    elsif tg_table_name = 'pipeline_comparisons' then
        if body ->> 'kind' is distinct from new.kind
           or jsonb_typeof(body -> 'passed') is distinct from 'boolean' then
            raise exception 'pipeline comparison kind differs from its receipt' using errcode='23514';
        end if;
    elsif tg_table_name = 'pipeline_observations' then
        if not exists (select 1 from public.documents d where d.id = new.document_id
                       and d.project_id = new.project_id and d.sha256 = body ->> 'source_sha256')
           or (new.extraction_run_id is not null and not exists (
               select 1 from public.extraction_runs r where r.id = new.extraction_run_id
                   and r.document_id = new.document_id)) then
            raise exception 'pipeline observation crosses its source/run project' using errcode='23514';
        end if;
    end if;
    return new;
end $$;
revoke all on function public.validate_pipeline_receipt() from public;
create trigger pipeline_configuration_immutable before update or delete
    on public.pipeline_configurations for each row
    execute function public.refuse_extractor_configuration_rewrite();
create trigger pipeline_policy_immutable before update or delete
    on public.pipeline_qualification_policies for each row
    execute function public.validate_pipeline_receipt();
"""

PIPELINE_SELECTION_GUARD = """
create function public.validate_pipeline_selection() returns trigger language plpgsql as $$
declare
    qualification public.pipeline_qualifications;
    acceptance public.pipeline_acceptances;
    previous bigint;
    body jsonb := new.receipt_text::jsonb;
    basis text := body ->> 'basis';
    basis_scope jsonb;
    basis_project bigint;
    basis_configuration text;
    basis_scope_sha256 text;
begin
    -- The project lock protects even direct maintenance INSERTs. The expected
    -- predecessor is a compare-and-swap, not a last-writer-wins update.
    perform 1 from public.projects where id = new.project_id for update;
    select id into previous from public.pipeline_selections
      where project_id = new.project_id and deployment = new.deployment
      order by id desc limit 1;
    if previous is distinct from new.previous_selection_id then
        raise exception 'pipeline selection predecessor changed' using errcode='40001';
    end if;
    -- ADR-0095: a passing gate or a recorded acceptance, never both and never
    -- neither, and the receipt says which so the two never read alike.
    if (new.qualification_id is null) = (new.acceptance_id is null) then
        raise exception 'a pipeline selection stands on exactly one basis' using errcode='23514';
    end if;
    if new.qualification_id is not null then
        select * into qualification from public.pipeline_qualifications where id = new.qualification_id;
        if qualification.id is null or qualification.status is distinct from 'passed'
           or qualification.receipt_text::jsonb ->> 'status' is distinct from 'passed'
           or qualification.receipt_text::jsonb -> 'missing' is distinct from '[]'::jsonb
           or qualification.receipt_text::jsonb -> 'failed' is distinct from '[]'::jsonb
           or basis is distinct from 'qualification'
           or (body ?& array['qualification_id', 'qualification_sha256']) is not true
           or body ->> 'qualification_sha256' is distinct from qualification.receipt_sha256
           or (body ->> 'qualification_id')::bigint is distinct from new.qualification_id
           or body ->> 'acceptance_id' is not null then
            raise exception 'pipeline selection needs its exact complete and passing gate' using errcode='23514';
        end if;
        basis_scope := qualification.receipt_text::jsonb -> 'scope';
        basis_project := qualification.project_id;
        basis_configuration := qualification.configuration_sha256;
        basis_scope_sha256 := qualification.scope_sha256;
    else
        select * into acceptance from public.pipeline_acceptances where id = new.acceptance_id;
        if acceptance.id is null
           or basis is distinct from 'maintainer_acceptance'
           or acceptance.receipt_text::jsonb ->> 'basis' is distinct from 'maintainer_acceptance'
           or (body ?& array['acceptance_id', 'acceptance_sha256']) is not true
           or body ->> 'acceptance_sha256' is distinct from acceptance.receipt_sha256
           or (body ->> 'acceptance_id')::bigint is distinct from new.acceptance_id
           or body ->> 'qualification_id' is not null then
            raise exception 'pipeline selection needs its exact recorded maintainer acceptance' using errcode='23514';
        end if;
        basis_scope := acceptance.receipt_text::jsonb -> 'scope';
        basis_project := acceptance.project_id;
        basis_configuration := acceptance.configuration_sha256;
        basis_scope_sha256 := acceptance.scope_sha256;
    end if;
    if basis_project is distinct from new.project_id
       or basis_configuration is distinct from new.configuration_sha256
       or basis_scope_sha256 is distinct from new.scope_sha256
       or basis_scope #>> '{deployment}' is distinct from new.deployment
       or body -> 'scope' is distinct from basis_scope
       or (body ?& array['previous_selection_id', 'actor', 'reason', 'enabled', 'basis']) is not true
       or new.actor !~ '^[a-z][a-z0-9._-]{1,31}:[^[:space:]]+$'
       or lower(substring(new.actor from position(':' in new.actor) + 1)) in ('agent', 'demo', 'extractor', 'reviewer', 'system')
       or length(trim(new.reason)) = 0
       or body ->> 'actor' is distinct from new.actor
       or body ->> 'reason' is distinct from new.reason
       or (body ->> 'enabled')::boolean is distinct from new.enabled
       or (body ->> 'previous_selection_id')::bigint is distinct from new.previous_selection_id then
        raise exception 'pipeline selection needs its exact qualified or accepted scope and human act' using errcode='23514';
    end if;
    return new;
end $$;
revoke all on function public.validate_pipeline_selection() from public;
create trigger pipeline_selection_scope before insert on public.pipeline_selections
    for each row execute function public.validate_pipeline_selection();
"""


def _create_pipeline_qualification_schema(op) -> None:
    """#447 adds routing authority for maintenance only, never record authority."""
    op.execute(PIPELINE_SCHEMA)
    additions = {
        "pipeline_observations": "document_id bigint not null references documents(id), extraction_run_id bigint references extraction_runs(id),",
        "pipeline_comparisons": "kind varchar(24) not null check (kind in ('repeatability', 'quality')),",
        "pipeline_qualifications": "status varchar(24) not null check (status in ('passed', 'failed', 'incomplete')),",
        "pipeline_acceptances": "implementation_revision varchar(40) not null, actor text not null,",
        "pipeline_selections": (
            "deployment text not null, qualification_id bigint references pipeline_qualifications(id), "
            "acceptance_id bigint references pipeline_acceptances(id), "
            "previous_selection_id bigint references pipeline_selections(id), "
            "actor text not null, reason text not null, enabled boolean not null, "
            "constraint ck_pipeline_selections_one_basis check "
            "((qualification_id is null) <> (acceptance_id is null)), "
            "constraint uq_pipeline_selections_successor unique nulls not distinct "
            "(project_id, deployment, previous_selection_id),"
        ),
    }
    for table, columns in additions.items():
        op.execute(f"""
            create table public.{table} (
                id bigserial primary key,
                project_id bigint not null references projects(id),
                configuration_sha256 varchar(64) not null references pipeline_configurations(configuration_sha256),
                scope_sha256 varchar(64) not null,
                receipt_sha256 varchar(64) not null unique,
                receipt_text text not null,
                {columns}
                created_at timestamptz not null default now()
            );
            create trigger {table}_immutable before insert or update or delete on public.{table}
                for each row execute function public.validate_pipeline_receipt();
            alter table public.{table} enable row level security;
            create policy p_{table}_project_partition on public.{table} to corridor_web
                using (project_id = any(public.current_project_partition()));
            create policy p_{table}_unpartitioned on public.{table} to corridor_worker using (true);
        """)
    op.execute(PIPELINE_SELECTION_GUARD)
    for table in PIPELINE_TABLES:
        op.execute(f"revoke all on public.{table} from {RUNTIME_LOGINS}")
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        if table not in ("pipeline_configurations", "pipeline_qualification_policies"):
            op.execute(f"revoke all on sequence public.{table}_id_seq from {RUNTIME_LOGINS}")
    # Ordinary capture may register its actual configuration and observation.
    # Comparisons, gates, acceptances and selection are maintenance tooling;
    # runtime logins have no write grant and no SECURITY DEFINER command that
    # manufactures one. An acceptance is the maintainer's act (ADR-0095), so no
    # worker, web request or other automated path can grant itself one here.
    for table in ("pipeline_configurations", "pipeline_observations"):
        op.execute(f"grant insert on public.{table} to {RUNTIME_LOGINS}")
    op.execute(f"grant usage on sequence public.pipeline_observations_id_seq to {RUNTIME_LOGINS}")


def upgrade(op) -> None:
    for name, expression in (
        ("ck_extraction_runs_row_accounting_shape", NATIVE_ROW_ACCOUNTING_SHAPE),
        ("ck_extraction_runs_completed_row_accounting", NATIVE_ROW_ACCOUNTING_COMPLETENESS),
    ):
        op.drop_constraint(name, "extraction_runs", type_="check")
        op.create_check_constraint(name, "extraction_runs", expression)
    op.drop_constraint("ck_facts_typed_value", "facts", type_="check")
    op.create_check_constraint("ck_facts_typed_value", "facts", FACTS_TYPED_VALUE_WITH_PDF)
    op.execute(NATIVE_SEGMENTS_SCHEMA)
    op.execute(APPEND_NATIVE_SOURCE_SEGMENTS)
    _create_pipeline_qualification_schema(op)


def downgrade(op) -> None:
    for table in PIPELINE_TABLES:
        if op.get_bind().scalar(sa.text(f"select exists (select 1 from public.{table})")):
            raise RuntimeError("permanent pipeline evidence cannot be represented by the supported predecessor")
    for table in reversed(PIPELINE_TABLES):
        op.execute(f"drop table public.{table} cascade")
    op.execute("drop function public.validate_pipeline_selection()")
    op.execute("drop function public.validate_pipeline_receipt()")
    if op.get_bind().scalar(sa.text(
        "select exists (select 1 from extraction_runs where "
        "row_accounting_json ->> 'schema_version' = 'native-matrix-row-accounting-v1')"
    )):
        raise RuntimeError("native matrix receipts cannot be represented by the supported predecessor")
    for name, expression in (
        ("ck_extraction_runs_row_accounting_shape", NATIVE_ROW_ACCOUNTING_SHAPE_BEFORE),
        ("ck_extraction_runs_completed_row_accounting", NATIVE_ROW_ACCOUNTING_COMPLETENESS_BEFORE),
    ):
        op.drop_constraint(name, "extraction_runs", type_="check")
        op.create_check_constraint(name, "extraction_runs", expression)

    # Never remove a recorded reading or its locator, even on downgrade.
    if op.get_bind().scalar(sa.text("select exists (select 1 from source_segments where reading_sha256 is not null)")):
        raise RuntimeError("native PDF source segments cannot be represented by the supported predecessor")
    if op.get_bind().scalar(sa.text(
        "select exists (select 1 from facts where transformation in "
        "('collapse_pdf_whitespace_v1', 'pdf_marked_resolution_v1'))"
    )):
        raise RuntimeError("PDF Fact transformations cannot be represented by the supported predecessor")
    op.drop_constraint("ck_facts_typed_value", "facts", type_="check")
    op.create_check_constraint("ck_facts_typed_value", "facts", FACTS_TYPED_VALUE_BEFORE_PDF)
    op.execute(NATIVE_SEGMENTS_SCHEMA_DOWN)
    op.execute(APPEND_SOURCE_SEGMENTS_OVER_ORIGIN.replace("create function", "create or replace function", 1))
