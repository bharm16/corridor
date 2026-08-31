"""Add typed Fact decisions and Project Record revisions.

Revision ID: 20c7d970be63
Revises: 437e8c9a0b1d
"""

from alembic import op


revision = "20c7d970be63"
down_revision = "437e8c9a0b1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the first guarded Record Inclusion authority spine."""

    op.execute(
        """
        do $$ begin
            if not exists (select 1 from pg_roles where rolname = 'corridor_fact_decision_writer') then
                create role corridor_fact_decision_writer nologin noinherit;
            end if;
        end $$;
        create table project_record_revisions (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            predecessor_revision_id bigint references project_record_revisions(id),
            command_type varchar(64) not null,
            human_principal varchar(128),
            released_policy varchar(128),
            idempotency_key varchar(160) not null,
            recorded_at timestamptz not null default now(),
            constraint uq_project_record_revision_key unique (project_id, idempotency_key),
            constraint ck_project_record_revision_authority_xor
                check ((human_principal is null) <> (released_policy is null))
        );
        create index ix_project_record_revisions_project_id
            on project_record_revisions(project_id);

        create table fact_decisions (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            fact_id bigint not null unique references facts(id),
            subject_key text not null,
            fact_type varchar(64) not null,
            revision_id bigint not null references project_record_revisions(id),
            superseded_by bigint references fact_decisions(id) deferrable initially deferred,
            decided_at timestamptz not null default now()
        );
        create index ix_fact_decisions_project_id on fact_decisions(project_id);
        create unique index uq_fact_decision_effective
            on fact_decisions(project_id, subject_key, fact_type)
            where superseded_by is null
              and fact_type in ('station_from', 'station_to');

        create function enforce_project_record_revision_write() returns trigger
        language plpgsql as $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'Project Record revisions are append-only' using errcode='23514';
            end if;
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Project Record revision requires the typed decision command'
                    using errcode='23514';
            end if;
            return new;
        end; $$;
        create trigger trg_project_record_revisions_guard
            before insert or update or delete on project_record_revisions
            for each row execute function enforce_project_record_revision_write();

        create function enforce_fact_decision_write() returns trigger language plpgsql as $$
        declare valid_binding boolean;
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Fact decision requires the typed decision command'
                    using errcode='23514';
            end if;
            if tg_op = 'DELETE' then
                raise exception 'Fact decisions are append-preserving' using errcode='23514';
            end if;
            if tg_op = 'UPDATE' then
                if new.id is distinct from old.id
                   or new.project_id is distinct from old.project_id
                   or new.fact_id is distinct from old.fact_id
                   or new.subject_key is distinct from old.subject_key
                   or new.fact_type is distinct from old.fact_type
                   or new.revision_id is distinct from old.revision_id
                   or new.decided_at is distinct from old.decided_at
                   or old.superseded_by is not null
                   or new.superseded_by is null then
                    raise exception 'Fact decision may only become superseded once'
                        using errcode='23514';
                end if;
                return new;
            end if;
            select exists (
                select 1 from facts
                join project_record_revisions on project_record_revisions.id = new.revision_id
                where facts.id = new.fact_id
                  and facts.project_id = new.project_id
                  and facts.subject_key = new.subject_key
                  and facts.fact_type = new.fact_type
                  and project_record_revisions.project_id = new.project_id
                  and project_record_revisions.released_policy is not null
                  and project_record_revisions.human_principal is null
            ) into valid_binding;
            if not valid_binding then
                raise exception 'Fact decision binding is invalid' using errcode='23514';
            end if;
            return new;
        end; $$;
        create trigger trg_fact_decisions_guard
            before insert or update or delete on fact_decisions
            for each row execute function enforce_fact_decision_write();

        create function include_stationing_fact_decision(
            p_project_id bigint,
            p_fact_id bigint,
            p_subject_key text,
            p_fact_type varchar,
            p_idempotency_key varchar,
            p_policy varchar
        ) returns jsonb language plpgsql security definer set search_path = public as $$
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
            if p_policy <> 'stationing-record-inclusion-v1'
               or p_fact_type not in ('station_from', 'station_to') then
                raise exception 'unrecognized Stationing inclusion policy' using errcode='23514';
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
                raise exception 'Fact is not eligible for released Stationing inclusion'
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
                p_project_id, predecessor_revision, 'include_stationing_fact',
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

        alter table project_record_revisions owner to corridor_fact_decision_writer;
        alter sequence project_record_revisions_id_seq owner to corridor_fact_decision_writer;
        alter table fact_decisions owner to corridor_fact_decision_writer;
        alter sequence fact_decisions_id_seq owner to corridor_fact_decision_writer;
        alter function include_stationing_fact_decision(bigint,bigint,text,varchar,varchar,varchar)
            owner to corridor_fact_decision_writer;
        revoke insert, update, delete, truncate on project_record_revisions from public;
        revoke insert, update, delete, truncate on fact_decisions from public;
        grant select on project_record_revisions, fact_decisions to public;
        grant select on facts, active_extraction_runs, extracted_proposal_facts,
            extracted_proposals, candidates, fact_sources, source_segments
            to corridor_fact_decision_writer;
        grant execute on function include_stationing_fact_decision(
            bigint,bigint,text,varchar,varchar,varchar
        ) to public;
        """
    )


def downgrade() -> None:
    """Project Record decision history has no destructive migration path."""

    raise RuntimeError("Fact decision migration downgrade is unsupported")
