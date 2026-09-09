"""Frozen Impact Derivation storage in the one supported transition (#643)."""

import sqlalchemy as sa


SCHEMA = """
create table proposed_delta_impact_derivations (
    id bigserial primary key, project_id bigint not null references projects(id),
    delta_id bigint not null, rule text not null check(length(rule)>0),
    rule_version text not null check(length(rule_version)>0),
    accepted_revision_id bigint references project_record_revisions(id),
    inputs jsonb not null check(jsonb_typeof(inputs)='object'),
    input_sha256 varchar(64) not null check(input_sha256 ~ '^[0-9a-f]{64}$'),
    evaluated_at timestamptz not null,
    affected_constraint_ids jsonb not null check(jsonb_typeof(affected_constraint_ids)='array'),
    affected_key_dates jsonb not null check(jsonb_typeof(affected_key_dates)='array'),
    derivation_sha256 varchar(64) not null check(derivation_sha256 ~ '^[0-9a-f]{64}$'),
    foreign key(project_id,delta_id) references proposed_deltas(project_id,id),
    unique(project_id,delta_id,rule,rule_version,input_sha256)
);
create function guard_delta_impact() returns trigger language plpgsql as $$
begin
    if tg_op <> 'INSERT' or current_user <> 'corridor_source_append' then
        raise exception 'impact derivations require immutable source append' using errcode='23514';
    end if;
    return new;
end; $$;
create trigger delta_impact_guard before insert or update or delete on proposed_delta_impact_derivations
    for each row execute function guard_delta_impact();
revoke all on function guard_delta_impact() from public;
"""

COMMAND = """
create function append_delta_impact(p_project bigint,p_delta bigint,p_rule text,p_version text,
    p_inputs jsonb,p_evaluated timestamptz,p_constraints jsonb,p_dates jsonb)
returns bigint language plpgsql security definer set search_path to 'public' as $$
declare delta proposed_deltas%rowtype; revision_id bigint; input_hash text; derivation_hash text;
    existing proposed_delta_impact_derivations%rowtype; result bigint;
begin
    if session_user='corridor_web' and not coalesce(p_project=any(current_project_partition()),false) then
        raise exception 'impact outside project partition' using errcode='23514';
    end if;
    perform pg_advisory_xact_lock(643,p_project::integer);
    select * into delta from proposed_deltas where id=p_delta and project_id=p_project;
    if not found then raise exception 'impact delta outside project' using errcode='23514'; end if;
    if delta.accepted_baseline_revision is not null then
        select id into revision_id from project_record_revisions where project_id=p_project
            and 'revision:' || id::text=delta.accepted_baseline_revision;
        if not found then raise exception 'impact revision outside project' using errcode='23514'; end if;
    end if;
    if p_inputs ->> 'accepted_baseline_revision' is distinct from delta.accepted_baseline_revision then
        raise exception 'impact inputs disagree with compared revision' using errcode='23514';
    end if;
    input_hash := encode(sha256(convert_to(jsonb_build_object('inputs',p_inputs,
        'revision',delta.accepted_baseline_revision)::text,'UTF8')),'hex');
    derivation_hash := encode(sha256(convert_to(jsonb_build_object('project',p_project,'delta',p_delta,
        'rule',p_rule,'version',p_version,'input_sha256',input_hash,
        'constraints',p_constraints,'key_dates',p_dates)::text,'UTF8')),'hex');
    select * into existing from proposed_delta_impact_derivations where project_id=p_project
        and delta_id=p_delta and rule=p_rule and rule_version=p_version and input_sha256=input_hash;
    if found then
        if existing.derivation_sha256<>derivation_hash then
            raise exception 'same impact inputs produced different consequences' using errcode='23514';
        end if;
        return existing.id;
    end if;
    insert into proposed_delta_impact_derivations(project_id,delta_id,rule,rule_version,
        accepted_revision_id,inputs,input_sha256,evaluated_at,affected_constraint_ids,
        affected_key_dates,derivation_sha256)
    values(p_project,p_delta,p_rule,p_version,revision_id,p_inputs,input_hash,p_evaluated,
        p_constraints,p_dates,derivation_hash) returning id into result;
    return result;
end; $$;
"""

SIGNATURE = "(bigint,bigint,text,text,jsonb,timestamptz,jsonb,jsonb)"


def upgrade(op):
    op.execute(SCHEMA)
    op.execute("revoke all on proposed_delta_impact_derivations from corridor_web,corridor_worker")
    op.execute("grant select on proposed_delta_impact_derivations to corridor_web,corridor_worker")
    op.execute("grant select,insert on proposed_delta_impact_derivations to corridor_source_append")
    op.execute("grant usage,select on sequence proposed_delta_impact_derivations_id_seq to corridor_source_append")
    op.execute("grant select on project_record_revisions to corridor_source_append")
    op.execute("alter table proposed_delta_impact_derivations enable row level security")
    op.execute("create policy p_proposed_delta_impact_derivations_project_partition on proposed_delta_impact_derivations to corridor_web using(project_id=any(current_project_partition()))")
    op.execute("create policy impact_internal on proposed_delta_impact_derivations to corridor_worker,corridor_source_append using(true) with check(true)")
    op.execute(COMMAND)
    op.execute(f"alter function append_delta_impact{SIGNATURE} owner to corridor_source_append")
    op.execute(f"revoke all on function append_delta_impact{SIGNATURE} from public")
    op.execute(f"grant execute on function append_delta_impact{SIGNATURE} to corridor_web,corridor_worker")


def downgrade(op):
    if op.get_bind().scalar(sa.text("select exists(select 1 from proposed_delta_impact_derivations)")):
        raise RuntimeError("retained impact derivations cannot be represented by the predecessor")
    op.execute(f"drop function append_delta_impact{SIGNATURE}")
    op.execute("drop table proposed_delta_impact_derivations")
    op.execute("drop function guard_delta_impact()")
