"""Immutable compatibility-history custody, folded into the existing edge.

The command copies database rows itself in one statement-level MVCC snapshot.
Callers supply a reviewed digest, never historical authors or accepted values.
Reversal appends a receipt and leaves the original bytes and source links intact.
"""

import json

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor.legacy_history_inventory import HISTORY_CLASSES


SCHEMA = """
create table legacy_history_batches (
 id bigserial primary key,
 project_id bigint not null references projects(id),
 run_key text not null check(length(btrim(run_key))>0),
 executor text not null check(length(btrim(executor))>0),
 code_revision text not null check(length(btrim(code_revision))>0),
 inventory_version text not null,
 content_sha256 text not null check(content_sha256 ~ '^[0-9a-f]{64}$'),
 payload jsonb not null check(jsonb_typeof(payload)='object'),
 counts jsonb not null check(jsonb_typeof(counts)='object'),
 captured_at timestamptz not null default clock_timestamp(),
 unique(project_id,run_key)
);
create table legacy_history_reversals (
 id bigserial primary key,
 batch_id bigint not null unique references legacy_history_batches(id),
 project_id bigint not null references projects(id),
 reversed_by text not null check(length(btrim(reversed_by))>0),
 reason text not null check(length(btrim(reason))>0),
 reversed_at timestamptz not null default transaction_timestamp()
);
create table legacy_history_evidence_migrations (
 id bigserial primary key,
 batch_id bigint not null references legacy_history_batches(id),
 project_id bigint not null references projects(id),
 legacy_evidence_link_id bigint not null,
 evidence_link_source_id bigint references evidence_link_sources(id),
 source_segment_id bigint references source_segments(id),
 original_quote_sha256 text not null check(original_quote_sha256 ~ '^[0-9a-f]{64}$'),
 outcome text not null check(outcome in ('segment_reference','already_native','retained_quote')),
 reason text not null,
 created_at timestamptz not null default transaction_timestamp(),
 unique(batch_id,legacy_evidence_link_id)
);
create function guard_legacy_history() returns trigger language plpgsql as $$
begin
 if tg_op <> 'INSERT' or current_user <> 'corridor_fact_decision_writer' then
  raise exception 'legacy history is immutable and command-owned' using errcode='23514';
 end if;
 return new;
end; $$;
create trigger guard_legacy_history_batches before insert or update or delete on legacy_history_batches
 for each row execute function guard_legacy_history();
create trigger guard_legacy_history_evidence before insert or update or delete on legacy_history_evidence_migrations
 for each row execute function guard_legacy_history();
create trigger guard_legacy_history_reversals before insert or update or delete on legacy_history_reversals
 for each row execute function guard_legacy_history();
"""


CAPTURE = """
create function capture_legacy_history(p_project_id bigint, p_run_key text,
 p_executor text, p_code_revision text, p_expected_digest text) returns bigint
language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
declare content jsonb; content_digest text; result bigint; previous legacy_history_batches;
begin
 if p_executor is distinct from session_user then
  raise exception 'migration executor must equal authenticated database login' using errcode='23514';
 end if;
 if session_user='corridor_web' and not coalesce(p_project_id=any(current_project_partition()),false) then
  raise exception 'history outside project partition' using errcode='23514';
 end if;
 perform id from projects where id=p_project_id for update;
 if not found then raise exception 'project does not exist' using errcode='23514'; end if;
 select * into previous from legacy_history_batches where project_id=p_project_id and run_key=p_run_key;
 if found then
  if previous.content_sha256 is distinct from p_expected_digest
   or previous.executor is distinct from p_executor or previous.code_revision is distinct from p_code_revision then
   raise exception 'history run key is bound to different content or provenance' using errcode='23514';
  end if;
  if exists(select 1 from legacy_history_reversals where batch_id=previous.id) then
   raise exception 'reversed history batch cannot be silently reactivated' using errcode='23514';
  end if;
  return previous.id;
 end if;
 content:=legacy_history_content(p_project_id);
 content_digest:=encode(sha256(convert_to(content::text,'UTF8')),'hex');
 if content_digest is distinct from p_expected_digest then
  raise exception 'legacy history changed since inventory' using errcode='23514';
 end if;
 insert into legacy_history_batches(project_id,run_key,executor,code_revision,inventory_version,content_sha256,payload,counts)
 values(p_project_id,p_run_key,p_executor,p_code_revision,'legacy-history-v1',content_digest,content,
  (select jsonb_object_agg(key,jsonb_array_length(value->'rows')) from jsonb_each(content->'classes')))
 returning id into result;
 return result;
end; $$;
create function reverse_legacy_history(p_project_id bigint,p_batch_id bigint,p_actor text,p_reason text)
 returns bigint language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
declare result bigint; previous legacy_history_reversals;
begin
 if p_actor is distinct from session_user then
  raise exception 'migration reversal actor must equal authenticated database login' using errcode='23514';
 end if;
 if session_user='corridor_web' and not coalesce(p_project_id=any(current_project_partition()),false) then
  raise exception 'history outside project partition' using errcode='23514';
 end if;
 perform id from projects where id=p_project_id for update;
 if not exists(select 1 from legacy_history_batches where id=p_batch_id and project_id=p_project_id) then
  raise exception 'history batch is outside project' using errcode='23514';
 end if;
 select * into previous from legacy_history_reversals where batch_id=p_batch_id;
 if found then
  if previous.reversed_by is distinct from p_actor or previous.reason is distinct from p_reason then
   raise exception 'history reversal is bound to another decision' using errcode='23514';
  end if;
  return previous.id;
 end if;
 insert into legacy_history_reversals(batch_id,project_id,reversed_by,reason)
 values(p_batch_id,p_project_id,p_actor,p_reason) returning id into result;
 return result;
end; $$;
"""


def _ensure_operations_role(op):
    """Converge on the exact winner of a cluster-global CREATE ROLE race."""
    connection = op.get_bind()
    exists = text("select exists(select 1 from pg_roles where rolname='corridor_history_operations')")
    if not connection.scalar(exists):
        try:
            with connection.begin_nested():
                connection.exec_driver_sql(
                    "create role corridor_history_operations nologin noinherit "
                    "nosuperuser nocreatedb nocreaterole nobypassrls noreplication"
                )
        except DBAPIError as exc:
            original = exc.orig
            code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
            raced = code == "42710" or (code == "23505" and constraint == "pg_authid_rolname_index")
            if not raced or not connection.scalar(exists):
                raise
    if connection.scalar(text("""
        select exists(select 1 from pg_roles where rolname='corridor_history_operations'
         and (rolsuper or rolcreaterole or rolcreatedb or rolcanlogin or rolbypassrls or rolreplication))
    """)):
        raise RuntimeError("history operations capability must be a non-login least-privilege role")


def upgrade(op):
    _ensure_operations_role(op)
    # PostgreSQL may check this function's EXECUTE privilege while planning
    # the guard even when session_user is not the web login. The command
    # owner needs the read-only sealed-scope reader, never the scope opener.
    op.execute("grant execute on function current_project_partition() to corridor_fact_decision_writer")
    # All table names and predicates are trusted, versioned program constants.
    # JSONB text supplies one database-native canonical digest on every replay.
    clauses = []
    for entry in HISTORY_CLASSES:
        declaration = json.dumps({"treatment": entry.treatment, "expiry": entry.expiry})
        clauses.append(
            f"select '{entry.table}' as name, '{declaration}'::jsonb || "
            f"jsonb_build_object('rows',coalesce(jsonb_agg(to_jsonb(h) order by to_jsonb(h)::text),'[]'::jsonb)) as value "
            f"from {entry.table} h where {entry.project_predicate}"
        )
    op.execute(SCHEMA)
    op.execute("""
      create function legacy_history_content(p_project_id bigint) returns jsonb
      language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
      declare result jsonb;
      begin
       if session_user='corridor_web' and not coalesce(p_project_id=any(current_project_partition()),false) then
  raise exception 'history outside project partition' using errcode='23514';
 end if;
       if not exists(select 1 from projects where id=p_project_id) then
        raise exception 'project does not exist' using errcode='23514';
       end if;
       select jsonb_build_object('version','legacy-history-v1','project_id',p_project_id,
        'classes',jsonb_object_agg(name,value)) into result from (
    """ + " union all ".join(clauses) + ") inventory; return result; end; $$;")
    op.execute(CAPTURE)
    op.execute(EVIDENCE_BACKFILL)
    # FOR UPDATE needs UPDATE on at least one column. Grant only identity-
    # column locking capability to the non-login command owner; quote bytes
    # remain outside its UPDATE privileges and runtime logins gain nothing.
    op.execute("grant update(id) on evidence_links to corridor_fact_decision_writer")
    op.execute("grant insert on evidence_link_sources to corridor_fact_decision_writer")
    op.execute("grant usage,select on sequence evidence_link_sources_id_seq to corridor_fact_decision_writer")
    for entry in HISTORY_CLASSES:
        op.execute(f"grant select on {entry.table} to corridor_fact_decision_writer")
    for table in ("legacy_history_batches", "legacy_history_reversals", "legacy_history_evidence_migrations"):
        op.execute(f"revoke all on {table} from public,corridor_web,corridor_worker")
        op.execute(f"grant select on {table} to corridor_web,corridor_worker,corridor_history_operations")
        op.execute(f"grant select,insert on {table} to corridor_fact_decision_writer")
        op.execute(f"grant usage,select on sequence {table}_id_seq to corridor_fact_decision_writer")
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"create policy p_{table}_project_partition on {table} to corridor_web using(project_id=any(current_project_partition()))")
        op.execute(f"create policy p_{table}_internal on {table} to corridor_worker,corridor_fact_decision_writer,corridor_history_operations using(true) with check(true)")
    for name, signature in (
        ("legacy_history_content", "(bigint)"),
        ("capture_legacy_history", "(bigint,text,text,text,text)"),
        ("reverse_legacy_history", "(bigint,bigint,text,text)"),
        ("backfill_legacy_evidence_sources", "(bigint,bigint)"),
    ):
        op.execute(f"alter function {name}{signature} owner to corridor_fact_decision_writer")
        op.execute(f"revoke all on function {name}{signature} from public")
        # Migration execution is deliberately absent from runtime logins. The
        # schema/migration owner retains EXECUTE through its role membership.
        op.execute(f"grant execute on function {name}{signature} to corridor_history_operations")
        if name == "legacy_history_content":
            op.execute(f"grant execute on function {name}{signature} to corridor_web,corridor_worker")
    op.execute("revoke all on function guard_legacy_history() from public")


EVIDENCE_BACKFILL = """
create function backfill_legacy_evidence_sources(p_project_id bigint,p_batch_id bigint)
 returns bigint language plpgsql security definer set search_path=public,pg_temp set timezone='UTC' as $$
declare batch legacy_history_batches; item jsonb; link evidence_links;
 segment_id bigint; matches bigint; citation_id bigint; migrated bigint:=0; outcome text; reason text;
begin
 if session_user='corridor_web' and not coalesce(p_project_id=any(current_project_partition()),false) then
  raise exception 'evidence migration outside project partition' using errcode='23514';
 end if;
 perform id from projects where id=p_project_id for update;
 select * into batch from legacy_history_batches where id=p_batch_id and project_id=p_project_id;
 if not found or exists(select 1 from legacy_history_reversals where batch_id=p_batch_id) then
  raise exception 'history batch is missing, reversed or outside project' using errcode='23514';
 end if;
 for item in select value from jsonb_array_elements(batch.payload->'classes'->'evidence_links'->'rows') loop
  if exists(select 1 from legacy_history_evidence_migrations where batch_id=p_batch_id and legacy_evidence_link_id=(item->>'id')::bigint) then
   continue;
  end if;
  select * into link from evidence_links where id=(item->>'id')::bigint for update;
  if not found or to_jsonb(link) is distinct from item then
   raise exception 'legacy evidence changed since reviewed history batch' using errcode='23514';
  end if;
  segment_id:=null; citation_id:=null;
  if exists(select 1 from evidence_link_sources s where s.evidence_link_id=link.id
    and (not exists(select 1 from legacy_history_evidence_migrations m where m.evidence_link_source_id=s.id)
      or exists(select 1 from legacy_history_evidence_migrations m where m.evidence_link_source_id=s.id
        and not exists(select 1 from legacy_history_reversals r where r.batch_id=m.batch_id)))) then
   outcome:='already_native'; reason:='Existing Source Segment citations remain unchanged.';
  else
   select count(*),min(id) into matches,segment_id from source_segments
    where project_id=p_project_id and document_id=link.document_id and page_no=link.page_no
     and kind in ('prose_span','pdf_span') and exact_text=link.quote
     and content_sha256=encode(sha256(convert_to(exact_text,'UTF8')),'hex');
   if matches=1 then
    select id into citation_id from evidence_link_sources where evidence_link_id=link.id and source_segment_id=segment_id;
    if citation_id is null then
     insert into evidence_link_sources(project_id,document_id,evidence_link_id,source_segment_id,ordinal)
     values(p_project_id,link.document_id,link.id,segment_id,
      (select coalesce(max(ordinal),0)+1 from evidence_link_sources where evidence_link_id=link.id))
     returning id into citation_id;
    end if;
    outcome:='segment_reference'; reason:='Exactly one whole source passage matches document, page, words and digest; original quote retained.';
    migrated:=migrated+1;
   else
    segment_id:=null; outcome:='retained_quote';
    reason:=case when matches=0 then 'No exact located passage; retain original quote.' else 'Multiple exact passages; source occurrence is ambiguous.' end;
   end if;
  end if;
  insert into legacy_history_evidence_migrations(batch_id,project_id,legacy_evidence_link_id,
   evidence_link_source_id,source_segment_id,original_quote_sha256,outcome,reason)
  values(p_batch_id,p_project_id,link.id,citation_id,segment_id,
   encode(sha256(convert_to(link.quote,'UTF8')),'hex'),outcome,reason);
 end loop;
 return migrated;
end; $$;
"""


def downgrade(op):
    """Reverse only an unused installation; retained history is never dropped."""
    connection = op.get_bind()
    for table in ("legacy_history_batches", "legacy_history_reversals", "legacy_history_evidence_migrations"):
        if connection.scalar(text(f"select exists(select 1 from {table})")):
            raise RuntimeError("legacy history custody and migration receipts cannot be discarded by downgrade")
    for function in (
        "backfill_legacy_evidence_sources(bigint,bigint)",
        "reverse_legacy_history(bigint,bigint,text,text)",
        "capture_legacy_history(bigint,text,text,text,text)",
        "legacy_history_content(bigint)",
    ):
        op.execute(f"drop function {function}")
    for table in ("legacy_history_evidence_migrations", "legacy_history_reversals", "legacy_history_batches"):
        op.execute(f"drop table {table}")
    op.execute("drop function guard_legacy_history()")
    op.execute("revoke update(id) on evidence_links from corridor_fact_decision_writer")
    op.execute("revoke execute on function current_project_partition() from corridor_fact_decision_writer")
    # The cluster-global NOLOGIN capability may serve another customer DB.
    # Its per-database privileges disappear with these objects; never drop it.
