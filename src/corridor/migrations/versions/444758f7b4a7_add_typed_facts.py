"""Add typed Facts and same-rendition Fact Sources.

Revision ID: 444758f7b4a7
Revises: 0ca809014df7
"""

from alembic import op


revision = "444758f7b4a7"
down_revision = "0ca809014df7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the Fact envelope and its role-tagged segment join."""

    op.execute(
        """
        create table facts (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            document_id bigint not null,
            extraction_run_id bigint not null,
            fact_type varchar(64) not null,
            subject_kind varchar(32) not null,
            subject_key text not null,
            text_value text,
            date_value date,
            date_range_start date,
            date_range_end date,
            external_org_value_id bigint references external_orgs(id),
            document_value_id bigint references documents(id),
            transformation varchar(64) not null,
            recorded_by varchar(128) not null,
            recorded_at timestamptz not null default now(),
            constraint uq_facts_scope_id unique (project_id, document_id, id),
            constraint uq_facts_run_type_subject
                unique (extraction_run_id, fact_type, subject_key),
            constraint fk_facts_document_scope
                foreign key (project_id, document_id)
                references documents(project_id, id),
            constraint fk_facts_extraction_run_document
                foreign key (document_id, extraction_run_id)
                references extraction_runs(document_id, id),
            constraint ck_facts_type
                check (fact_type in ('station_from', 'station_to')),
            constraint ck_facts_subject
                check (subject_kind = 'source_row' and length(trim(subject_key)) > 0),
            constraint ck_facts_typed_value check (
                fact_type not in ('station_from', 'station_to') or (
                    text_value is not null and length(trim(text_value)) > 0
                    and date_value is null and date_range_start is null
                    and date_range_end is null and external_org_value_id is null
                    and document_value_id is null
                    and transformation = 'trim_cell_text_v1'
                )
            ),
            constraint ck_facts_recorded_by
                check (length(trim(recorded_by)) > 0)
        );
        create index ix_facts_project_id on facts (project_id);
        create index ix_facts_document_id on facts (document_id);
        create index ix_facts_extraction_run_id on facts (extraction_run_id);

        create table fact_sources (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            document_id bigint not null,
            fact_id bigint not null,
            source_segment_id bigint not null,
            role varchar(32) not null,
            ordinal integer not null,
            constraint uq_fact_sources_role_ordinal unique (fact_id, role, ordinal),
            constraint uq_fact_sources_link unique (fact_id, source_segment_id, role),
            constraint fk_fact_sources_fact_scope
                foreign key (project_id, document_id, fact_id)
                references facts(project_id, document_id, id),
            constraint fk_fact_sources_segment_scope
                foreign key (project_id, document_id, source_segment_id)
                references source_segments(project_id, document_id, id),
            constraint ck_fact_sources_role
                check (role in ('value_source', 'context', 'attribution_source')),
            constraint ck_fact_sources_ordinal check (ordinal > 0)
        );
        create index ix_fact_sources_project_id on fact_sources (project_id);
        create index ix_fact_sources_document_id on fact_sources (document_id);
        create index ix_fact_sources_fact_id on fact_sources (fact_id);
        create index ix_fact_sources_source_segment_id on fact_sources (source_segment_id);

        create function enforce_facts_append_only() returns trigger language plpgsql as $$
        begin raise exception 'facts are append-only'; end; $$;
        create trigger trg_facts_append_only before update or delete on facts
            for each row execute function enforce_facts_append_only();
        create trigger trg_facts_no_truncate before truncate on facts
            for each statement execute function enforce_facts_append_only();

        create function enforce_fact_sources_append_only() returns trigger language plpgsql as $$
        begin raise exception 'fact sources are append-only'; end; $$;
        create trigger trg_fact_sources_append_only before update or delete on fact_sources
            for each row execute function enforce_fact_sources_append_only();
        create trigger trg_fact_sources_no_truncate before truncate on fact_sources
            for each statement execute function enforce_fact_sources_append_only();

        create function require_fact_value_source() returns trigger language plpgsql as $$
        begin
            if not exists (
                select 1 from fact_sources
                where fact_id = new.id and role = 'value_source'
            ) then
                raise exception 'source-backed Fact requires a value source';
            end if;
            return null;
        end; $$;
        create constraint trigger trg_facts_require_value_source
            after insert on facts deferrable initially deferred
            for each row execute function require_fact_value_source();
        """
    )


def downgrade() -> None:
    """Class A Fact history has no destructive migration path."""

    raise RuntimeError("typed Fact migration downgrade is unsupported")
