"""Add Class B retention manifests and holds.

Revision ID: 7a3e91c4d8b2
Revises: 961bd259310f
"""

from alembic import op


revision = "7a3e91c4d8b2"
down_revision = "2e3f4a5b6c7d"
branch_labels = None
depends_on = None


_REQUEST_TABLES = (
    "coordination_summary_requests",
    "production_run_explanation_requests",
    "extraction_failure_diagnosis_requests",
    "revision_change_explanation_requests",
    "source_intake_draft_requests",
)


def upgrade() -> None:
    """Classify only intermediary receipts and add hold-aware deletion controls."""

    for table in _REQUEST_TABLES:
        op.execute(
            f"""
            alter table {table}
                add column retention_class varchar(16) not null default 'class_b',
                add column retention_content_sha256 varchar(64),
                add column retention_deleted_at timestamptz,
                add constraint ck_{table}_retention_class
                    check (retention_class = 'class_b'),
                add constraint ck_{table}_retention_digest
                    check (retention_content_sha256 is null or
                           retention_content_sha256 ~ '^[0-9a-f]{{64}}$');
            """
        )

    for table, constraint in {
        "coordination_summary_configurations": "ck_summary_config_retention",
        "production_run_explanation_configurations": "ck_run_explanation_config_retention",
        "extraction_failure_diagnosis_configurations": "ck_failure_diagnosis_config_retention",
        "revision_change_explanation_configurations": "ck_rev_change_expl_cfg_retention",
        "source_intake_draft_configurations": "ck_intake_draft_config_retention",
    }.items():
        op.execute(
            f"""
            alter table {table} drop constraint {constraint};
            alter table {table} add constraint {constraint}
                check (retention_policy in ('retained_indefinitely', 'class_b_30_days'));
            """
        )

    for table, columns in {
        "coordination_summary_requests": ("project_reading_json", "summary_markdown"),
        "production_run_explanation_requests": (
            "competing_run_ids_json", "comparison_json", "explanation_json",
            "execution_lineage_json", "budget_json", "usage_json",
        ),
        "extraction_failure_diagnosis_requests": (
            "source_context_json", "diagnosis_json", "execution_lineage_json",
            "budget_json", "usage_json",
        ),
        "revision_change_explanation_requests": (
            "comparison_json", "explanation_json", "execution_lineage_json",
            "budget_json", "usage_json",
        ),
        "source_intake_draft_requests": (
            "permitted_pages_json", "source_json", "proposals_json",
            "execution_lineage_json", "budget_json", "usage_json",
        ),
    }.items():
        for column in columns:
            op.execute(f"alter table {table} alter column {column} drop not null")

    op.execute(
        """
        create table processing_artifacts (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            kind varchar(64) not null,
            retention_class varchar(16) not null,
            storage_path text not null,
            content_sha256 varchar(64) not null,
            terminal_at timestamptz not null,
            deleted_at timestamptz,
            constraint uq_processing_artifact_path unique (storage_path),
            constraint ck_processing_artifact_kind check (kind in (
                'page_render', 'raw_ocr', 'alternate_table_hypothesis',
                'unselected_model_response', 'copied_prompt_context',
                'agent_trace', 'evaluation_working_data',
                'abandoned_report_preparation')),
            constraint ck_processing_artifact_class check (retention_class = 'class_b'),
            constraint ck_processing_artifact_sha check (content_sha256 ~ '^[0-9a-f]{64}$')
        );
        create index ix_processing_artifacts_project_id on processing_artifacts(project_id);

        create table retention_holds (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            reason text not null,
            placed_by varchar(128) not null,
            placed_at timestamptz not null default now(),
            lifted_by varchar(128),
            lifted_at timestamptz,
            constraint ck_retention_holds_reason check (length(trim(reason)) > 0),
            constraint ck_retention_holds_actor check (length(trim(placed_by)) > 0),
            constraint ck_retention_holds_lift check (
                (lifted_by is null and lifted_at is null) or
                (length(trim(lifted_by)) > 0 and lifted_at is not null)
            )
        );
        create unique index uq_retention_holds_active_project
            on retention_holds(project_id) where lifted_at is null;

        create table retention_references (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            family varchar(64) not null,
            source_row_id bigint not null,
            kind varchar(32) not null,
            referenced_by text not null,
            opened_at timestamptz not null default now(),
            closed_at timestamptz,
            constraint ck_retention_reference_kind check (
                kind in ('segment', 'decision', 'review', 'release', 'processing_failure')
            ),
            constraint ck_retention_reference_identity
                check (length(trim(family)) > 0 and length(trim(referenced_by)) > 0)
        );
        create index ix_retention_references_source
            on retention_references(family, source_row_id) where closed_at is null;

        create table retention_manifests (
            id bigserial primary key,
            public_id varchar(36) not null unique,
            as_of timestamptz not null,
            status varchar(16) not null,
            content_sha256 varchar(64) not null,
            created_by varchar(128) not null,
            created_at timestamptz not null default now(),
            executed_at timestamptz,
            constraint ck_retention_manifest_status
                check (status in ('dry_run', 'executed', 'refused')),
            constraint ck_retention_manifest_sha
                check (content_sha256 ~ '^[0-9a-f]{64}$'),
            constraint ck_retention_manifest_actor check (length(trim(created_by)) > 0)
        );
        create table retention_manifest_items (
            id bigserial primary key,
            manifest_id bigint not null references retention_manifests(id),
            project_id bigint not null references projects(id),
            family varchar(64) not null,
            source_row_id bigint not null,
            content_sha256 varchar(64) not null,
            terminal_at timestamptz not null,
            delete_after timestamptz not null,
            constraint uq_retention_manifest_item unique (manifest_id, family, source_row_id),
            constraint ck_retention_manifest_item_sha
                check (content_sha256 ~ '^[0-9a-f]{64}$')
        );

        create function block_held_intermediary_delete() returns trigger
        language plpgsql as $$
        begin
            if exists (
                select 1 from retention_holds
                where project_id = old.project_id and lifted_at is null
            ) then
                raise exception 'active retention hold blocks intermediary deletion'
                    using errcode = '23514';
            end if;
            return old;
        end;
        $$;

        create function enforce_manifested_class_b_expiry() returns trigger
        language plpgsql as $$
        declare
            manifest bigint;
            family_name text;
            allowed text[];
        begin
            if tg_op <> 'UPDATE' then
                raise exception '% are append-only', tg_table_name;
            end if;
            manifest := nullif(current_setting('corridor.retention_manifest_id', true), '')::bigint;
            family_name := case tg_table_name
                when 'coordination_summary_requests' then 'coordination_summary'
                when 'production_run_explanation_requests' then 'production_run_explanation'
                when 'extraction_failure_diagnosis_requests' then 'extraction_failure_diagnosis'
                when 'revision_change_explanation_requests' then 'revision_change_explanation'
                when 'source_intake_draft_requests' then 'source_intake_draft'
            end;
            allowed := case tg_table_name
                when 'coordination_summary_requests' then array[
                    'project_reading_json', 'summary_markdown',
                    'retention_content_sha256', 'retention_deleted_at']
                when 'production_run_explanation_requests' then array[
                    'competing_run_ids_json', 'comparison_json', 'explanation_json',
                    'execution_lineage_json', 'budget_json', 'usage_json',
                    'retention_content_sha256', 'retention_deleted_at']
                when 'extraction_failure_diagnosis_requests' then array[
                    'source_context_json', 'diagnosis_json', 'execution_lineage_json',
                    'budget_json', 'usage_json', 'retention_content_sha256',
                    'retention_deleted_at']
                when 'revision_change_explanation_requests' then array[
                    'comparison_json', 'explanation_json', 'execution_lineage_json',
                    'budget_json', 'usage_json', 'retention_content_sha256',
                    'retention_deleted_at']
                when 'source_intake_draft_requests' then array[
                    'permitted_pages_json', 'source_json', 'proposals_json',
                    'execution_lineage_json', 'budget_json', 'usage_json',
                    'retention_content_sha256', 'retention_deleted_at']
            end;
            if manifest is null
               or new.retention_class <> 'class_b'
               or new.retention_content_sha256 is null
               or new.retention_deleted_at is null
               or (to_jsonb(new) - allowed) <> (to_jsonb(old) - allowed)
               or not exists (
                    select 1 from retention_manifest_items item
                    join retention_manifests manifest_row on manifest_row.id = item.manifest_id
                    where item.manifest_id = manifest
                      and manifest_row.status = 'dry_run'
                      and item.family = family_name
                      and item.source_row_id = old.id
                      and item.content_sha256 = new.retention_content_sha256
               ) then
                raise exception '% are append-only outside manifested retention', tg_table_name;
            end if;
            return new;
        end;
        $$;
        """
    )
    for table in _REQUEST_TABLES:
        old_trigger = table.replace("_requests", "_requests_are_immutable")
        op.execute(
            f"""
            drop trigger {old_trigger} on {table};
            create trigger {table}_retention_aware_immutable
                before update or delete on {table}
                for each row execute function enforce_manifested_class_b_expiry();
            create trigger aa_{table}_retention_hold
                before delete on {table}
                for each row execute function block_held_intermediary_delete();
            """
        )


def downgrade() -> None:
    """Retention manifests and legal-hold history are not destructively retired."""

    raise RuntimeError("Class B retention migration downgrade is unsupported")
