"""Reference native supporting-document facts and preserve original act authority.

Publication support is a document relationship, not a Ready flag. These mappings
bind existing logical fact subjects to independent native subjects and retain
source citation lineage. Missing source/actor proof is an explicit routing gap.
"""

from sqlalchemy import text

SCHEMA = """
create table support_scope_lineage (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 subject_id uuid not null references coordination_record_subjects(id),
 legacy_dependency_id bigint not null,
 field_name text,
 fact_subject_key text not null,
 unique nulls not distinct(project_id,legacy_dependency_id,field_name),
 unique(project_id,fact_subject_key)
);
create table support_history_receipts (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 batch_id bigint references legacy_history_batches(id),
 scope_id bigint not null references support_scope_lineage(id),
 legacy_support_id bigint not null,
 legacy_evidence_link_id bigint not null,
 original_scope_sha256 text not null check(original_scope_sha256 ~ '^[0-9a-f]{64}$'),
 fact_decision_id bigint references fact_decisions(id),
 source_segment_id bigint references source_segments(id),
 outcome text not null check(outcome in ('native','retained_compatibility')),
 reason text not null,
 original_actor text not null,
 original_time timestamptz not null,
 policy_run_id bigint references policy_runs(id),
 policy_approval_id bigint references policy_approvals(id),
 recorded_at timestamptz not null default clock_timestamp(),
 predecessor_receipt_id bigint references support_history_receipts(id),
 unique nulls not distinct(scope_id,batch_id,original_scope_sha256,predecessor_receipt_id,outcome)
);
"""

COMMAND = """
create function bind_support_history(p_project bigint,p_batch bigint,p_support bigint,
 p_fact bigint,p_segment bigint,p_expected_digest text) returns bigint
language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
declare original operative_support; link evidence_links; fact facts; subject_uuid uuid;
 scope_row support_scope_lineage; digest text; source_key text; result bigint;
 actor text; policy_identity text; policy_run bigint; policy_approval bigint; failure text;
 active fact_decisions; revision bigint; prior_revision bigint;
 target_decision bigint; replacement bigint; previous_receipt bigint; locator_matches bigint; prior_native support_history_receipts;
begin
 if session_user='corridor_web' and not coalesce(p_project=any(current_project_partition()),false) then
  raise exception 'support history outside project partition' using errcode='23514';
 end if;
 if p_batch is not null and current_setting('transaction_isolation')<>'read committed' then
  raise exception 'support migration requires read committed post-lock visibility' using errcode='23514';
 end if;
 perform id from projects where id=p_project for update;
 select s.* into original from operative_support s join dependencies d on d.id=s.dependency_id
  where s.id=p_support and d.project_id=p_project;
 if not found then raise exception 'support scope outside project' using errcode='23514'; end if;
 digest:=encode(sha256(convert_to(to_jsonb(original)::text,'UTF8')),'hex');
 if digest is distinct from p_expected_digest then
  raise exception 'support designation changed since observation' using errcode='23514';
 end if;
 if original.role<>'publication' then
  raise exception 'readiness is a derivation, not a publication-support migration' using errcode='23514';
 end if;
 if p_batch is not null and not exists(select 1 from legacy_history_batches b,
  jsonb_array_elements(b.payload->'classes'->'operative_support'->'rows') r
  where b.id=p_batch and b.project_id=p_project and r=to_jsonb(original)
  and not exists(select 1 from legacy_history_reversals v where v.batch_id=b.id)) then
  raise exception 'support history differs from its active reviewed batch' using errcode='23514';
 end if;
 select * into link from evidence_links where id=original.evidence_link_id;
 if not found or not exists(select 1 from documents where id=link.document_id and project_id=p_project) then
  raise exception 'support document is outside project' using errcode='23514';
 end if;
 source_key:='dependency:'||original.dependency_id::text||case when original.field_name is null then '' else ':field:'||original.field_name end;
 select * into scope_row from support_scope_lineage where project_id=p_project and fact_subject_key=source_key;
 if not found then
  if p_batch is null then return null; end if;
  select subject_id into subject_uuid from coordination_subject_lineage where project_id=p_project and legacy_dependency_id=original.dependency_id;
  if subject_uuid is null then
   insert into coordination_record_subjects(project_id,subject_kind) values(p_project,'constraint') returning id into subject_uuid;
   insert into coordination_subject_lineage(project_id,subject_id,legacy_dependency_id,history_batch_id)
    values(p_project,subject_uuid,original.dependency_id,p_batch);
  end if;
  insert into support_scope_lineage(project_id,subject_id,legacy_dependency_id,field_name,fact_subject_key)
   values(p_project,subject_uuid,original.dependency_id,original.field_name,source_key) returning * into scope_row;
 end if;
 if p_batch is null and not exists(select 1 from support_history_receipts admitted
   where admitted.scope_id=scope_row.id and admitted.batch_id is not null and admitted.outcome='native'
    and not exists(select 1 from legacy_history_reversals v where v.batch_id=admitted.batch_id)) then
  raise exception 'support refresh requires an active reviewed native admission' using errcode='23514';
 end if;
 select max(id) into previous_receipt from support_history_receipts where scope_id=scope_row.id;
 select * into prior_native from support_history_receipts where scope_id=scope_row.id and outcome='native' order by id desc limit 1;
 select count(*) into locator_matches from source_segments
  where project_id=p_project and document_id=link.document_id and page_no=link.page_no
    and exact_text=link.quote and kind in ('prose_span','pdf_span')
    and content_sha256=encode(sha256(convert_to(exact_text,'UTF8')),'hex');
 if locator_matches>1 then failure:='ambiguous_exact_source_locator';
 elsif locator_matches<>1 or p_segment is null or not exists(select 1 from source_segments where id=p_segment
   and project_id=p_project and document_id=link.document_id and page_no=link.page_no
   and exact_text=link.quote and kind in ('prose_span','pdf_span')
   and content_sha256=encode(sha256(convert_to(exact_text,'UTF8')),'hex')) then
  failure:='missing_exact_source_locator';
 end if;
 if prior_native.original_scope_sha256=digest and prior_native.source_segment_id=p_segment then
  if not exists(select 1 from fact_decisions where id=prior_native.fact_decision_id and superseded_by is null and disposition='include') then
   failure:=coalesce(failure,'native_authority_advanced');
  elsif failure is null and prior_native.id=previous_receipt
    and (p_batch is null or prior_native.batch_id is not distinct from p_batch) then
   return prior_native.id;
  end if;
 end if;
 if valid_coordination_history_actor(original.designated_by) then
  if session_user='corridor_worker' then
   failure:=coalesce(failure,'worker_requires_recorded_policy_transfer');
  else
   actor:=original.designated_by;
  end if;
 elsif original.designated_by='corridor:automatic-carry-forward' then
  -- Runtime may append these legacy receipts, approvals and run outcomes.
  -- Agreement among their labels cannot authenticate a released policy or
  -- authorize a backdated Project Record Revision. Preserve the original
  -- protected designation as compatibility history until a protected native
  -- decision or a new attributable adoption supplies accepted authority.
  failure:=coalesce(failure,'unproven_original_policy_identity');
 end if;
 if actor is null and policy_identity is null then failure:=coalesce(failure,'unproven_original_authority'); end if;
 if failure is null then
  select * into fact from facts where id=p_fact and project_id=p_project
   and subject_key=source_key and fact_type='supporting_documentation_in_use' and document_value_id=link.document_id;
  if not found then raise exception 'support migration requires the matching registered-document Fact' using errcode='23514'; end if;
  select d.id into target_decision from fact_decisions d where d.fact_id=p_fact and d.superseded_by is null and d.disposition='include';
  if target_decision is not null and exists(select 1 from support_history_receipts prior_context
    where prior_context.id=(select max(id) from support_history_receipts where scope_id=scope_row.id and outcome='native')
      and prior_context.fact_decision_id=target_decision and prior_context.source_segment_id is distinct from p_segment) then
   -- Choosing a different source passage changes publication provenance even
   -- when the document relationship itself is unchanged. Bind that real act
   -- to a new revision instead of rewriting an earlier revision's context.
   target_decision:=null;
  end if;
  if target_decision is null then
   select max(id) into prior_revision from project_record_revisions where project_id=p_project;
   insert into project_record_revisions(project_id,predecessor_revision_id,command_type,human_principal,released_policy,idempotency_key,recorded_at)
    values(p_project,prior_revision,'designate_support',actor,policy_identity,
     'support-history:'||scope_row.id::text||':'||digest||':'||coalesce(previous_receipt,0)::text,original.designated_at) returning id into revision;
   for active in select d.* from fact_decisions d where d.project_id=p_project and d.subject_key=source_key
    and d.fact_type='supporting_documentation_in_use' and d.superseded_by is null
     and (d.disposition='include' or d.fact_id=p_fact) loop
    replacement:=nextval('fact_decisions_id_seq');
    update fact_decisions set superseded_by=replacement where id=active.id;
    insert into fact_decisions(id,project_id,fact_id,subject_key,fact_type,revision_id,disposition,decided_at)
     values(replacement,p_project,active.fact_id,source_key,'supporting_documentation_in_use',revision,
      case when active.fact_id=p_fact then 'include' else 'restore' end,original.designated_at);
    if active.fact_id=p_fact then target_decision:=replacement; end if;
   end loop;
   if target_decision is null then
    insert into fact_decisions(project_id,fact_id,subject_key,fact_type,revision_id,disposition,decided_at)
     values(p_project,p_fact,source_key,'supporting_documentation_in_use',revision,'include',original.designated_at)
     returning id into target_decision;
   end if;
  end if;
 end if;
 if failure is not null and exists(select 1 from support_history_receipts where id=previous_receipt
    and batch_id is not distinct from p_batch and original_scope_sha256=digest
    and outcome='retained_compatibility' and reason=failure) then return previous_receipt; end if;
 insert into support_history_receipts(project_id,batch_id,scope_id,legacy_support_id,legacy_evidence_link_id,
  original_scope_sha256,fact_decision_id,source_segment_id,outcome,reason,original_actor,original_time,policy_run_id,policy_approval_id,predecessor_receipt_id)
 values(p_project,p_batch,scope_row.id,original.id,original.evidence_link_id,digest,target_decision,
  case when failure is null then p_segment end,
  case when failure is null then 'native' else 'retained_compatibility' end,
  coalesce(failure,'original_authority_and_exact_source_preserved'),original.designated_by,original.designated_at,policy_run,policy_approval,previous_receipt)
 on conflict(scope_id,batch_id,original_scope_sha256,predecessor_receipt_id,outcome) do nothing returning id into result;
 if result is null then
  select id into result from support_history_receipts where scope_id=scope_row.id and batch_id is not distinct from p_batch
   and original_scope_sha256=digest and predecessor_receipt_id is not distinct from previous_receipt and outcome='retained_compatibility';
 end if;
 return result;
end; $$;
"""


def upgrade(op):
    op.execute(SCHEMA)
    op.execute(COMMAND)
    op.execute(SOURCE_DESCRIPTOR)
    for table in ("support_scope_lineage", "support_history_receipts"):
        op.execute(f"create trigger guard_{table} before insert or update or delete on {table} for each row execute function guard_coordination_record()")
        op.execute(f"revoke all on {table} from public,corridor_web,corridor_worker")
        op.execute(f"grant select on {table} to corridor_web,corridor_worker,corridor_history_operations")
        op.execute(f"grant select,insert on {table} to corridor_fact_decision_writer")
        op.execute(f"grant usage,select on sequence {table}_id_seq to corridor_fact_decision_writer")
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"create policy p_{table}_project_partition on {table} to corridor_web using(project_id=any(current_project_partition()))")
        op.execute(f"create policy p_{table}_internal on {table} to corridor_worker,corridor_fact_decision_writer,corridor_history_operations using(true) with check(true)")
    # Operations may capture a validated source Fact, never raw Fact/decision
    # rows. The standard appender retains all scope/type/digest checks.
    op.execute("grant execute on function append_fact(bigint,bigint,bigint,varchar,varchar,text,text,date,bigint,bigint,varchar,varchar,varchar,jsonb,jsonb) to corridor_history_operations")
    for table in ("documents", "facts"):
        op.execute(f"grant select on {table} to corridor_history_operations")
        op.execute(f"create policy p_{table}_history_operations_read on {table} for select to corridor_history_operations using(true)")
    op.execute("alter function bind_support_history(bigint,bigint,bigint,bigint,bigint,text) owner to corridor_fact_decision_writer")
    op.execute("revoke all on function bind_support_history(bigint,bigint,bigint,bigint,bigint,text) from public")
    op.execute("""
        create function migrate_support_scope(bigint,bigint,bigint,bigint,bigint,text) returns bigint
        language sql security definer set search_path=public,pg_temp as $$
          select bind_support_history($1,$2,$3,$4,$5,$6);
        $$;
        create function refresh_support_scope(bigint,bigint,bigint,bigint,text) returns bigint
        language sql security definer set search_path=public,pg_temp as $$
          select bind_support_history($1,null,$2,$3,$4,$5);
        $$;
        alter function migrate_support_scope(bigint,bigint,bigint,bigint,bigint,text) owner to corridor_fact_decision_writer;
        alter function refresh_support_scope(bigint,bigint,bigint,bigint,text) owner to corridor_fact_decision_writer;
        revoke all on function migrate_support_scope(bigint,bigint,bigint,bigint,bigint,text) from public;
        revoke all on function refresh_support_scope(bigint,bigint,bigint,bigint,text) from public;
        grant execute on function migrate_support_scope(bigint,bigint,bigint,bigint,bigint,text) to corridor_history_operations;
        grant execute on function refresh_support_scope(bigint,bigint,bigint,bigint,text) to corridor_web,corridor_worker;
    """)


SOURCE_DESCRIPTOR = """
create function support_scope_source(p_project bigint,p_scope bigint) returns jsonb
language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
declare original operative_support; link evidence_links;
begin
 if session_user='corridor_web' and not coalesce(p_project=any(current_project_partition()),false) then
  raise exception 'support source outside project partition' using errcode='23514';
 end if;
 select s.* into original from operative_support s join dependencies d on d.id=s.dependency_id
  where s.id=p_scope and d.project_id=p_project and s.role='publication';
 if not found then return null; end if;
 select * into link from evidence_links where id=original.evidence_link_id;
 return jsonb_build_object('original',to_jsonb(original),'evidence',to_jsonb(link),
  'digest',encode(sha256(convert_to(to_jsonb(original)::text,'UTF8')),'hex'),
  'segments',(select coalesce(jsonb_agg(s.id),'[]'::jsonb) from source_segments s
    where s.project_id=p_project and s.document_id=link.document_id and s.page_no=link.page_no
     and s.kind in ('prose_span','pdf_span') and s.exact_text=link.quote
     and s.content_sha256=encode(sha256(convert_to(s.exact_text,'UTF8')),'hex')));
end; $$;
alter function support_scope_source(bigint,bigint) owner to corridor_fact_decision_writer;
revoke all on function support_scope_source(bigint,bigint) from public;
grant execute on function support_scope_source(bigint,bigint) to corridor_web,corridor_worker,corridor_history_operations;
"""


def downgrade(op):
    for table in ("support_scope_lineage", "support_history_receipts"):
        if op.get_bind().scalar(text(f"select exists(select 1 from {table})")):
            raise RuntimeError("native support lineage and original authority cannot be discarded by downgrade")
    for function in ("refresh_support_scope(bigint,bigint,bigint,bigint,text)",
                     "migrate_support_scope(bigint,bigint,bigint,bigint,bigint,text)",
                     "bind_support_history(bigint,bigint,bigint,bigint,bigint,text)",
                     "support_scope_source(bigint,bigint)"):
        op.execute(f"drop function {function}")
    op.execute("drop table support_history_receipts")
    op.execute("drop table support_scope_lineage")
    op.execute("revoke execute on function append_fact(bigint,bigint,bigint,varchar,varchar,text,text,date,bigint,bigint,varchar,varchar,varchar,jsonb,jsonb) from corridor_history_operations")
    for table in ("documents", "facts"):
        op.execute(f"drop policy p_{table}_history_operations_read on {table}")
        op.execute(f"revoke select on {table} from corridor_history_operations")
