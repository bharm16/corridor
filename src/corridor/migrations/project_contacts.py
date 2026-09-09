"""Frozen contact import/correction commands in the single supported transition."""

import sqlalchemy as sa


SCHEMA = """
create table project_contact_imports (
    id bigserial primary key, project_id bigint not null references projects(id),
    document_id bigint not null references documents(id), delivery_id bigint references source_deliveries(id),
    customer text not null, source_family text not null, source_revision text not null,
    idempotency_key text not null, content_sha256 varchar(64) not null,
    mapping_json jsonb not null, accounting_json jsonb not null,
    recorded_at timestamptz not null default clock_timestamp(),
    constraint uq_contact_import_scope unique(project_id,id),
    constraint uq_contact_import_key unique(project_id,idempotency_key),
    constraint uq_contact_import_revision unique(project_id,source_family,source_revision)
);
create table project_contacts (
    id bigserial primary key, project_id bigint not null references projects(id),
    import_id bigint not null, source_contact_id text not null,
    organization_id bigint references external_orgs(id), values_json jsonb not null,
    source_locators jsonb not null, unresolved_reason text, corrects_id bigint,
    correction_key text, corrected_by text, correction_reason text,
    recorded_at timestamptz not null default clock_timestamp(),
    foreign key(project_id,import_id) references project_contact_imports(project_id,id),
    constraint uq_project_contact_scope unique(project_id,id),
    foreign key(project_id,corrects_id) references project_contacts(project_id,id),
    constraint uq_project_contact_correction unique(corrects_id),
    constraint uq_project_contact_correction_key unique(project_id,correction_key)
);
create unique index uq_project_contact_import_identity on project_contacts(import_id,source_contact_id)
    where corrects_id is null;
create function guard_project_contacts() returns trigger language plpgsql as $$
begin
    if tg_op <> 'INSERT' or current_user not in ('corridor_source_append','corridor_fact_decision_writer') then
        raise exception 'project contacts require immutable import/correction commands' using errcode='23514';
    end if;
    if tg_table_name = 'project_contacts' then
        if new.unresolved_reason = 'refused_contact_revision' then
            if current_user <> 'corridor_source_append' or new.corrects_id is not null
                or coalesce(new.source_contact_id,'') = ''
                or new.values_json ->> 'source_contact_id' is distinct from new.source_contact_id then
                raise exception 'invalid refused contact occurrence' using errcode='23514';
            end if;
            return new;
        end if;
        if coalesce(new.values_json ->> 'source_contact_id','') = ''
            or coalesce(new.values_json ->> 'organization_ref','') = ''
            or coalesce(new.values_json ->> 'responsible_role','') = ''
            or new.values_json ->> 'source_contact_id' is distinct from new.source_contact_id
            or (new.values_json ->> 'channel') not in ('email','phone')
            or ((new.values_json ->> 'effective_from')::date >= (new.values_json ->> 'effective_until')::date)
            or (new.corrects_id is not null and (current_user <> 'corridor_fact_decision_writer'
                or coalesce(new.corrected_by,'') !~ '^[a-z][a-z0-9._-]+:[^[:space:]]+$'
                or coalesce(new.correction_reason,'') = '')) then
            raise exception 'invalid project contact contract' using errcode='23514';
        end if;
    end if;
    return new;
end; $$;
create trigger project_contact_imports_guard before insert or update or delete on project_contact_imports
    for each row execute function guard_project_contacts();
create trigger project_contacts_guard before insert or update or delete on project_contacts
    for each row execute function guard_project_contacts();
revoke all on function guard_project_contacts() from public;
"""

IMPORT = """
create function append_project_contact_import(p_project bigint, p_document bigint, p_delivery bigint,
    p_customer text, p_family text, p_revision text, p_key text, p_digest text, p_mapping jsonb, p_accounting jsonb)
returns bigint language plpgsql security definer set search_path to 'public' as $$
declare existing project_contact_imports%rowtype; import_id bigint; item jsonb; record jsonb;
begin
    perform 1 from projects where id=p_project for update;
    if not found or not exists(select 1 from documents where id=p_document and project_id=p_project)
      or coalesce(p_key,'')='' or coalesce(p_family,'')='' or coalesce(p_revision,'')=''
      or p_digest !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid contact source binding' using errcode='23514';
    end if;
    if p_delivery is not null then
        if not exists(select 1 from source_deliveries s join documents d on d.id=p_document
            where s.id=p_delivery and s.project_id=p_project and s.customer=p_customer
            and s.disposition='stored' and s.content_sha256=d.sha256) then
            raise exception 'contact envelope is outside its source' using errcode='23514';
        end if;
    elsif not exists(select 1 from project_baseline_sources
        where project_id=p_project and document_id=p_document and customer=p_customer) then
        raise exception 'contact workbook is not the adopted source' using errcode='23514';
    end if;
    select * into existing from project_contact_imports where project_id=p_project
        and (idempotency_key=p_key or (source_family=p_family and source_revision=p_revision));
    if found then
        if existing.content_sha256<>p_digest then
            raise exception 'contact import replay has different content' using errcode='23514';
        end if;
        return existing.id;
    end if;
    if jsonb_typeof(p_accounting -> 'rows') is distinct from 'array' then
        raise exception 'contact import requires row accounting' using errcode='23514';
    end if;
    insert into project_contact_imports(project_id,document_id,delivery_id,customer,source_family,source_revision,
        idempotency_key,content_sha256,mapping_json,accounting_json)
    values(p_project,p_document,p_delivery,p_customer,p_family,p_revision,p_key,p_digest,p_mapping,p_accounting)
    returning id into import_id;
    for item in select value from jsonb_array_elements(p_accounting -> 'rows') loop
        record := coalesce(nullif(item -> 'record','null'::jsonb),nullif(item -> 'refused_record','null'::jsonb));
        if record is not null then
            insert into project_contacts(project_id,import_id,source_contact_id,organization_id,
                values_json,source_locators,unresolved_reason)
            values(p_project,import_id,record ->> 'source_contact_id',(item ->> 'organization_id')::bigint,
                record,item -> 'source_locators',item ->> 'unresolved_reason');
        end if;
    end loop;
    return import_id;
end; $$;
"""

CORRECT = """
create function correct_project_contact(p_project bigint,p_record bigint,p_values jsonb,p_org bigint,
    p_unresolved text,p_principal text,p_reason text,p_key text)
returns bigint language plpgsql security definer set search_path to 'public' as $$
declare previous project_contacts%rowtype; existing project_contacts%rowtype; result bigint; family text;
begin
    perform 1 from projects where id=p_project for update;
    select * into previous from project_contacts where id=p_record and project_id=p_project;
    if not found then raise exception 'contact correction is outside project' using errcode='23514'; end if;
    select * into existing from project_contacts where project_id=p_project and correction_key=p_key;
    if found then
        if existing.corrects_id is distinct from p_record or existing.values_json<>p_values
            or existing.corrected_by<>p_principal or existing.correction_reason<>p_reason then
            raise exception 'contact correction replay differs' using errcode='23514';
        end if;
        return existing.id;
    end if;
    select source_family into family from project_contact_imports where id=previous.import_id;
    if exists(select 1 from project_contacts c join project_contact_imports i on i.id=c.import_id
        where c.project_id=p_project and i.source_family=family and c.source_contact_id=previous.source_contact_id
        and c.id>previous.id) or p_values ->> 'source_contact_id' is distinct from previous.source_contact_id then
        raise exception 'contact correction predecessor is stale or changes identity' using errcode='23514';
    end if;
    if coalesce(p_key,'')='' or coalesce(p_principal,'')='' or coalesce(p_reason,'')='' then
        raise exception 'contact correction needs actor, reason and key' using errcode='23514';
    end if;
    insert into project_contacts(project_id,import_id,source_contact_id,organization_id,values_json,source_locators,
        unresolved_reason,corrects_id,correction_key,corrected_by,correction_reason)
    values(p_project,previous.import_id,previous.source_contact_id,p_org,p_values,
        jsonb_build_object('correction_of',p_record),p_unresolved,p_record,p_key,p_principal,p_reason)
    returning id into result;
    return result;
end; $$;
"""


def upgrade(op):
    op.execute(SCHEMA)
    for table in ("project_contact_imports", "project_contacts"):
        op.execute(f"revoke all on {table} from corridor_web,corridor_worker")
        op.execute(f"grant select on {table} to corridor_web,corridor_worker")
        op.execute(f"grant select,insert on {table} to corridor_source_append,corridor_fact_decision_writer")
        op.execute(f"grant usage,select on sequence {table}_id_seq to corridor_source_append,corridor_fact_decision_writer")
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"create policy p_{table}_project_partition on {table} to corridor_web using(project_id=any(current_project_partition()))")
        op.execute(f"create policy p_{table}_internal on {table} to corridor_worker,corridor_source_append,corridor_fact_decision_writer using(true) with check(true)")
    op.execute("grant select on project_baseline_sources,source_deliveries to corridor_source_append")
    op.execute("grant select,update on projects to corridor_source_append,corridor_fact_decision_writer")
    for body, name, signature, owner, roles in (
        (IMPORT, "append_project_contact_import", "(bigint,bigint,bigint,text,text,text,text,text,jsonb,jsonb)", "corridor_source_append", "corridor_web,corridor_worker"),
        (CORRECT, "correct_project_contact", "(bigint,bigint,jsonb,bigint,text,text,text,text)", "corridor_fact_decision_writer", "corridor_web"),
    ):
        op.execute(body)
        op.execute(f"alter function {name}{signature} owner to {owner}")
        op.execute(f"revoke all on function {name}{signature} from public")
        op.execute(f"grant execute on function {name}{signature} to {roles}")


def downgrade(op):
    if op.get_bind().scalar(sa.text("select exists(select 1 from project_contact_imports)")):
        raise RuntimeError("contact import history cannot be represented by the predecessor")
    op.execute("drop function correct_project_contact(bigint,bigint,jsonb,bigint,text,text,text,text)")
    op.execute("drop function append_project_contact_import(bigint,bigint,bigint,text,text,text,text,text,jsonb,jsonb)")
    op.execute("drop table project_contacts,project_contact_imports")
    op.execute("drop function guard_project_contacts()")
