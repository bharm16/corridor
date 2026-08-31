"""Add scoped Source Fact append idempotency.

Revision ID: 1142da5be661
Revises: d430a1b2c3d4
"""

from alembic import op


revision = "1142da5be661"
down_revision = "d430a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add Fact content identities and immutable command receipts."""

    op.execute(
        """
        alter table facts add column content_sha256 varchar(64);
        alter table facts add constraint ck_facts_content_sha256
            check (content_sha256 is null or content_sha256 ~ '^[0-9a-f]{64}$');
        create unique index uq_facts_content_sha256 on facts (content_sha256)
            where content_sha256 is not null;

        create table source_fact_append_receipts (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            document_id bigint not null references documents(id),
            extraction_run_id bigint not null unique,
            idempotency_key varchar(160) not null,
            content_sha256 varchar(64) not null,
            created_at timestamptz not null default now(),
            constraint uq_source_fact_append_key
                unique (project_id, idempotency_key),
            constraint uq_source_fact_append_content
                unique (project_id, content_sha256),
            constraint fk_source_fact_append_run_document
                foreign key (document_id, extraction_run_id)
                references extraction_runs(document_id, id),
            constraint ck_source_fact_append_content_sha256
                check (content_sha256 ~ '^[0-9a-f]{64}$'),
            constraint ck_source_fact_append_key
                check (length(trim(idempotency_key)) > 0)
        );
        create index ix_source_fact_append_receipts_project_id
            on source_fact_append_receipts (project_id);
        create index ix_source_fact_append_receipts_document_id
            on source_fact_append_receipts (document_id);

        create function enforce_source_fact_append_receipts_immutable()
        returns trigger language plpgsql as $$
        begin raise exception 'source Fact append receipts are immutable'; end; $$;
        create trigger trg_source_fact_append_receipts_immutable
            before update or delete on source_fact_append_receipts
            for each row execute function enforce_source_fact_append_receipts_immutable();
        create trigger trg_source_fact_append_receipts_no_truncate
            before truncate on source_fact_append_receipts
            for each statement execute function enforce_source_fact_append_receipts_immutable();
        """
    )


def downgrade() -> None:
    """Class A Fact identity has no destructive migration path."""

    raise RuntimeError("scoped Source Fact append downgrade is unsupported")
