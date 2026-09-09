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
    op.execute("drop table public.shadow_projects")
    op.execute("drop function public.preserve_shadow_receipt()")
