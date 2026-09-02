"""Persist PDF page inventories, routing, and scoped processing failures.

Revision ID: 1d2e3f4a5b6c
Revises: 1142da5be661
"""

from alembic import op


revision = "1d2e3f4a5b6c"
down_revision = "1142da5be661"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add rebuildable page decisions without rewriting older page evidence."""

    op.execute(
        """
        alter table doc_pages add column inventory_json jsonb;
        alter table doc_pages add column routing_json jsonb;
        alter table doc_pages add constraint ck_doc_pages_inventory_routing_pair
            check ((inventory_json is null) = (routing_json is null));

        create table page_processing_failures (
            id bigserial primary key,
            document_id bigint not null references documents(id),
            page_number integer not null,
            engine varchar(64) not null,
            configuration_json jsonb not null,
            region_id varchar(64) not null,
            scope_json jsonb not null,
            error_type varchar(160) not null,
            error_message text not null,
            created_at timestamptz not null default now(),
            constraint ck_page_processing_failures_page_number
                check (page_number > 0),
            constraint ck_page_processing_failures_engine
                check (length(engine) > 0),
            constraint ck_page_processing_failures_region_id
                check (length(region_id) > 0),
            constraint ck_page_processing_failures_error_type
                check (length(error_type) > 0),
            constraint ck_page_processing_failures_error_message
                check (length(error_message) > 0)
        );
        create index ix_page_processing_failures_document_id
            on page_processing_failures (document_id);
        """
    )


def downgrade() -> None:
    """Class B inventory can drop only through the later retention policy."""

    raise RuntimeError("PDF page inventory migration downgrade is unsupported")
