"""Frozen #455 additions folded into the single supported transition.

MIME has its own locator; it never impersonates a retired PDF page. A later
thread reading may supersede an unaccepted delta even when its result is a
question or agrees with the record. The supersession still lives in the one
delta lifecycle and names its replacement source evidence in that case.
"""

import sqlalchemy as sa


READING_COLUMNS = """
alter table inbound_thread_readings add column project_id bigint references projects(id);
update inbound_thread_readings r set project_id = t.project_id from inbound_threads t where t.id = r.thread_id;
alter table inbound_thread_readings alter column project_id set not null;
alter table inbound_thread_readings add constraint uq_inbound_thread_readings_project_id unique (project_id, id);
alter table inbound_thread_readings add column source_fact_id bigint references facts(id);
alter table inbound_thread_readings add column proposed_delta_id bigint references proposed_deltas(id);
alter table inbound_thread_readings add column input_sha256 varchar(64);
alter table inbound_thread_readings add column question_segment_id bigint references source_segments(id);
alter table delta_supersessions alter column superseding_delta_id drop not null;
alter table delta_supersessions add column source_reading_id bigint;
alter table delta_supersessions add constraint fk_delta_supersessions_reading_scope
    foreign key(project_id, source_reading_id) references inbound_thread_readings(project_id, id);
alter table delta_supersessions add constraint ck_delta_supersessions_successor
    check (superseding_delta_id is not null or source_reading_id is not null);
alter table source_segments add constraint ck_source_segments_email_complete check (
    kind <> 'email_span' or (start_offset is not null and end_offset is not null
    and location_json is not null and coalesce(location_json ->> 'scheme' = 'email-mime-v1', false)
    and coalesce(location_json ->> 'section' in
       ('header', 'body', 'quoted_history', 'signature', 'draft', 'html', 'attachment'), false)
    and coalesce(jsonb_typeof(location_json -> 'part_path') = 'array', false)));
"""

EMAIL_SEGMENT_BRANCH = """
                elsif segment_kind = 'email_span' then
                    if incoming.location_json ->> 'source_digest' is distinct from
                        (select sha256 from documents where id = p_document_id)
                        or incoming.location_json ->> 'scheme' is distinct from 'email-mime-v1'
                        or (incoming.location_json ->> 'section' in
                            ('header', 'body', 'quoted_history', 'signature', 'draft', 'html', 'attachment')) is not true
                        or jsonb_typeof(incoming.location_json -> 'part_path') is distinct from 'array'
                    then
                        raise exception 'email source locator is invalid' using errcode = '23514';
                    end if;
                    perform pg_advisory_xact_lock(hashtextextended('email-source:' || p_document_id::text, 0));
                    select * into existing from source_segments
                    where document_id = p_document_id and kind = 'email_span' and ordinal = incoming.ordinal;
"""

EMAIL_FACT_VALIDATION = """
            if p_fact_type in ('email_header', 'email_attachment') or exists (
                select 1 from jsonb_array_elements(p_sources) src join source_segments s
                on s.id = (src ->> 'source_segment_id')::bigint where s.kind = 'email_span'
            ) then
                if p_fact_type not in ('email_header', 'email_attachment', 'statement_wording')
                    or jsonb_array_length(p_sources) <> (case when p_fact_type = 'statement_wording' then 2 else 1 end)
                    or (select count(*) from jsonb_array_elements(p_sources) src
                        where src ->> 'role' = 'value_source') <> 1
                    or not exists (
                        select 1 from jsonb_array_elements(p_sources) src join source_segments s
                        on s.id = (src ->> 'source_segment_id')::bigint
                        where src ->> 'role' = 'value_source' and s.kind = 'email_span'
                        and s.project_id = p_project_id and s.document_id = p_document_id
                        and s.exact_text = p_text_value and s.location_json ->> 'section' =
                            case p_fact_type when 'email_header' then 'header'
                                when 'email_attachment' then 'attachment' else 'body' end
                    ) or (p_fact_type = 'statement_wording' and not exists (
                        select 1 from jsonb_array_elements(p_sources) src join source_segments s
                        on s.id = (src ->> 'source_segment_id')::bigint
                        where src ->> 'role' = 'attribution_source' and s.kind = 'email_span'
                        and s.project_id = p_project_id and s.document_id = p_document_id
                        and s.location_json ->> 'section' = 'header'
                        and s.location_json ->> 'header_name' = 'from'
                        and s.location_json -> 'part_path' = '[]'::jsonb
                    )) then
                    raise exception 'email Fact must replay its typed source roles' using errcode = '23514';
                end if;
            end if;
"""

READING_COMMAND = """
create function append_email_thread_reading(
    p_project bigint, p_thread bigint, p_closing bigint, p_input text,
    p_fact bigint, p_delta bigint, p_question bigint, p_context jsonb,
    p_prompt text, p_model text
) returns bigint language plpgsql security definer set search_path to 'public' as $$
declare
    closing inbound_messages%rowtype;
    existing inbound_thread_readings%rowtype;
    reading_id bigint;
    wording text;
begin
    perform 1 from inbound_threads where id = p_thread and project_id = p_project for update;
    if not found then
        raise exception 'email thread is outside project' using errcode = '23514';
    end if;
    select * into closing from inbound_messages where thread_id = p_thread order by id desc limit 1;
    if closing.id is distinct from p_closing or closing.project_id is distinct from p_project then
        raise exception 'email reading no longer names the closing turn' using errcode = '23514';
    end if;
    select * into existing from inbound_thread_readings where thread_id = p_thread and closing_message_id = p_closing;
    if found then
        if existing.input_sha256 is distinct from p_input then
            raise exception 'email reading replay has different inputs' using errcode = '23514';
        end if;
        return existing.id;
    end if;
    if p_input is null or p_input !~ '^[0-9a-f]{64}$' or (p_fact is null) = (p_question is null) then
        raise exception 'email reading needs one statement or question' using errcode = '23514';
    end if;
    if p_fact is not null and not exists (
        select 1 from facts where id = p_fact and project_id = p_project
          and document_id = closing.document_id and fact_type = 'statement_wording'
          and subject_key = 'email-thread:' || p_thread::text
    ) then
        raise exception 'email outcome Fact is outside closing turn' using errcode = '23514';
    end if;
    if p_delta is not null and (p_fact is null or not exists (
        select 1 from proposed_deltas d join facts f on f.id = p_fact
        join delta_groups g on g.id = d.group_id
        where d.id = p_delta and d.project_id = p_project and g.document_id = closing.document_id
          and d.source_family = 'email-thread:' || p_thread::text
          and d.target_subject_identity = f.subject_key
          and (d.proposed_value = to_jsonb(f.text_value)
            or d.proposed_value -> 'statement_wording' = to_jsonb(f.text_value))
    )) then
        raise exception 'email delta is outside its outcome Fact' using errcode = '23514';
    end if;
    if p_question is not null then
        select exact_text into wording from source_segments
        where id = p_question and document_id = closing.document_id and project_id = p_project and kind = 'email_span';
        if not found then
            raise exception 'email question has no closing-turn source' using errcode = '23514';
        end if;
    end if;
    if p_context is null or jsonb_typeof(p_context) <> 'array' or exists (
        select 1 from jsonb_array_elements(p_context) c where not exists (
            select 1 from inbound_messages m where m.id = (c ->> 'message_id')::bigint
            and m.thread_id = p_thread and m.project_id = p_project
            and m.raw_sha256 = c ->> 'raw_sha256'
            and exists (select 1 from extraction_runs r where r.id = (c ->> 'extraction_run_id')::bigint
                        and r.document_id = m.document_id)
        )
    ) then
        raise exception 'email context crosses its thread' using errcode = '23514';
    end if;
    insert into inbound_thread_readings (project_id, thread_id, closing_message_id, resolution,
        source_fact_id, proposed_delta_id, input_sha256, question_segment_id,
        open_question, turn_context_json, prompt_version, model)
    values (p_project, p_thread, p_closing, case when p_fact is null then 'unresolved' else 'concluded' end,
        p_fact, p_delta, p_input, p_question, wording, p_context, p_prompt, p_model)
    returning id into reading_id;
    insert into delta_supersessions(project_id, prior_delta_id, superseding_delta_id, source_reading_id, reason)
    select p_project, d.id, p_delta, reading_id, 'newer_email_thread_reading'
    from proposed_deltas d where d.project_id = p_project
      and d.source_family = 'email-thread:' || p_thread::text
      and d.id is distinct from p_delta
      -- A decision the coordinator undid settles nothing, so the delta it
      -- was about is an open question a newer reading supersedes like any
      -- other (#948, ADR-0035).
      and public.proposed_delta_effective_disposition(d.id) is null
      and not exists (select 1 from delta_supersessions where prior_delta_id = d.id)
      -- A delta retired because its capture was corrected is not superseded by
      -- a newer reading either; it already left the actionable set with its own
      -- explanation, and this sweep passes over it exactly as it passes over a
      -- decided one (ADR-0101).
      and public.proposed_delta_capture_correction(d.id) is null;
    return reading_id;
end; $$;
alter function append_email_thread_reading(bigint,bigint,bigint,text,bigint,bigint,bigint,jsonb,text,text)
    owner to corridor_source_append;
revoke all on function append_email_thread_reading(bigint,bigint,bigint,text,bigint,bigint,bigint,jsonb,text,text) from public;
grant execute on function append_email_thread_reading(bigint,bigint,bigint,text,bigint,bigint,bigint,jsonb,text,text)
    to corridor_web, corridor_worker;
grant select, insert on inbound_thread_readings to corridor_source_append;
grant usage, select on sequence inbound_thread_readings_id_seq to corridor_source_append;
grant select, update on inbound_threads to corridor_source_append;
grant select on inbound_messages to corridor_source_append;
create function preserve_email_thread_reading() returns trigger language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        if new.input_sha256 is not null and current_user <> 'corridor_source_append' then
            raise exception 'email source reading requires its append command' using errcode = '23514';
        end if;
        return new;
    end if;
    if old.input_sha256 is not null then
        raise exception 'email source reading is append-only' using errcode = '23514';
    end if;
    if tg_op = 'UPDATE' and new.input_sha256 is not null then
        raise exception 'legacy reading cannot be converted into a source reading' using errcode = '23514';
    end if;
    return case when tg_op = 'DELETE' then old else new end;
end; $$;
create trigger trg_email_thread_reading_append_only before insert or update or delete on inbound_thread_readings
    for each row execute function preserve_email_thread_reading();
revoke all on function preserve_email_thread_reading() from public;
"""


def upgrade(op, segment_command, fact_command):
    op.execute(READING_COLUMNS)
    for table, name, expression in CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)
    needle = "                elsif segment_kind = 'spreadsheet_cell' then"
    assert segment_command.count(needle) == 1
    op.execute(segment_command.replace(needle, EMAIL_SEGMENT_BRANCH + needle))
    needle = "            -- Replay: the digest covers every value, reference, and source"
    assert fact_command.count(needle) == 1
    op.execute(fact_command.replace("create function", "create or replace function", 1).replace(
        needle, EMAIL_FACT_VALIDATION + needle))
    op.execute(READING_COMMAND)


def downgrade(op):
    if op.get_bind().scalar(sa.text("select exists (select 1 from source_segments where kind = 'email_span')")):
        raise RuntimeError("email evidence cannot be represented by the supported predecessor")
    op.execute("drop function append_email_thread_reading(bigint,bigint,bigint,text,bigint,bigint,bigint,jsonb,text,text)")
    op.execute("drop trigger trg_email_thread_reading_append_only on inbound_thread_readings")
    op.execute("drop function preserve_email_thread_reading()")
    op.execute("alter table source_segments drop constraint ck_source_segments_email_complete")
    for table, name, expression in CHECKS_BEFORE:
        if table == "facts":
            op.drop_constraint(name, table, type_="check")
            op.create_check_constraint(name, table, expression)
    op.execute("alter table delta_supersessions drop constraint ck_delta_supersessions_successor")
    op.execute("alter table delta_supersessions drop column source_reading_id")
    op.execute("alter table delta_supersessions alter column superseding_delta_id set not null")
    op.execute("alter table inbound_thread_readings drop constraint ck_inbound_thread_reading_claim")
    for column in ("source_fact_id", "proposed_delta_id", "input_sha256", "question_segment_id"):
        op.execute(f"alter table inbound_thread_readings drop column {column}")
    op.execute("alter table inbound_thread_readings drop column project_id")
    op.create_check_constraint("ck_inbound_thread_reading_claim", "inbound_thread_readings",
                               "(resolution = 'concluded') = (candidate_id is not null)")


# Expressions frozen when #455 was written, not imported from live ORM models.
CHECKS = [('facts',
  'ck_facts_subject',
  "(fact_type in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', "
  "'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', "
  "'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', "
  "'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', "
  "'offset_side', 'potential_conflict', 'data_source', 'marked_resolution', 'committed_date', "
  "'action_due_date', 'need_date') and subject_kind = 'source_row' and length(trim(subject_key)) > 0) or "
  "(fact_type in ('applies_to', 'closure_result') and subject_kind = 'source_row' and "
  "length(trim(subject_key)) > 0) or (fact_type in ('statement_wording', 'statement_timing', 'applies_to') "
  "and subject_kind = 'statement_candidate' and length(trim(subject_key)) > 0) or (fact_type = "
  "'supporting_documentation_in_use' and subject_kind = 'record_subject' and length(trim(subject_key)) > 0) "
  "or (fact_type in ('email_header', 'email_attachment') and subject_kind = 'email_message' and "
  'length(trim(subject_key)) > 0)'),
 ('facts',
  'ck_facts_type',
  "fact_type in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', "
  "'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', "
  "'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', "
  "'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', "
  "'offset_side', 'potential_conflict', 'data_source', 'marked_resolution', 'committed_date', "
  "'action_due_date', 'need_date', 'applies_to', 'closure_result', 'statement_wording', 'statement_timing', "
  "'supporting_documentation_in_use', 'email_header', 'email_attachment')"),
 ('facts',
  'ck_facts_typed_value',
  "(fact_type in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', "
  "'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', "
  "'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', "
  "'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', "
  "'offset_side', 'potential_conflict', 'data_source', 'marked_resolution') and text_value is not null and "
  'length(trim(text_value)) > 0 and date_value is null and date_range_start is null and date_range_end is '
  "null and (fact_type = 'external_org' or external_org_value_id is null) and document_value_id is null and "
  "(transformation in ('trim_cell_text_v1', 'collapse_pdf_whitespace_v1') or (fact_type = "
  "'resolution_strategy' and transformation = 'pdf_marked_resolution_v1'))) or (fact_type in "
  "('committed_date', 'action_due_date', 'need_date') and text_value is null and date_value is not null and "
  'date_range_start is null and date_range_end is null and external_org_value_id is null and '
  "document_value_id is null and transformation = 'iso_date_cell_v1') or (fact_type = 'applies_to' and "
  'text_value is null and date_value is null and date_range_start is null and date_range_end is null and '
  'external_org_value_id is null and document_value_id is null and transformation = '
  "'structured_reference_set_v1') or (fact_type = 'closure_result' and text_value is null and date_value is "
  'null and date_range_start is null and date_range_end is null and external_org_value_id is null and '
  "document_value_id is null and transformation = 'typed_closure_result_v1') or (fact_type = "
  "'statement_wording' and text_value is not null and length(trim(text_value)) > 0 and date_value is null "
  'and date_range_start is null and date_range_end is null and external_org_value_id is null and '
  "document_value_id is null and transformation = 'exact_prose_span_v1') or (fact_type = 'statement_timing' "
  'and text_value is null and date_value is null and date_range_start is null and date_range_end is null and '
  'external_org_value_id is null and document_value_id is null and transformation = '
  "'typed_statement_timing_v1') or (fact_type = 'supporting_documentation_in_use' and text_value is null and "
  'date_value is null and date_range_start is null and date_range_end is null and external_org_value_id is '
  "null and document_value_id is not null and transformation = 'supporting_document_revision_v1') or "
  "(fact_type in ('email_header', 'email_attachment') and text_value is not null and length(text_value) > 0 "
  'and date_value is null and date_range_start is null and date_range_end is null and external_org_value_id '
  "is null and document_value_id is null and transformation = 'exact_email_part_v1')"),
 ('inbound_thread_readings',
  'ck_inbound_thread_reading_claim',
  "(resolution = 'concluded') = (candidate_id is not null or source_fact_id is not null)"),
 ('source_segments',
  'ck_source_segments_kind',
  "kind in ('spreadsheet_cell', 'prose_span', 'recorded_verbal_statement', 'pdf_span', 'pdf_cell', "
  "'email_span')"),
 ('source_segments',
  'ck_source_segments_locator',
  "(kind = 'spreadsheet_cell' and document_id is not null and recorded_verbal_origin_id is null and "
  "length(sheet_name) > 0 and cell_range ~ '^[A-Z]+[1-9][0-9]*$' and page_no is null and start_offset is "
  "null and end_offset is null) or (kind = 'prose_span' and document_id is not null and "
  'recorded_verbal_origin_id is null and sheet_name is null and cell_range is null and page_no > 0 and '
  "start_offset >= 0 and end_offset > start_offset) or (kind = 'pdf_span' and document_id is not null and "
  'recorded_verbal_origin_id is null and sheet_name is null and cell_range is null and page_no > 0 and '
  "start_offset >= 0 and end_offset > start_offset and span_stream in ('page', 'clipped') and table_index is "
  'null and cell_row is null and cell_column is null and row_span is null and column_span is null) or (kind '
  "= 'pdf_cell' and document_id is not null and recorded_verbal_origin_id is null and sheet_name is null and "
  'cell_range is null and page_no > 0 and start_offset is null and end_offset is null and span_stream is '
  'null and table_index >= 0 and cell_row >= 0 and cell_column >= 0 and row_span > 0 and column_span > 0) or '
  "(kind = 'recorded_verbal_statement' and document_id is null and recorded_verbal_origin_id is not null and "
  'sheet_name is null and cell_range is null and page_no is null and start_offset is null and end_offset is '
  "null) or (kind = 'email_span' and document_id is not null and recorded_verbal_origin_id is null and "
  'sheet_name is null and cell_range is null and page_no is null and start_offset >= 0 and end_offset > '
  'start_offset)'),
 ('source_segments',
  'ck_source_segments_reading',
  "(kind in ('pdf_span', 'pdf_cell') and rendition_sha256 is not null and rendition_sha256 ~ "
  "'^[0-9a-f]{64}$' and reading_sha256 is not null and reading_sha256 ~ '^[0-9a-f]{64}$' and reader_identity "
  "is not null and jsonb_typeof(reader_identity) = 'object' and location_json is not null and "
  "jsonb_typeof(location_json) = 'object') or (kind not in ('pdf_span', 'pdf_cell') and rendition_sha256 is "
  'null and reading_sha256 is null and reader_identity is null and location_json is null and span_stream is '
  'null and table_index is null and cell_row is null and cell_column is null and row_span is null and '
  "column_span is null) or (kind = 'email_span' and rendition_sha256 is null and reading_sha256 is null and "
  "reader_identity is null and location_json is not null and location_json ->> 'scheme' = 'email-mime-v1' "
  'and span_stream is null and table_index is null and cell_row is null and cell_column is null and row_span '
  'is null and column_span is null)')]

CHECKS_BEFORE = [('facts',
  'ck_facts_subject',
  "(fact_type in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', "
  "'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', "
  "'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', "
  "'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', "
  "'offset_side', 'potential_conflict', 'data_source', 'marked_resolution', 'committed_date', "
  "'action_due_date', 'need_date') and subject_kind = 'source_row' and length(trim(subject_key)) > 0) or "
  "(fact_type in ('applies_to', 'closure_result') and subject_kind = 'source_row' and "
  "length(trim(subject_key)) > 0) or (fact_type in ('statement_wording', 'statement_timing', 'applies_to') "
  "and subject_kind = 'statement_candidate' and length(trim(subject_key)) > 0) or (fact_type = "
  "'supporting_documentation_in_use' and subject_kind = 'record_subject' and length(trim(subject_key)) > 0)"),
 ('facts',
  'ck_facts_type',
  "fact_type in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', "
  "'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', "
  "'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', "
  "'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', "
  "'offset_side', 'potential_conflict', 'data_source', 'marked_resolution', 'committed_date', "
  "'action_due_date', 'need_date', 'applies_to', 'closure_result', 'statement_wording', 'statement_timing', "
  "'supporting_documentation_in_use')"),
 ('facts',
  'ck_facts_typed_value',
  "(fact_type in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', "
  "'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', "
  "'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', "
  "'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', "
  "'offset_side', 'potential_conflict', 'data_source', 'marked_resolution') and text_value is not null and "
  'length(trim(text_value)) > 0 and date_value is null and date_range_start is null and date_range_end is '
  "null and (fact_type = 'external_org' or external_org_value_id is null) and document_value_id is null and "
  "(transformation in ('trim_cell_text_v1', 'collapse_pdf_whitespace_v1') or (fact_type = "
  "'resolution_strategy' and transformation = 'pdf_marked_resolution_v1'))) or (fact_type in "
  "('committed_date', 'action_due_date', 'need_date') and text_value is null and date_value is not null and "
  'date_range_start is null and date_range_end is null and external_org_value_id is null and '
  "document_value_id is null and transformation = 'iso_date_cell_v1') or (fact_type = 'applies_to' and "
  'text_value is null and date_value is null and date_range_start is null and date_range_end is null and '
  'external_org_value_id is null and document_value_id is null and transformation = '
  "'structured_reference_set_v1') or (fact_type = 'closure_result' and text_value is null and date_value is "
  'null and date_range_start is null and date_range_end is null and external_org_value_id is null and '
  "document_value_id is null and transformation = 'typed_closure_result_v1') or (fact_type = "
  "'statement_wording' and text_value is not null and length(trim(text_value)) > 0 and date_value is null "
  'and date_range_start is null and date_range_end is null and external_org_value_id is null and '
  "document_value_id is null and transformation = 'exact_prose_span_v1') or (fact_type = 'statement_timing' "
  'and text_value is null and date_value is null and date_range_start is null and date_range_end is null and '
  'external_org_value_id is null and document_value_id is null and transformation = '
  "'typed_statement_timing_v1') or (fact_type = 'supporting_documentation_in_use' and text_value is null and "
  'date_value is null and date_range_start is null and date_range_end is null and external_org_value_id is '
  "null and document_value_id is not null and transformation = 'supporting_document_revision_v1')"),
 ('inbound_thread_readings',
  'ck_inbound_thread_reading_claim',
  "(resolution = 'concluded') = (candidate_id is not null)"),
 ('source_segments',
  'ck_source_segments_kind',
  "kind in ('spreadsheet_cell', 'prose_span', 'recorded_verbal_statement', 'pdf_span', 'pdf_cell')"),
 ('source_segments',
  'ck_source_segments_locator',
  "(kind = 'spreadsheet_cell' and document_id is not null and recorded_verbal_origin_id is null and "
  "length(sheet_name) > 0 and cell_range ~ '^[A-Z]+[1-9][0-9]*$' and page_no is null and start_offset is "
  "null and end_offset is null) or (kind = 'prose_span' and document_id is not null and "
  'recorded_verbal_origin_id is null and sheet_name is null and cell_range is null and page_no > 0 and '
  "start_offset >= 0 and end_offset > start_offset) or (kind = 'pdf_span' and document_id is not null and "
  'recorded_verbal_origin_id is null and sheet_name is null and cell_range is null and page_no > 0 and '
  "start_offset >= 0 and end_offset > start_offset and span_stream in ('page', 'clipped') and table_index is "
  'null and cell_row is null and cell_column is null and row_span is null and column_span is null) or (kind '
  "= 'pdf_cell' and document_id is not null and recorded_verbal_origin_id is null and sheet_name is null and "
  'cell_range is null and page_no > 0 and start_offset is null and end_offset is null and span_stream is '
  'null and table_index >= 0 and cell_row >= 0 and cell_column >= 0 and row_span > 0 and column_span > 0) or '
  "(kind = 'recorded_verbal_statement' and document_id is null and recorded_verbal_origin_id is not null and "
  'sheet_name is null and cell_range is null and page_no is null and start_offset is null and end_offset is '
  'null)'),
 ('source_segments',
  'ck_source_segments_reading',
  "(kind in ('pdf_span', 'pdf_cell') and rendition_sha256 is not null and rendition_sha256 ~ "
  "'^[0-9a-f]{64}$' and reading_sha256 is not null and reading_sha256 ~ '^[0-9a-f]{64}$' and reader_identity "
  "is not null and jsonb_typeof(reader_identity) = 'object' and location_json is not null and "
  "jsonb_typeof(location_json) = 'object') or (kind not in ('pdf_span', 'pdf_cell') and rendition_sha256 is "
  'null and reading_sha256 is null and reader_identity is null and location_json is null and span_stream is '
  'null and table_index is null and cell_row is null and cell_column is null and row_span is null and '
  'column_span is null)')]
