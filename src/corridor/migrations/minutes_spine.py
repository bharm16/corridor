"""Frozen five-field minutes extensions to the one supported transition (#456)."""

import sqlalchemy as sa


SCHEMA = """
alter table fact_applies_to alter column dependency_id drop not null;
alter table fact_applies_to add column record_subject_key text;
alter table fact_applies_to add column source_segment_id bigint references source_segments(id);
alter table fact_applies_to add column reference_text text;
alter table fact_applies_to add constraint uq_fact_applies_to_record_subject unique(fact_id,record_subject_key);
alter table fact_applies_to add constraint ck_fact_applies_to_target check(num_nonnulls(dependency_id,record_subject_key)=1);
create table minutes_captures (
    id bigserial primary key, project_id bigint not null references projects(id),
    document_id bigint not null references documents(id), extraction_run_id bigint not null references extraction_runs(id),
    source_family text not null, source_revision text not null, input_sha256 varchar(64) not null,
    accepted_revision_id bigint not null references project_record_revisions(id), output_json jsonb not null,
    recorded_at timestamptz not null default clock_timestamp(),
    constraint uq_minutes_capture_scope unique(project_id,id),
    constraint uq_minutes_capture_revision unique(project_id,source_family,source_revision)
);
alter table delta_supersessions add column minutes_capture_id bigint;
alter table delta_supersessions add constraint fk_delta_supersession_minutes
    foreign key(project_id,minutes_capture_id) references minutes_captures(project_id,id);
alter table delta_supersessions drop constraint ck_delta_supersessions_successor;
alter table delta_supersessions add constraint ck_delta_supersessions_successor
    check(superseding_delta_id is not null or source_reading_id is not null or minutes_capture_id is not null);
create function guard_minutes_capture() returns trigger language plpgsql as $$
begin
    if tg_op<>'INSERT' or current_user<>'corridor_source_append' then
        raise exception 'minutes captures require the immutable source append command' using errcode='23514';
    end if;
    return new;
end; $$;
create trigger minutes_capture_guard before insert or update or delete on minutes_captures
    for each row execute function guard_minutes_capture();
revoke all on function guard_minutes_capture() from public;
"""

SCOPE_APPEND = """
            if p_satellites ? 'applies_to_subjects' then
                slot := 0;
                for timing in select value from jsonb_array_elements(p_satellites -> 'applies_to_subjects') loop
                    slot := slot + 1;
                    if not exists(select 1 from current_project_record r
                        where r.project_id=p_project_id and r.subject_key=timing ->> 'subject_key'
                          and r.fact_type='utility_id' and lower(r.text_value)=lower(timing ->> 'reference_text'))
                      or not exists(select 1 from source_segments s
                        where s.id=(timing ->> 'source_segment_id')::bigint and s.project_id=p_project_id
                          and s.document_id=p_document_id and position(timing ->> 'reference_text' in s.exact_text)>0)
                      or not exists(select 1 from jsonb_array_elements(p_sources) src
                        where src ->> 'source_segment_id'=timing ->> 'source_segment_id') then
                        raise exception 'minutes scope lacks an exact project/source reference' using errcode='23514';
                    end if;
                    insert into fact_applies_to(project_id,fact_id,ordinal,record_subject_key,source_segment_id,reference_text)
                    values(p_project_id,new_id,slot,timing ->> 'subject_key',(timing ->> 'source_segment_id')::bigint,
                           timing ->> 'reference_text');
                end loop;
            end if;
"""

APPEND = """
create function append_minutes_capture(p_project bigint,p_document bigint,p_run bigint,
    p_family text,p_revision text,p_digest text,p_accepted bigint,p_output jsonb)
returns bigint language plpgsql security definer set search_path to 'public' as $$
declare existing minutes_captures%rowtype; result bigint; item jsonb; identifier text;
    family_hash text; current_delta_ids bigint[]:='{}'; replacement bigint; prior record;
begin
    perform 1 from projects where id=p_project for update;
    if not found or not exists(select 1 from documents where id=p_document and project_id=p_project)
        or not exists(select 1 from extraction_runs where id=p_run and document_id=p_document)
        or not exists(select 1 from project_baseline_adoptions where project_id=p_project) then
        raise exception 'minutes input is outside its adopted project' using errcode='23514';
    end if;
    select * into existing from minutes_captures where project_id=p_project and source_family=p_family and source_revision=p_revision;
    if found then
        if existing.input_sha256<>p_digest then
            raise exception 'minutes revision replay has different inputs' using errcode='23514';
        end if;
        return existing.id;
    end if;
    if (select max(id) from project_record_revisions where project_id=p_project) is distinct from p_accepted then
        raise exception 'accepted revision moved during minutes capture' using errcode='23514';
    end if;
    if coalesce(p_family,'')='' or coalesce(p_revision,'')='' or p_digest !~ '^[0-9a-f]{64}$'
        or jsonb_typeof(p_output -> 'outcomes') is distinct from 'array' then
        raise exception 'minutes capture requires bounded identity and accounting' using errcode='23514';
    end if;
    family_hash:=encode(sha256(convert_to('minutes:' || p_family,'UTF8')),'hex');
    for item in select value from jsonb_array_elements(p_output -> 'outcomes') loop
        for identifier in select value from jsonb_array_elements_text(item -> 'fact_ids') loop
            if not exists(select 1 from facts where id=identifier::bigint and project_id=p_project
                and document_id=p_document and extraction_run_id=p_run) then
                raise exception 'minutes Fact is outside its source run' using errcode='23514';
            end if;
        end loop;
        for identifier in select value from jsonb_array_elements_text(item -> 'delta_ids') loop
            if not exists(select 1 from proposed_deltas d join delta_groups g on g.id=d.group_id
                where d.id=identifier::bigint and d.project_id=p_project and g.document_id=p_document
                  and d.source_family=family_hash) then
                raise exception 'minutes delta is outside its source family' using errcode='23514';
            end if;
            current_delta_ids:=current_delta_ids || identifier::bigint;
        end loop;
    end loop;
    insert into minutes_captures(project_id,document_id,extraction_run_id,source_family,source_revision,
        input_sha256,accepted_revision_id,output_json)
    values(p_project,p_document,p_run,p_family,p_revision,p_digest,p_accepted,p_output) returning id into result;
    for prior in select d.* from proposed_deltas d where d.project_id=p_project and d.source_family=family_hash
        and not(d.id=any(current_delta_ids))
        and not exists(select 1 from delta_dispositions where delta_id=d.id)
        and not exists(select 1 from delta_supersessions where prior_delta_id=d.id)
        -- Retired because its capture was corrected: already out of the
        -- actionable set, with its own explanation (ADR-0101).
        and public.proposed_delta_capture_correction(d.id) is null loop
        select d.id into replacement from proposed_deltas d where d.id=any(current_delta_ids)
            and d.target_subject_identity=prior.target_subject_identity
            and d.target_field is not distinct from prior.target_field order by d.id limit 1;
        insert into delta_supersessions(project_id,prior_delta_id,superseding_delta_id,minutes_capture_id,reason)
        values(p_project,prior.id,replacement,result,'newer_minutes_source_revision');
    end loop;
    return result;
end; $$;
alter function append_minutes_capture(bigint,bigint,bigint,text,text,text,bigint,jsonb) owner to corridor_source_append;
revoke all on function append_minutes_capture(bigint,bigint,bigint,text,text,text,bigint,jsonb) from public;
grant execute on function append_minutes_capture(bigint,bigint,bigint,text,text,text,bigint,jsonb) to corridor_worker,corridor_web;
grant select on project_baseline_adoptions,project_record_revisions,current_project_record,fact_decisions to corridor_source_append;
"""


def upgrade(op):
    op.execute(SCHEMA)
    for table, name, expression in CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)
    sql = op.get_bind().scalar(sa.text("select pg_get_functiondef(oid) from pg_proc where proname='append_fact' and pronamespace='public'::regnamespace"))
    needle = "            if p_satellites ? 'timings' then"
    assert sql.count(needle) == 1
    op.execute(sql.replace(needle, SCOPE_APPEND + needle))
    op.execute("revoke all on minutes_captures from corridor_web,corridor_worker")
    op.execute("grant select on minutes_captures to corridor_web,corridor_worker")
    op.execute("grant select,insert on minutes_captures to corridor_source_append")
    op.execute("grant usage,select on sequence minutes_captures_id_seq to corridor_source_append")
    op.execute("alter table minutes_captures enable row level security")
    op.execute("create policy p_minutes_captures_project_partition on minutes_captures to corridor_web using(project_id=any(current_project_partition()))")
    op.execute("create policy p_minutes_internal on minutes_captures to corridor_worker,corridor_source_append using(true) with check(true)")
    op.execute(APPEND)


def downgrade(op):
    if op.get_bind().scalar(sa.text("select exists(select 1 from minutes_captures)")):
        raise RuntimeError("minutes capture history cannot be represented by the predecessor")
    op.execute("drop function append_minutes_capture(bigint,bigint,bigint,text,text,text,bigint,jsonb)")
    op.execute("alter table delta_supersessions drop constraint ck_delta_supersessions_successor")
    op.execute("alter table delta_supersessions drop column minutes_capture_id")
    op.execute("alter table delta_supersessions add constraint ck_delta_supersessions_successor check(superseding_delta_id is not null or source_reading_id is not null)")
    op.execute("drop table minutes_captures")
    op.execute("drop function guard_minutes_capture()")
    op.execute("alter table fact_applies_to drop constraint ck_fact_applies_to_target")
    op.execute("alter table fact_applies_to drop column record_subject_key, drop column source_segment_id, drop column reference_text")
    op.execute("alter table fact_applies_to alter column dependency_id set not null")
    for table, name, expression in CHECKS_BEFORE:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)


CHECKS = [('fact_closure_results',
  'ck_fact_closure_result_kind',
  "closure_kind in ('source_marked_resolved', 'constraint_closed', 'constraint_remains_open', "
  "'completion_reported')"),
 ('fact_statement_timings',
  'ck_fact_statement_timing_bounds',
  "(precision = 'day' and start_date is not null and end_date = start_date) or (precision = 'month' and "
  "start_date is not null and end_date is not null and start_date = date_trunc('month', "
  "start_date::timestamp)::date and end_date = (date_trunc('month', start_date::timestamp) + interval "
  "'1 month - 1 day')::date) or (precision = 'range' and start_date is not null and end_date is not "
  "null and start_date <= end_date) or (precision = 'approximate' and start_date is null and end_date "
  'is null)'),
 ('fact_statement_timings',
  'ck_fact_statement_timing_precision',
  "precision in ('day', 'month', 'range', 'approximate')"),
 ('facts',
  'ck_facts_source_binding',
  '(document_id is not null and extraction_run_id is not null and fact_type not in '
  "('supporting_documentation_in_use')) or (document_id is null and extraction_run_id is null and "
  "fact_type in ('statement_wording', 'statement_timing', 'applies_to', "
  "'supporting_documentation_in_use'))"),
 ('facts',
  'ck_facts_subject',
  "(fact_type in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', "
  "'utility_subtype', 'utility_function', 'operational_status', 'size', 'material', 'oh_ug', "
  "'row_placement', 'orientation', 'baseline', 'station_from', 'station_to', 'offset_from', "
  "'offset_to', 'sue_level', 'conflict_description', 'resolution_strategy', 'notes', 'alignment', "
  "'location_start', 'location_end', 'offset_side', 'potential_conflict', 'data_source', "
  "'marked_resolution', 'committed_date', 'action_due_date', 'need_date') and subject_kind = "
  "'source_row' and length(trim(subject_key)) > 0) or (fact_type in ('applies_to', 'closure_result') "
  "and subject_kind = 'source_row' and length(trim(subject_key)) > 0) or (fact_type in "
  "('statement_wording', 'statement_timing', 'applies_to', 'closure_result') and subject_kind = "
  "'statement_candidate' and length(trim(subject_key)) > 0) or (fact_type = "
  "'supporting_documentation_in_use' and subject_kind = 'record_subject' and length(trim(subject_key)) "
  "> 0) or (fact_type in ('email_header', 'email_attachment') and subject_kind = 'email_message' and "
  'length(trim(subject_key)) > 0)')]
CHECKS_BEFORE = [('fact_closure_results',
  'ck_fact_closure_result_kind',
  "closure_kind in ('source_marked_resolved', 'constraint_closed', 'constraint_remains_open')"),
 ('fact_statement_timings',
  'ck_fact_statement_timing_bounds',
  "(precision = 'day' and start_date is not null and end_date = start_date) or (precision = 'month' and "
  "start_date is not null and end_date is not null and start_date = date_trunc('month', "
  "start_date::timestamp)::date and end_date = (date_trunc('month', start_date::timestamp) + interval "
  "'1 month - 1 day')::date) or (precision = 'approximate' and start_date is null and end_date is "
  'null)'),
 ('fact_statement_timings',
  'ck_fact_statement_timing_precision',
  "precision in ('day', 'month', 'approximate')"),
 ('facts',
  'ck_facts_source_binding',
  "(document_id is not null and extraction_run_id is not null and fact_type not in ('statement_timing', "
  "'supporting_documentation_in_use')) or (document_id is null and extraction_run_id is null and "
  "fact_type in ('statement_wording', 'statement_timing', 'applies_to', "
  "'supporting_documentation_in_use'))"),
 ('facts',
  'ck_facts_subject',
  "(fact_type in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', "
  "'utility_subtype', 'utility_function', 'operational_status', 'size', 'material', 'oh_ug', "
  "'row_placement', 'orientation', 'baseline', 'station_from', 'station_to', 'offset_from', "
  "'offset_to', 'sue_level', 'conflict_description', 'resolution_strategy', 'notes', 'alignment', "
  "'location_start', 'location_end', 'offset_side', 'potential_conflict', 'data_source', "
  "'marked_resolution', 'committed_date', 'action_due_date', 'need_date') and subject_kind = "
  "'source_row' and length(trim(subject_key)) > 0) or (fact_type in ('applies_to', 'closure_result') "
  "and subject_kind = 'source_row' and length(trim(subject_key)) > 0) or (fact_type in "
  "('statement_wording', 'statement_timing', 'applies_to') and subject_kind = 'statement_candidate' and "
  "length(trim(subject_key)) > 0) or (fact_type = 'supporting_documentation_in_use' and subject_kind = "
  "'record_subject' and length(trim(subject_key)) > 0) or (fact_type in ('email_header', "
  "'email_attachment') and subject_kind = 'email_message' and length(trim(subject_key)) > 0)")]
