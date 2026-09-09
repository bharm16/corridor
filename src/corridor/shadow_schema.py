"""Database guards for the separately provisioned shadow environment (#564).

The ordinary schema contains an empty registry. An operator registers a shadow
project only after the separate human baseline bootstrap. Registration prevents
customer membership and release artifacts at the database boundary; runtime
credentials cannot register projects, remove guards, or change frozen receipts.
"""

SCHEMA = """
create table public.shadow_projects (
    project_id bigint primary key references public.projects(id),
    environment text not null,
    customer text not null,
    database_name text not null,
    bootstrap_operator text not null,
    registered_at timestamptz not null default now()
);
create table public.shadow_runs (
    identity varchar(64) primary key,
    project_id bigint not null references public.shadow_projects(project_id),
    payload jsonb not null,
    output_sha256 varchar(64) not null,
    recorded_at timestamptz not null default now()
);
create function public.seal_shadow_run() returns trigger
language plpgsql security definer set search_path = pg_catalog, public as $$
declare
    scope record;
    delivery record;
    item jsonb;
    native jsonb;
    state jsonb;
    native_row record;
    disposition record;
    supersession record;
    deferral record;
    sealed_deltas jsonb := '[]'::jsonb;
    frozen_at timestamptz;
    watermark bigint;
    canonical_payload text;
begin
    perform pg_advisory_xact_lock(hashtextextended('shadow:' || new.project_id::text, 0));
    if jsonb_typeof(new.payload) is distinct from 'object'
       or new.identity !~ '^[0-9a-f]{64}$'
       or new.payload->>'identity' is distinct from new.identity
       or (new.payload->>'project_id')::bigint is distinct from new.project_id
       or coalesce(btrim(new.payload->>'source_configuration'), '') = '' then
        raise exception 'shadow receipt identity or project is invalid' using errcode='23514';
    end if;
    select * into strict scope from public.shadow_projects where project_id=new.project_id;
    if scope.database_name <> current_database()
       or new.payload->>'customer' is distinct from scope.customer
       or new.payload->>'environment' is distinct from scope.environment then
        raise exception 'shadow receipt is outside its registered environment' using errcode='23514';
    end if;
    select sd.* into delivery from public.source_deliveries sd
      where sd.id=(new.payload->>'delivery_id')::bigint and sd.project_id=new.project_id
        and sd.disposition='stored';
    if not found or delivery.customer <> scope.customer
       or delivery.content_sha256 is distinct from new.payload->>'source_sha256'
       or delivery.channel is distinct from new.payload->>'ingress'
       or not exists(select 1 from public.documents d
         where d.id=(new.payload->>'document_id')::bigint and d.project_id=new.project_id
           and d.source_delivery_id=delivery.id) then
        raise exception 'shadow receipt delivery or document scope is invalid' using errcode='23514';
    end if;
    if exists(select 1 from public.shadow_runs r where r.project_id=new.project_id
       and r.payload->>'delivery_id'=delivery.id::text
       and r.payload->>'source_configuration'=new.payload->>'source_configuration') then
        raise exception 'this delivery and shadow configuration are already frozen' using errcode='23514';
    end if;
    if jsonb_typeof(new.payload->'deltas') is distinct from 'array' then
        raise exception 'shadow receipt must name native deltas' using errcode='23514';
    end if;
    if jsonb_array_length(new.payload->'deltas') <> (
         select count(distinct (value->>'id')::bigint) from jsonb_array_elements(new.payload->'deltas'))
       or jsonb_array_length(new.payload->'deltas') <> (
         select count(*) from public.proposed_deltas d join public.delta_groups g
           on g.id=d.group_id and g.project_id=d.project_id
          where d.project_id=new.project_id and g.document_id=(new.payload->>'document_id')::bigint
            and d.source_revision=delivery.content_sha256) then
        raise exception 'shadow receipt omits or duplicates native deltas' using errcode='23514';
    end if;
    if jsonb_typeof(new.payload->'fact_ids') is distinct from 'array'
       or exists(select 1 from jsonb_array_elements_text(new.payload->'fact_ids') as supplied(fact_id)
         where not exists(select 1 from public.facts f where f.id=supplied.fact_id::bigint
           and f.project_id=new.project_id and f.document_id=(new.payload->>'document_id')::bigint)) then
        raise exception 'shadow facts are outside the native document/project' using errcode='23514';
    end if;
    frozen_at := clock_timestamp();
    select coalesce(max(id),0) into watermark from public.source_deliveries;
    for item in select value from jsonb_array_elements(new.payload->'deltas') loop
        select d.* into native_row from public.proposed_deltas d
          join public.delta_groups g on g.id=d.group_id and g.project_id=d.project_id
          where d.id=(item->>'id')::bigint and d.project_id=new.project_id
            and g.document_id=(new.payload->>'document_id')::bigint
            and d.source_revision=delivery.content_sha256;
        if not found then
            raise exception 'shadow delta is outside the native document/project' using errcode='23514';
        end if;
        native := to_jsonb(native_row);
        if item - 'lifecycle' - 'created_at' is distinct from native - 'created_at'
           or (item->>'created_at')::timestamptz is distinct from native_row.created_at then
            raise exception 'shadow delta differs from native values' using errcode='23514';
        end if;
        -- Preserve #518's disposition > supersession > active deferral order.
        state := jsonb_build_object('delta_id',native_row.id,'status','open',
          'disposition',null,'deferred_until',null,'wake_condition',null,'superseded_by_delta_id',null);
        select * into disposition from public.delta_dispositions where delta_id=native_row.id;
        if found then
            state := state || jsonb_build_object('status','resolved','disposition',disposition.disposition);
        else
            select * into supersession from public.delta_supersessions where prior_delta_id=native_row.id;
            if found then
                state := state || jsonb_build_object('status','superseded','superseded_by_delta_id',supersession.superseding_delta_id);
            else
                select * into deferral from public.delta_deferrals where delta_id=native_row.id order by id desc limit 1;
                if found and (deferral.deferred_until is null or deferral.deferred_until > frozen_at) then
                    state := state || jsonb_build_object('status','deferred','deferred_until',deferral.deferred_until,'wake_condition',deferral.wake_condition);
                end if;
            end if;
        end if;
        sealed_deltas := sealed_deltas || jsonb_build_array(native || jsonb_build_object('lifecycle',state));
    end loop;
    new.recorded_at := frozen_at;
    new.payload := new.payload || jsonb_build_object(
      'deltas',sealed_deltas,'frozen_at',frozen_at,'source_delivery_watermark',watermark,
      'ingress_configuration_identity',delivery.configuration_identity,
      'ingress_configuration_version',delivery.configuration_version,
      'canonicalization','postgresql-jsonb-text-v1',
      'groups',(select coalesce(jsonb_agg(to_jsonb(g) order by g.id),'[]'::jsonb)
        from public.delta_groups g where g.project_id=new.project_id
          and g.document_id=(new.payload->>'document_id')::bigint),
      'source_provenance',(select coalesce(jsonb_agg(jsonb_build_object('fact_id',fs.fact_id,
        'role',fs.role,'ordinal',fs.ordinal,'segment',to_jsonb(s)) order by fs.fact_id,fs.role,fs.ordinal),'[]'::jsonb)
        from public.fact_sources fs join public.source_segments s on s.id=fs.source_segment_id
        where fs.project_id=new.project_id and fs.document_id=(new.payload->>'document_id')::bigint));
    -- This database rendering is the ONE canonical definition. Consumers
    -- hash these retained UTF-8 bytes, never recreate a JSON serializer.
    canonical_payload := new.payload::text;
    new.output_sha256 := encode(sha256(convert_to(canonical_payload,'UTF8')),'hex');
    return new;
end $$;
revoke all on function public.seal_shadow_run() from public;
create trigger seal_shadow_run before insert on public.shadow_runs
for each row execute function public.seal_shadow_run();
revoke all on public.shadow_projects, public.shadow_runs from public, corridor_web, corridor_worker;
grant select on public.shadow_projects, public.shadow_runs to corridor_worker;
grant insert on public.shadow_runs to corridor_worker;
create function public.guard_shadow_customer_surface() returns trigger
language plpgsql security definer set search_path = public as $$
begin
    if exists (select 1 from shadow_projects where project_id = new.project_id) then
        raise exception 'shadow project cannot enter customer coordination or release' using errcode = '42501';
    end if;
    return new;
end; $$;
revoke all on function public.guard_shadow_customer_surface() from public;
create trigger shadow_roster before insert or update on public.project_roster_entries
for each row execute function public.guard_shadow_customer_surface();
create trigger shadow_release_request before insert or update on public.release_preparation_requests
for each row execute function public.guard_shadow_customer_surface();
create trigger shadow_release_candidate before insert or update on public.release_candidates
for each row execute function public.guard_shadow_customer_surface();
create trigger shadow_release_package before insert or update on public.release_packages
for each row execute function public.guard_shadow_customer_surface();
create function public.shadow_project_visible(p_id bigint) returns boolean
language sql stable security definer set search_path = public as $$
select not exists (select 1 from shadow_projects where project_id = p_id)
$$;
revoke all on function public.shadow_project_visible(bigint) from public;
grant execute on function public.shadow_project_visible(bigint) to corridor_web;
alter table public.projects enable row level security;
create policy shadow_project_acl on public.projects for all to public
using (true) with check (true);
create policy shadow_projects_hidden on public.projects as restrictive
for select to corridor_web using (public.shadow_project_visible(id));
create function public.preserve_shadow_receipt() returns trigger language plpgsql as $$
begin raise exception 'shadow registry and receipts are immutable' using errcode = '42501'; end; $$;
revoke all on function public.preserve_shadow_receipt() from public;
create trigger immutable_shadow_project before update or delete on public.shadow_projects
for each row execute function public.preserve_shadow_receipt();
create trigger immutable_shadow_run before update or delete on public.shadow_runs
for each row execute function public.preserve_shadow_receipt();
create trigger immutable_shadow_project_truncate before truncate on public.shadow_projects
for each statement execute function public.preserve_shadow_receipt();
create trigger immutable_shadow_run_truncate before truncate on public.shadow_runs
for each statement execute function public.preserve_shadow_receipt();
"""


def install(op):
    """Install through the single migration owner, never from a runtime request."""
    op.execute(SCHEMA)


def uninstall(op):
    """Refuse destructive downgrade while any shadow custody remains."""
    import sqlalchemy as sa
    if op.get_bind().scalar(sa.text("select exists(select 1 from public.shadow_projects)")):
        raise RuntimeError("dispose the complete shadow environment before downgrade")
    for name, table in (("shadow_roster", "project_roster_entries"),
                        ("shadow_release_request", "release_preparation_requests"),
                        ("shadow_release_candidate", "release_candidates"),
                        ("shadow_release_package", "release_packages")):
        op.execute(f"drop trigger {name} on public.{table}")
    op.execute("drop policy shadow_projects_hidden on public.projects")
    op.execute("drop policy shadow_project_acl on public.projects")
    op.execute("alter table public.projects disable row level security")
    op.execute("drop function public.shadow_project_visible(bigint)")
    op.execute("drop function public.guard_shadow_customer_surface()")
    op.execute("drop table public.shadow_runs")
    op.execute("drop function public.seal_shadow_run()")
    op.execute("drop table public.shadow_projects")
    op.execute("drop function public.preserve_shadow_receipt()")
