"""Baseline/delta operating mode (#520, ADR-0076, ADR-0083, ADR-0084).

The mode is not a column anybody can set.  It is derived from one immutable
baseline-adoption receipt: a project with a receipt is in `adopted_baseline`
mode, a project without one is `legacy`.  The receipt is written once, by one
command the record-decision role owns, and can never be updated, deleted, or
truncated, so the transition is one-way by construction rather than by
convention.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


OPERATING_MODE_ROLE = "corridor_fact_decision_writer"

# The legacy tables whose rows are accepted values: the Constraint Record that
# `run_dependency_admission` and the schedule update paths write, and the
# statements `run_event_admission` admits.  Work List scheduling, deferral, and
# packet presentation are deliberately absent: they are ADR-0035 human work,
# not accepted authority, and #524 governs them (ADR-0084).
OPERATING_MODE_GUARDED_TABLES = ("dependencies", "dependency_events")

BASELINE_ADOPTION_SCHEMA = """
create table public.project_baseline_adoptions (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    -- The revision Adopt Baseline writes (#509).  Nullable so this transition
    -- exists before the importer does and a test fixture can reach adopted
    -- mode without it; #509 passes the revision it created in the same
    -- transaction.
    revision_id bigint references public.project_record_revisions (id),
    adopted_by_principal character varying(128) not null,
    baseline_source_sha256 character varying(64) not null,
    importer_identity character varying(128) not null,
    importer_version character varying(64) not null,
    idempotency_key character varying(160) not null,
    adopted_at timestamp with time zone not null default now(),
    constraint uq_project_baseline_adoptions_project unique (project_id),
    constraint ck_project_baseline_adoptions_digest check (
        baseline_source_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_project_baseline_adoptions_principal check (
        length(btrim(adopted_by_principal)) > 0
    )
);
"""

BASELINE_ADOPTION_SCHEMA_DOWN = """
drop table if exists public.project_baseline_adoptions cascade;
"""

OPERATING_MODE_FUNCTIONS = """
create function public.project_operating_mode(p_project_id bigint) returns text
    language sql stable
    set search_path to 'public'
    as $$
        select case
            when exists (
                select 1 from public.project_baseline_adoptions
                 where project_id = p_project_id
            ) then 'adopted_baseline'
            else 'legacy'
        end;
    $$;

create function public.enforce_project_baseline_adoption_write() returns trigger
    language plpgsql
    as $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'Baseline adoption is immutable: the operating mode moves from legacy to adopted baseline once and never back'
                    using errcode='23514';
            end if;
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Baseline adoption requires the typed adoption command'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_project_baseline_adoptions_write
    before insert or update or delete on public.project_baseline_adoptions
    for each row execute function public.enforce_project_baseline_adoption_write();

create trigger trg_project_baseline_adoptions_truncate
    before truncate on public.project_baseline_adoptions
    for each statement execute function public.enforce_project_baseline_adoption_write();

create function public.refuse_legacy_accepted_write_for_adopted_project()
    returns trigger
    language plpgsql security definer
    set search_path to 'public'
    as $$
        begin
            if public.project_operating_mode(new.project_id) = 'adopted_baseline' then
                raise exception 'project % has an adopted baseline: the legacy % path may capture Source Facts and propose deltas, but may not write an accepted value',
                        new.project_id, tg_table_name
                    using errcode='23514';
            end if;
            return new;
        end; $$;
"""

OPERATING_MODE_FUNCTIONS_DOWN = """
drop function if exists public.refuse_legacy_accepted_write_for_adopted_project();
drop function if exists public.enforce_project_baseline_adoption_write() cascade;
drop function if exists public.project_operating_mode(bigint);
"""

ADOPT_PROJECT_BASELINE = """
create function public.adopt_project_baseline(
    p_project_id bigint,
    p_adopted_by_principal character varying,
    p_baseline_source_sha256 character varying,
    p_importer_identity character varying,
    p_importer_version character varying,
    p_idempotency_key character varying,
    p_revision_id bigint
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing project_baseline_adoptions%ROWTYPE;
            new_id bigint;
        begin
            if p_revision_id is not null and not exists (
                select 1 from project_record_revisions
                 where id = p_revision_id and project_id = p_project_id
            ) then
                raise exception 'the adoption revision belongs to another project'
                    using errcode='23514';
            end if;
            select * into existing from project_baseline_adoptions
             where project_id = p_project_id;
            if found then
                if existing.idempotency_key is distinct from p_idempotency_key
                   or existing.baseline_source_sha256
                       is distinct from p_baseline_source_sha256 then
                    raise exception 'project % already has an adopted baseline; the operating mode transition happens once', p_project_id
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'adoption_id', existing.id,
                    'project_id', p_project_id,
                    'mode', 'adopted_baseline',
                    'created', false
                );
            end if;
            insert into project_baseline_adoptions (
                project_id, revision_id, adopted_by_principal,
                baseline_source_sha256, importer_identity, importer_version,
                idempotency_key
            ) values (
                p_project_id, p_revision_id, p_adopted_by_principal,
                p_baseline_source_sha256, p_importer_identity, p_importer_version,
                p_idempotency_key
            ) returning id into new_id;
            return jsonb_build_object(
                'adoption_id', new_id,
                'project_id', p_project_id,
                'mode', 'adopted_baseline',
                'created', true
            );
        end; $$;
"""

ADOPT_PROJECT_BASELINE_SIGNATURE = (
    "(bigint, character varying, character varying, character varying, "
    "character varying, character varying, bigint)"
)

# The one legacy accepted-value writer that really is a SQL function.  It is
# re-issued here with the mode check as its first act, so the refusal lives in
# the command itself and not only in the Python that calls it; the downgrade
# re-issues the released body unchanged.
STRUCTURED_CELL_INCLUSION_RELEASED = """\
CREATE OR REPLACE FUNCTION public.include_structured_cell_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_idempotency_key character varying, p_policy character varying) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
        declare
            existing_revision bigint;
            existing_fact bigint;
            current_decision bigint;
            current_fact bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_decision bigint;
            eligible boolean;
        begin
            if p_policy <> 'structured-cell-record-inclusion-v1'
               or p_fact_type not in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', 'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', 'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', 'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', 'offset_side', 'potential_conflict', 'data_source', 'marked_resolution', 'committed_date', 'action_due_date', 'need_date', 'applies_to', 'closure_result') then
                raise exception 'unrecognized structured-cell inclusion policy'
                    using errcode='23514';
            end if;
            select exists (
                select 1 from facts
                join active_extraction_runs
                  on active_extraction_runs.document_id = facts.document_id
                 and active_extraction_runs.extraction_run_id = facts.extraction_run_id
                join extracted_proposal_facts
                  on extracted_proposal_facts.fact_id = facts.id
                join extracted_proposals
                  on extracted_proposals.id = extracted_proposal_facts.proposal_id
                join candidates on candidates.id = extracted_proposals.candidate_id
                where facts.id = p_fact_id
                  and facts.project_id = p_project_id
                  and facts.subject_key = p_subject_key
                  and facts.fact_type = p_fact_type
                  and candidates.state in ('accepted', 'merged')
                  and candidates.merged_into is not null
                  and (
                      facts.fact_type <> 'external_org'
                      or facts.external_org_value_id is not null
                  )
                  and (
                      facts.fact_type <> 'applies_to'
                      or exists (
                          select 1 from fact_applies_to
                          where fact_applies_to.fact_id = facts.id
                      )
                  )
                  and (
                      facts.fact_type <> 'closure_result'
                      or (
                          exists (
                              select 1 from fact_closure_results
                              where fact_closure_results.fact_id = facts.id
                                and fact_closure_results.closure_kind =
                                    'source_marked_resolved'
                          )
                          and exists (
                              select 1 from fact_closure_sources
                              where fact_closure_sources.fact_id = facts.id
                          )
                      )
                  )
                  and exists (
                      select 1 from fact_sources
                      join source_segments
                        on source_segments.id = fact_sources.source_segment_id
                      where fact_sources.fact_id = facts.id
                        and fact_sources.role = 'value_source'
                        and source_segments.kind = 'spreadsheet_cell'
                  )
                  and not exists (
                      select 1 from fact_sources
                      join source_segments
                        on source_segments.id = fact_sources.source_segment_id
                      where fact_sources.fact_id = facts.id
                        and fact_sources.role = 'value_source'
                        and source_segments.kind <> 'spreadsheet_cell'
                  )
            ) into eligible;
            if not eligible then
                raise exception 'Fact is not eligible for released structured-cell inclusion'
                    using errcode='23514';
            end if;
            select id into existing_revision from project_record_revisions
             where project_id = p_project_id and idempotency_key = p_idempotency_key;
            if existing_revision is not null then
                select id, fact_id into current_decision, existing_fact from fact_decisions
                 where revision_id = existing_revision;
                if current_decision is null or existing_fact <> p_fact_id then
                    raise exception 'Record Inclusion key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', current_decision,
                    'created', false
                );
            end if;
            select id, fact_id into current_decision, current_fact from fact_decisions
             where project_id = p_project_id and subject_key = p_subject_key
               and fact_type = p_fact_type and superseded_by is null;
            if current_decision is not null and current_fact = p_fact_id then
                select revision_id into existing_revision from fact_decisions
                 where id = current_decision;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', current_decision,
                    'created', false
                );
            end if;
            select max(id) into predecessor_revision from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, 'include_structured_cell_fact',
                null, p_policy, p_idempotency_key
            ) returning id into new_revision;
            new_decision := nextval('fact_decisions_id_seq');
            if current_decision is not null then
                update fact_decisions set superseded_by = new_decision
                 where id = current_decision;
            end if;
            insert into fact_decisions (
                id, project_id, fact_id, subject_key, fact_type, revision_id, superseded_by
            ) values (
                new_decision, p_project_id, p_fact_id, p_subject_key,
                p_fact_type, new_revision, null
            );
            return jsonb_build_object(
                'revision_id', new_revision,
                'decision_id', new_decision,
                'created', true
            );
        end; $$;
"""

STRUCTURED_CELL_INCLUSION_MODE_CHECK = """\
            if project_operating_mode(p_project_id) = 'adopted_baseline' then
                raise exception 'project % has an adopted baseline: structured-cell inclusion captures Source Facts and proposes deltas, and may not replace an accepted value', p_project_id
                    using errcode='23514';
            end if;
"""

STRUCTURED_CELL_INCLUSION_MODE_GUARDED = (
    STRUCTURED_CELL_INCLUSION_RELEASED.replace(
        "\n        begin\n",
        "\n        begin\n" + STRUCTURED_CELL_INCLUSION_MODE_CHECK,
        1,
    )
)


def upgrade(op) -> None:
    op.execute(BASELINE_ADOPTION_SCHEMA)
    op.execute(OPERATING_MODE_FUNCTIONS)
    op.execute(ADOPT_PROJECT_BASELINE)
    op.execute(STRUCTURED_CELL_INCLUSION_MODE_GUARDED)
    for table in OPERATING_MODE_GUARDED_TABLES:
        op.execute(
            f"create trigger trg_{table}_operating_mode "
            f"before insert or update on public.{table} "
            "for each row execute function "
            "public.refuse_legacy_accepted_write_for_adopted_project()"
        )
    # The mode is readable by the application; only the record-decision role's
    # command can establish it.  A new table arrives carrying the schema's
    # default privileges, so the write half is taken back explicitly.
    op.execute(
        f"grant select on public.project_baseline_adoptions to {RUNTIME_LOGINS}"
    )
    op.execute(
        "revoke insert, update, delete, truncate "
        f"on public.project_baseline_adoptions from {RUNTIME_LOGINS}"
    )
    op.execute(f"grant usage on schema public to {OPERATING_MODE_ROLE}")
    op.execute(
        "grant select, insert on public.project_baseline_adoptions "
        f"to {OPERATING_MODE_ROLE}"
    )
    op.execute(
        "grant usage, select on sequence public.project_baseline_adoptions_id_seq "
        f"to {OPERATING_MODE_ROLE}"
    )
    op.execute(
        f"alter function public.adopt_project_baseline"
        f"{ADOPT_PROJECT_BASELINE_SIGNATURE} owner to {OPERATING_MODE_ROLE}"
    )
    for function in (
        f"adopt_project_baseline{ADOPT_PROJECT_BASELINE_SIGNATURE}",
        "refuse_legacy_accepted_write_for_adopted_project()",
    ):
        op.execute(f"revoke all on function public.{function} from public")
    # Adopt Baseline is a bulk human command, so it joins the other human
    # decision commands on the web capability alone (#509, ADR-0076).
    op.execute(
        f"grant execute on function public.adopt_project_baseline"
        f"{ADOPT_PROJECT_BASELINE_SIGNATURE} to corridor_web"
    )


def downgrade(op) -> None:
    for table in OPERATING_MODE_GUARDED_TABLES:
        op.execute(f"drop trigger if exists trg_{table}_operating_mode on public.{table}")
    op.execute(
        f"drop function if exists public.adopt_project_baseline"
        f"{ADOPT_PROJECT_BASELINE_SIGNATURE}"
    )
    op.execute(STRUCTURED_CELL_INCLUSION_RELEASED)
    op.execute(OPERATING_MODE_FUNCTIONS_DOWN)
    op.execute(BASELINE_ADOPTION_SCHEMA_DOWN)
