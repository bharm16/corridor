"""Add immutable proposals and Fact correction dispositions.

Revision ID: 961bd259310f
Revises: 1d2e3f4a5b6c
"""

from alembic import op


revision = "961bd259310f"
down_revision = "1d2e3f4a5b6c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create immutable Fact-reference proposal and correction history."""

    op.execute(
        """
        alter table facts drop constraint uq_facts_run_type_subject;
        alter table facts add constraint uq_facts_run_scope_id
            unique (project_id, document_id, extraction_run_id, id);
        create table extraction_run_candidates (
            id bigserial primary key,
            extraction_run_id bigint not null references extraction_runs(id),
            candidate_id bigint not null unique references candidates(id),
            constraint uq_extraction_run_candidate
                unique (extraction_run_id, candidate_id)
        );
        create index ix_extraction_run_candidates_extraction_run_id
            on extraction_run_candidates(extraction_run_id);
        create table extracted_proposals (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            document_id bigint not null references documents(id),
            extraction_run_id bigint not null,
            candidate_id bigint not null unique references candidates(id),
            kind varchar(32) not null,
            subject_key text not null,
            candidate_metadata_json jsonb not null,
            created_at timestamptz not null default now(),
            constraint uq_extracted_proposal_subject unique (extraction_run_id, subject_key),
            constraint uq_extracted_proposal_scope_id
                unique (project_id, document_id, extraction_run_id, id),
            constraint fk_extracted_proposal_run_document
                foreign key (document_id, extraction_run_id)
                references extraction_runs(document_id, id)
        );
        create index ix_extracted_proposals_project_id on extracted_proposals(project_id);
        create index ix_extracted_proposals_document_id on extracted_proposals(document_id);
        create index ix_extracted_proposals_extraction_run_id on extracted_proposals(extraction_run_id);
        create table extracted_proposal_facts (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            document_id bigint not null,
            extraction_run_id bigint not null,
            proposal_id bigint not null,
            fact_id bigint not null,
            ordinal integer not null,
            constraint uq_extracted_proposal_fact unique (proposal_id, fact_id),
            constraint uq_extracted_proposal_ordinal unique (proposal_id, ordinal),
            constraint fk_extracted_proposal_fact_proposal_scope
                foreign key (project_id, document_id, extraction_run_id, proposal_id)
                references extracted_proposals(project_id, document_id, extraction_run_id, id),
            constraint fk_extracted_proposal_fact_fact_scope
                foreign key (project_id, document_id, extraction_run_id, fact_id)
                references facts(project_id, document_id, extraction_run_id, id)
        );
        create index ix_extracted_proposal_facts_proposal_id on extracted_proposal_facts(proposal_id);
        create index ix_extracted_proposal_facts_fact_id on extracted_proposal_facts(fact_id);
        create table fact_dispositions (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            predecessor_fact_id bigint not null unique references facts(id),
            successor_fact_id bigint not null unique references facts(id),
            kind varchar(64) not null check (kind = 'source_reading_correction'),
            recorded_by varchar(128) not null,
            recorded_at timestamptz not null default now()
        );
        create index ix_fact_dispositions_project_id on fact_dispositions(project_id);
        create function enforce_immutable_proposal_spine() returns trigger language plpgsql as $$
        begin raise exception 'Extracted Proposal spine is immutable'; end; $$;
        create trigger trg_extracted_proposals_immutable before update or delete on extracted_proposals
            for each row execute function enforce_immutable_proposal_spine();
        create trigger trg_extracted_proposal_facts_immutable before update or delete on extracted_proposal_facts
            for each row execute function enforce_immutable_proposal_spine();
        create trigger trg_fact_dispositions_immutable before update or delete on fact_dispositions
            for each row execute function enforce_immutable_proposal_spine();
        create trigger trg_extracted_proposals_no_truncate before truncate on extracted_proposals
            for each statement execute function enforce_immutable_proposal_spine();
        create trigger trg_extracted_proposal_facts_no_truncate before truncate on extracted_proposal_facts
            for each statement execute function enforce_immutable_proposal_spine();
        create trigger trg_fact_dispositions_no_truncate before truncate on fact_dispositions
            for each statement execute function enforce_immutable_proposal_spine();
        create trigger trg_extraction_run_candidates_immutable
            before update or delete on extraction_run_candidates
            for each row execute function enforce_immutable_proposal_spine();
        create trigger trg_extraction_run_candidates_no_truncate
            before truncate on extraction_run_candidates
            for each statement execute function enforce_immutable_proposal_spine();

        create or replace function enforce_candidate_run_lineage() returns trigger
        language plpgsql as $$
        declare
            demo_project boolean;
            owned_by_run boolean;
        begin
            if tg_op = 'TRUNCATE' then
                raise exception 'Candidate run lineage is immutable' using errcode = '23514';
            end if;
            if tg_op = 'DELETE' then
                select exists (
                    select 1 from projects where projects.id = old.project_id
                    and projects.is_synthetic and projects.slug = 'corridor-demo'
                ) into demo_project;
                if demo_project then return old; end if;
                raise exception 'Candidate run lineage is immutable' using errcode = '23514';
            end if;
            if tg_op = 'INSERT' then
                if new.extraction_run_id is not null then
                    raise exception 'Candidate must attach to its run after input capture'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            if new.id is distinct from old.id
               or new.project_id is distinct from old.project_id
               or new.source_document_id is distinct from old.source_document_id
               or new.kind is distinct from old.kind
               or new.source_pages is distinct from old.source_pages
               or new.confidence is distinct from old.confidence
               or new.prompt_version is distinct from old.prompt_version
               or new.model is distinct from old.model
               or new.created_at is distinct from old.created_at then
                raise exception 'Candidate run lineage is immutable' using errcode = '23514';
            end if;
            if new.extraction_run_id is distinct from old.extraction_run_id then
                if old.extraction_run_id is not null or new.extraction_run_id is null then
                    raise exception 'Candidate run lineage is immutable' using errcode = '23514';
                end if;
                select exists (
                    select 1 from extraction_runs
                    join documents on documents.id = extraction_runs.document_id
                    where extraction_runs.id = new.extraction_run_id
                    and extraction_runs.document_id = new.source_document_id
                    and documents.project_id = new.project_id
                    and extraction_runs.prompt_version = new.prompt_version
                    and extraction_runs.model is not distinct from new.model
                    and (
                        extraction_runs.candidate_inputs_json @> jsonb_build_array(
                            jsonb_build_object(
                                'candidate_id', new.id,
                                'project_id', new.project_id,
                                'source_document_id', new.source_document_id,
                                'prompt_version', new.prompt_version,
                                'model', new.model
                            )
                        )
                        or (
                            (extraction_runs.candidate_inputs_json is null
                             or jsonb_typeof(extraction_runs.candidate_inputs_json) = 'null')
                            and exists (
                                select 1 from extraction_run_candidates
                                where extraction_run_candidates.extraction_run_id = new.extraction_run_id
                                and extraction_run_candidates.candidate_id = new.id
                            )
                        )
                    )
                ) into owned_by_run;
                if not owned_by_run then
                    raise exception 'Candidate is absent from its immutable run inputs'
                        using errcode = '23514';
                end if;
            end if;
            return new;
        end;
        $$;
        """
    )


def downgrade() -> None:
    """Class A proposal and correction history has no destructive path."""

    raise RuntimeError("immutable proposal migration downgrade is unsupported")
