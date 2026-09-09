"""Native Coordination Decisions retain human authority without Source Facts.

Legacy identities live only in explicit compatibility mappings. The native
current/as-of view joins native subjects, decisions and revisions; a historical
import derives every actor, time and value from the sealed original rows.
"""

SCHEMA = """
create function valid_coordination_history_actor(actor text) returns boolean
 language sql immutable as $$
 select coalesce(actor ~ '^[a-z][a-z0-9._-]{1,31}:[^[:space:]]+$'
  and actor !~* '^[^:]+:(agent|demo|extractor|reviewer|system)$',false);
$$;
create function attributable_coordination_subject(p_dependency bigint,p_lineage bigint) returns boolean
 language sql stable as $$
 select not exists(select 1 from work_decisions w
  where (w.dependency_id=p_dependency or w.commitment_lineage_id=p_lineage)
  and (not valid_coordination_history_actor(w.recorded_by)
   or exists(select 1 from statement_coordination_receipts r
    where w.id in (r.internal_owner_decision_id,r.next_action_decision_id,r.milestone_impact_decision_id)
    and r.recorded_by is distinct from w.recorded_by)
   or exists(select 1 from follow_up_plan_receipts r
    where w.id in (r.internal_owner_decision_id,r.next_action_decision_id,r.resumed_deferral_decision_id)
    and r.recorded_by is distinct from w.recorded_by)
   or exists(select 1 from statement_coordination_receipts r join statement_coordination_reversals u on u.receipt_id=r.id
    where w.id in (r.internal_owner_decision_id,r.next_action_decision_id,r.milestone_impact_decision_id)
    and not valid_coordination_history_actor(u.recorded_by))));
$$;

create table coordination_record_subjects (
 id uuid primary key default gen_random_uuid(),
 project_id bigint not null references projects(id),
 subject_kind text not null check(subject_kind in ('constraint','commitment')),
 created_at timestamptz not null default clock_timestamp(),
 unique(project_id,id)
);
create table coordination_subject_lineage (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 subject_id uuid not null references coordination_record_subjects(id),
 legacy_dependency_id bigint unique,
 legacy_commitment_lineage_id bigint unique,
 history_batch_id bigint not null references legacy_history_batches(id),
 check(num_nonnulls(legacy_dependency_id,legacy_commitment_lineage_id)=1),
 unique(subject_id)
);
create table coordination_history_activations (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 subject_id uuid not null references coordination_record_subjects(id),
 history_batch_id bigint not null references legacy_history_batches(id),
 recorded_at timestamptz not null default clock_timestamp(),
 unique(subject_id,history_batch_id)
);
create table coordination_record_decisions (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 subject_id uuid not null references coordination_record_subjects(id),
 revision_id bigint not null references project_record_revisions(id),
 field text not null check(field in ('internal_owner','next_action','milestone_impact','deferral')),
 decision_type text not null check(decision_type in ('assign_internal_owner','set_next_action','complete_next_action','cancel_next_action','set_milestone_impact','defer_work','resume_work','undo_follow_up_plan')),
 value_text text,
 action_due_date date,
 action_due_date_reason text,
 milestone_ids bigint[] not null default '{}',
 deferral_reason text,
 deferral_return_date date,
 no_follow_up_reason text,
 cancellation_reason text,
 note text,
 recorded_by text not null check(valid_coordination_history_actor(recorded_by)),
 recorded_at timestamptz not null,
 predecessor_id bigint unique references coordination_record_decisions(id),
 unique(project_id,id),
 foreign key(project_id,subject_id) references coordination_record_subjects(project_id,id)
);
create unique index uq_coordination_record_root on coordination_record_decisions(subject_id,field) where predecessor_id is null;
create table coordination_decision_lineage (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 decision_id bigint not null unique references coordination_record_decisions(id),
 legacy_work_decision_id bigint not null unique,
 history_batch_id bigint references legacy_history_batches(id),
 original_content_sha256 text not null check(original_content_sha256 ~ '^[0-9a-f]{64}$'),
 observation_lineage jsonb not null default '{}'
);
create table coordination_record_reversals (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 decision_id bigint not null unique references coordination_record_decisions(id),
 revision_id bigint not null references project_record_revisions(id),
 recorded_by text not null check(valid_coordination_history_actor(recorded_by)),
 recorded_at timestamptz not null
);
create table coordination_reversal_lineage (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 reversal_id bigint not null unique references coordination_record_reversals(id),
 legacy_statement_reversal_id bigint not null,
 unique(legacy_statement_reversal_id,reversal_id)
);
create function guard_coordination_record() returns trigger language plpgsql as $$
begin
 if tg_op <> 'INSERT' or current_user <> 'corridor_fact_decision_writer' then
  raise exception 'Coordination Decisions are immutable and command-owned' using errcode='23514';
 end if;
 return new;
end; $$;
create view current_coordination_record as
 select d.* from coordination_record_decisions d
 where not exists(select 1 from coordination_record_decisions successor where successor.predecessor_id=d.id)
 and not exists(select 1 from coordination_record_reversals reversed where reversed.decision_id=d.id)
 and exists(select 1 from coordination_history_activations a where a.subject_id=d.subject_id
  and not exists(select 1 from legacy_history_reversals r where r.batch_id=a.history_batch_id));
"""


IMPORT_ONE = """
create function import_coordination_decision(p_project bigint,p_legacy bigint,p_batch bigint,p_operation text)
 returns bigint language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
declare original work_decisions; subject_uuid uuid; prior bigint; revision bigint; result bigint;
 digest text; existing coordination_decision_lineage; prior_revision bigint; value jsonb;
 kind text; source_project bigint; operation text; expected_actor text; event_time timestamptz;
begin
 perform id from projects where id=p_project for update;
 select * into original from work_decisions where id=p_legacy;
 if not found then raise exception 'original Coordination Decision is missing' using errcode='23514'; end if;
 if not valid_coordination_history_actor(original.recorded_by) then
  raise exception 'original Coordination Decision has no attributable human identity' using errcode='23514';
 end if;
 if original.dependency_id is not null then
  select project_id into source_project from dependencies where id=original.dependency_id; kind:='constraint';
 else
  select project_id into source_project from commitment_lineages where id=original.commitment_lineage_id; kind:='commitment';
 end if;
 if source_project is distinct from p_project then
  raise exception 'Coordination Decision is outside project' using errcode='23514';
 end if;
 digest:=encode(sha256(convert_to(to_jsonb(original)::text,'UTF8')),'hex');
 select * into existing from coordination_decision_lineage where legacy_work_decision_id=p_legacy;
 if found then
  if existing.original_content_sha256 is distinct from digest then
   raise exception 'original Coordination Decision changed after migration' using errcode='23514';
  end if;
  return existing.decision_id;
 end if;
 if p_batch is not null and not exists(
  select 1 from legacy_history_batches b, jsonb_array_elements(b.payload->'classes'->'work_decisions'->'rows') r
  where b.id=p_batch and b.project_id=p_project and r=to_jsonb(original)
   and not exists(select 1 from legacy_history_reversals reversed where reversed.batch_id=b.id)) then
  raise exception 'original decision differs from the active reviewed history batch' using errcode='23514';
 end if;
 select subject_id into subject_uuid from coordination_subject_lineage
  where project_id=p_project and (legacy_dependency_id=original.dependency_id or legacy_commitment_lineage_id=original.commitment_lineage_id);
 if subject_uuid is null then
  insert into coordination_record_subjects(project_id,subject_kind) values(p_project,kind) returning id into subject_uuid;
  insert into coordination_subject_lineage(project_id,subject_id,legacy_dependency_id,legacy_commitment_lineage_id,history_batch_id)
   values(p_project,subject_uuid,original.dependency_id,original.commitment_lineage_id,p_batch);
 end if;
 if original.predecessor_decision_id is null and original.before_value is not null then
  raise exception 'original Coordination Decision lacks its before-value lineage' using errcode='23514';
 end if;
 if original.predecessor_decision_id is not null then
  if not exists(select 1 from work_decisions where id=original.predecessor_decision_id and after_value is not distinct from original.before_value) then
   raise exception 'original Coordination Decision before-value disagrees with its predecessor' using errcode='23514';
  end if;
  select decision_id into prior from coordination_decision_lineage where legacy_work_decision_id=original.predecessor_decision_id;
  if prior is null then raise exception 'native predecessor requires the earlier migration batch' using errcode='23514'; end if;
  if not exists(select 1 from coordination_record_decisions where id=prior and subject_id=subject_uuid and field=original.field)
   or exists(select 1 from coordination_record_decisions where predecessor_id=prior) then
   raise exception 'native Coordination Decision predecessor is stale or outside subject' using errcode='23514';
  end if;
 end if;
 operation:=p_operation; expected_actor:=original.recorded_by; event_time:=original.recorded_at;
 if operation is null then
  select 'legacy-coordinate:statement:'||id::text,recorded_by,created_at into operation,expected_actor,event_time
   from statement_coordination_receipts where p_legacy in (internal_owner_decision_id,next_action_decision_id,milestone_impact_decision_id);
  if not found then
   select 'legacy-coordinate:plan:'||id::text,recorded_by,created_at into operation,expected_actor,event_time
    from follow_up_plan_receipts where p_legacy in (internal_owner_decision_id,next_action_decision_id,resumed_deferral_decision_id);
  end if;
  if not found then
   select 'legacy-coordinate:undo-plan:'||id::text,recorded_by,created_at into operation,expected_actor,event_time
    from follow_up_plan_reversals where p_legacy in (internal_owner_reversal_decision_id,next_action_reversal_decision_id,deferral_reversal_decision_id);
  end if;
  if operation is null then
   operation:='legacy-coordinate:decision:'||p_legacy::text; expected_actor:=original.recorded_by; event_time:=original.recorded_at;
  end if;
 end if;
 if expected_actor is distinct from original.recorded_by then
  raise exception 'grouped Coordination Decisions disagree about original authorship' using errcode='23514';
 end if;
 select id into revision from project_record_revisions where project_id=p_project and idempotency_key=operation;
 if revision is not null and not exists(select 1 from project_record_revisions where id=revision and human_principal=expected_actor and command_type='coordinate_record') then
  raise exception 'Coordination Decision operation belongs to another authority' using errcode='23514';
 end if;
 if revision is null then
  select max(id) into prior_revision from project_record_revisions where project_id=p_project;
  insert into project_record_revisions(project_id,predecessor_revision_id,command_type,human_principal,released_policy,idempotency_key,recorded_at)
   values(p_project,prior_revision,'coordinate_record',expected_actor,null,operation,event_time) returning id into revision;
 end if;
 value:=case when original.field in ('next_action','milestone_impact','deferral') and original.after_value is not null then original.after_value::jsonb else '{}'::jsonb end;
 if original.decision_type<>'undo_follow_up_plan' and not (
  (original.field='internal_owner' and original.decision_type='assign_internal_owner')
  or (original.field='next_action' and original.decision_type in ('set_next_action','complete_next_action','cancel_next_action'))
  or (original.field='milestone_impact' and original.decision_type='set_milestone_impact')
  or (original.field='deferral' and original.decision_type in ('defer_work','resume_work'))) then
  raise exception 'original Coordination Decision type disagrees with its field' using errcode='23514';
 end if;
 if original.after_value is not null and original.field='next_action' and (
   jsonb_typeof(value)<>'object' or not value ?& array['action','due_date']
   or value-array['action','due_date']<>'{}'::jsonb
   or jsonb_typeof(value->'action')<>'string'
   or (value->>'due_date' is not null and value->>'due_date' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')) then
  raise exception 'original Next Action is not a lossless typed value' using errcode='23514';
 end if;
 if original.after_value is not null and original.field='milestone_impact' and (
   jsonb_typeof(value)<>'object' or not value ?& array['state','milestone_ids']
   or value-array['state','milestone_ids']<>'{}'::jsonb
   or value->>'state' not in ('affects','does_not_affect','not_yet_known')
   or jsonb_typeof(value->'milestone_ids')<>'array') then
  raise exception 'original Milestone Impact is not a lossless typed value' using errcode='23514';
 end if;
 if original.after_value is not null and original.field='deferral' and (
   value is distinct from jsonb_build_object('reason',original.deferral_reason,'return_date',original.deferral_return_date::text)) then
  raise exception 'original deferral disagrees with its typed fields' using errcode='23514';
 end if;

 insert into coordination_record_decisions(project_id,subject_id,revision_id,field,decision_type,value_text,
  action_due_date,action_due_date_reason,milestone_ids,deferral_reason,deferral_return_date,no_follow_up_reason,
  cancellation_reason,note,recorded_by,recorded_at,predecessor_id)
 values(p_project,subject_uuid,revision,original.field,original.decision_type,
  case original.field when 'internal_owner' then original.after_value when 'next_action' then value->>'action'
   when 'milestone_impact' then value->>'state' else null end,
  (value->>'due_date')::date,original.action_due_date_reason,
  case when original.field='milestone_impact' then array(select jsonb_array_elements_text(coalesce(value->'milestone_ids','[]'::jsonb))::bigint) else '{}'::bigint[] end,
  original.deferral_reason,original.deferral_return_date,original.no_follow_up_reason,original.cancellation_reason,
  original.note,original.recorded_by,original.recorded_at,prior) returning id into result;
 insert into coordination_decision_lineage(project_id,decision_id,legacy_work_decision_id,history_batch_id,original_content_sha256,observation_lineage)
 values(p_project,result,p_legacy,p_batch,digest,jsonb_build_object(
  'observed_statement_event_id',original.observed_statement_event_id,
  'observed_scope_decision_id',original.observed_scope_decision_id,
  'observed_milestone_impact_decision_id',original.observed_milestone_impact_decision_id));
 return result;
end; $$;
"""


COMMANDS = """
create function migrate_coordination_history(p_project bigint,p_batch bigint) returns bigint
 language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
declare item jsonb; count bigint:=0; subject_uuid uuid; class_name text; reviewed jsonb; current_content jsonb; previously_imported boolean;
begin
 perform id from projects where id=p_project for update;
 if not exists(select 1 from legacy_history_batches where id=p_batch and project_id=p_project)
  or exists(select 1 from legacy_history_reversals where batch_id=p_batch) then
  raise exception 'Coordination history requires an active reviewed batch' using errcode='23514';
 end if;
 select payload into reviewed from legacy_history_batches where id=p_batch;
 previously_imported:=exists(select 1 from coordination_subject_lineage where history_batch_id=p_batch)
  and not exists(select 1 from jsonb_array_elements(reviewed->'classes'->'work_decisions'->'rows') r
   where not exists(select 1 from coordination_decision_lineage where legacy_work_decision_id=(r->>'id')::bigint));
 if not previously_imported then
  current_content:=legacy_history_content(p_project);
  foreach class_name in array array['work_decisions','statement_coordination_receipts','statement_coordination_reversals','follow_up_plan_receipts','follow_up_plan_reversals'] loop
   if current_content->'classes'->class_name->'rows' is distinct from reviewed->'classes'->class_name->'rows' then
    raise exception 'coordination history changed since the reviewed batch' using errcode='23514';
   end if;
  end loop;
 end if;
 foreach class_name in array array['dependencies','commitment_lineages'] loop
  for item in select r from legacy_history_batches b,
   jsonb_array_elements(b.payload->'classes'->class_name->'rows') r where b.id=p_batch loop
   if not attributable_coordination_subject(
       case when class_name='dependencies' then (item->>'id')::bigint end,
       case when class_name='commitment_lineages' then (item->>'id')::bigint end) then
    continue;
   end if;
   if not exists(select 1 from coordination_subject_lineage where project_id=p_project and
     ((class_name='dependencies' and legacy_dependency_id=(item->>'id')::bigint)
      or (class_name='commitment_lineages' and legacy_commitment_lineage_id=(item->>'id')::bigint))) then
    insert into coordination_record_subjects(project_id,subject_kind)
     values(p_project,case when class_name='dependencies' then 'constraint' else 'commitment' end) returning id into subject_uuid;
    insert into coordination_subject_lineage(project_id,subject_id,legacy_dependency_id,legacy_commitment_lineage_id,history_batch_id)
     values(p_project,subject_uuid,case when class_name='dependencies' then (item->>'id')::bigint end,
      case when class_name='commitment_lineages' then (item->>'id')::bigint end,p_batch);
   end if;
   select subject_id into subject_uuid from coordination_subject_lineage where project_id=p_project
    and ((class_name='dependencies' and legacy_dependency_id=(item->>'id')::bigint)
     or (class_name='commitment_lineages' and legacy_commitment_lineage_id=(item->>'id')::bigint));
   insert into coordination_history_activations(project_id,subject_id,history_batch_id)
    values(p_project,subject_uuid,p_batch) on conflict(subject_id,history_batch_id) do nothing;
  end loop;
 end loop;
 for item in select r from legacy_history_batches b,
  jsonb_array_elements(b.payload->'classes'->'work_decisions'->'rows') r
  where b.id=p_batch order by (r->>'recorded_at')::timestamptz,(r->>'id')::bigint loop
  if exists(select 1 from coordination_subject_lineage where project_id=p_project
    and (legacy_dependency_id=(item->>'dependency_id')::bigint or legacy_commitment_lineage_id=(item->>'commitment_lineage_id')::bigint)) then
   perform import_coordination_decision(p_project,(item->>'id')::bigint,p_batch,null); count:=count+1;
  end if;
 end loop;
 perform sync_coordination_reversals(p_project);
 return count;
end; $$;
create function mirror_coordination_decision(p_project bigint,p_legacy bigint,p_operation text) returns bigint
 language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
begin
 if session_user='corridor_web' and not coalesce(p_project=any(current_project_partition()),false) then
  raise exception 'Coordination Decision outside project partition' using errcode='23514';
 end if;
 if not exists(select 1 from work_decisions w join coordination_subject_lineage l
  on l.legacy_dependency_id=w.dependency_id or l.legacy_commitment_lineage_id=w.commitment_lineage_id
  where w.id=p_legacy and l.project_id=p_project
  and exists(select 1 from coordination_history_activations a where a.subject_id=l.subject_id
   and not exists(select 1 from legacy_history_reversals r where r.batch_id=a.history_batch_id))) then
  -- Legacy projects remain on the explicitly unconverted route until the
  -- reviewed historical batch establishes complete native predecessor chains.
  return null;
 end if;
 if p_operation is null or p_operation not like 'coordinate:%' then
  raise exception 'live coordination requires a bounded operation identity' using errcode='23514';
 end if;
 return import_coordination_decision(p_project,p_legacy,null,p_operation);
end; $$;
create function sync_coordination_reversals(p_project bigint) returns bigint
 language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
declare original record; revision bigint; prior_revision bigint; target bigint; result bigint; count bigint:=0;
begin
 if session_user='corridor_web' and not coalesce(p_project=any(current_project_partition()),false) then
  raise exception 'Coordination reversal outside project partition' using errcode='23514';
 end if;
 perform id from projects where id=p_project for update;
 for original in select r.*,s.internal_owner_decision_id,s.next_action_decision_id,s.milestone_impact_decision_id
  from statement_coordination_reversals r join statement_coordination_receipts s on s.id=r.receipt_id
  join candidates c on c.id=s.candidate_id where c.project_id=p_project order by r.created_at,r.id loop
  for target in select l.decision_id from coordination_decision_lineage l where l.project_id=p_project
   and l.legacy_work_decision_id in (original.internal_owner_decision_id,original.next_action_decision_id,original.milestone_impact_decision_id)
   and not exists(select 1 from coordination_record_reversals r where r.decision_id=l.decision_id) loop
   select id into revision from project_record_revisions where project_id=p_project and idempotency_key='legacy-coordinate:undo-statement:'||original.id::text;
   if revision is not null and not exists(select 1 from project_record_revisions where id=revision
      and human_principal=original.recorded_by and command_type='reverse_coordination') then
    raise exception 'Coordination reversal operation belongs to another authority' using errcode='23514';
   end if;
   if revision is null then
    select max(id) into prior_revision from project_record_revisions where project_id=p_project;
    insert into project_record_revisions(project_id,predecessor_revision_id,command_type,human_principal,released_policy,idempotency_key,recorded_at)
    values(p_project,prior_revision,'reverse_coordination',original.recorded_by,null,'legacy-coordinate:undo-statement:'||original.id::text,original.created_at)
    returning id into revision;
   end if;
   insert into coordination_record_reversals(project_id,decision_id,revision_id,recorded_by,recorded_at)
   values(p_project,target,revision,original.recorded_by,original.created_at) returning id into result;
   insert into coordination_reversal_lineage(project_id,reversal_id,legacy_statement_reversal_id) values(p_project,result,original.id);
   count:=count+1;
  end loop;
 end loop;
 return count;
end; $$;
"""

TABLES = ("coordination_record_subjects", "coordination_subject_lineage", "coordination_history_activations", "coordination_record_decisions",
          "coordination_decision_lineage", "coordination_record_reversals", "coordination_reversal_lineage")


def upgrade(op):
    op.execute(SCHEMA)
    op.execute(IMPORT_ONE)
    # PL/pgSQL resolves the sibling function when called, after both exist.
    op.execute(COMMANDS)
    for table in TABLES:
        op.execute(f"create trigger guard_{table} before insert or update or delete on {table} for each row execute function guard_coordination_record()")
        op.execute(f"revoke all on {table} from public,corridor_web,corridor_worker")
        op.execute(f"grant select on {table} to corridor_web,corridor_worker")
        op.execute(f"grant select,insert on {table} to corridor_fact_decision_writer")
        if table != "coordination_record_subjects":
            op.execute(f"grant usage,select on sequence {table}_id_seq to corridor_fact_decision_writer")
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"create policy p_{table}_partition on {table} to corridor_web using(project_id=any(current_project_partition()))")
        op.execute(f"create policy p_{table}_internal on {table} to corridor_worker,corridor_fact_decision_writer using(true) with check(true)")
    for name, signature, runtime in (
        ("import_coordination_decision", "(bigint,bigint,bigint,text)", False),
        ("migrate_coordination_history", "(bigint,bigint)", False),
        ("mirror_coordination_decision", "(bigint,bigint,text)", True),
        ("sync_coordination_reversals", "(bigint)", True),
    ):
        op.execute(f"alter function {name}{signature} owner to corridor_fact_decision_writer")
        op.execute(f"revoke all on function {name}{signature} from public")
        if runtime:
            op.execute(f"grant execute on function {name}{signature} to corridor_web")
    op.execute("revoke all on function guard_coordination_record() from public")
    op.execute("revoke all on function attributable_coordination_subject(bigint,bigint) from public")
    op.execute("grant execute on function attributable_coordination_subject(bigint,bigint) to corridor_fact_decision_writer")
    op.execute("alter view current_coordination_record set (security_invoker=true)")
    op.execute("grant select on current_coordination_record to corridor_web,corridor_worker,corridor_fact_decision_writer")
