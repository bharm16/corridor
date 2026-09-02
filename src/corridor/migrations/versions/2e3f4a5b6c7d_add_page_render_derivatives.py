"""Persist regenerable purpose-specific render derivative manifests.

Revision ID: 2e3f4a5b6c7d
Revises: 961bd259310f
"""

from alembic import op


revision = "2e3f4a5b6c7d"
down_revision = "452c7d8e9f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a Class B manifest owner without copying source or artifact bytes."""

    op.execute(
        """
        create table page_render_derivatives (
            id bigserial primary key,
            document_id bigint not null references documents(id),
            page_number integer not null,
            derivative_key varchar(64) not null unique,
            profile_name varchar(32) not null,
            profile_id varchar(64) not null,
            source_sha256 varchar(64) not null,
            artifact_path text not null,
            artifact_sha256 varchar(64) not null,
            artifact_bytes bigint not null,
            manifest_json jsonb not null,
            retention_class varchar(32) not null default 'intermediary_processing',
            created_at timestamptz not null default now(),
            constraint ck_page_render_derivatives_page_number check (page_number > 0),
            constraint ck_page_render_derivatives_profile_name check (length(profile_name) > 0),
            constraint ck_page_render_derivatives_profile_id check (length(profile_id) > 0),
            constraint ck_page_render_derivatives_source_sha256
                check (source_sha256 ~ '^[0-9a-f]{64}$'),
            constraint ck_page_render_derivatives_artifact_sha256
                check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
            constraint ck_page_render_derivatives_artifact_bytes check (artifact_bytes > 0),
            constraint ck_page_render_derivatives_retention_class
                check (retention_class = 'intermediary_processing')
        );
        create index ix_page_render_derivatives_document_id
            on page_render_derivatives (document_id);
        """
    )


def downgrade() -> None:
    """Derivative disposal belongs to the hold-aware retention path."""

    raise RuntimeError("page render derivative migration downgrade is unsupported")
