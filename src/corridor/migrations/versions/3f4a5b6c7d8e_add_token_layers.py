"""Persist native and OCR token-layer manifests; classify their artifacts.

Revision ID: 3f4a5b6c7d8e
Revises: 7a3e91c4d8b2
"""

from alembic import op


revision = "3f4a5b6c7d8e"
down_revision = "7a3e91c4d8b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A Class B manifest owner for token layers, and a token_layer artifact kind."""

    op.execute(
        """
        create table token_layers (
            id bigserial primary key,
            document_id bigint not null references documents(id),
            page_no integer not null,
            origin varchar(8) not null,
            layer_key varchar(64) not null unique,
            source_sha256 varchar(64) not null,
            engine_json jsonb not null,
            token_count integer not null,
            quality_json jsonb not null,
            artifact_path text not null,
            artifact_sha256 varchar(64) not null,
            artifact_bytes bigint not null,
            retention_class varchar(32) not null default 'intermediary_processing',
            created_at timestamptz not null default now(),
            constraint ck_token_layers_page_no check (page_no > 0),
            constraint ck_token_layers_origin check (origin in ('native', 'ocr')),
            constraint ck_token_layers_source_sha256
                check (source_sha256 ~ '^[0-9a-f]{64}$'),
            constraint ck_token_layers_artifact_sha256
                check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
            constraint ck_token_layers_artifact_bytes check (artifact_bytes > 0),
            constraint ck_token_layers_token_count check (token_count >= 0),
            constraint ck_token_layers_retention_class
                check (retention_class = 'intermediary_processing')
        );
        create index ix_token_layers_document_id on token_layers (document_id);

        alter table processing_artifacts drop constraint ck_processing_artifact_kind;
        alter table processing_artifacts add constraint ck_processing_artifact_kind
            check (kind in (
                'page_render', 'raw_ocr', 'token_layer',
                'alternate_table_hypothesis', 'unselected_model_response',
                'copied_prompt_context', 'agent_trace', 'evaluation_working_data',
                'abandoned_report_preparation'));
        """
    )


def downgrade() -> None:
    """Token-layer disposal belongs to the hold-aware retention path."""

    raise RuntimeError("token layer migration downgrade is unsupported")
