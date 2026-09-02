"""Add immutable spreadsheet Source Segments.

Revision ID: 0ca809014df7
Revises: c0a1d0b5e11e
"""

from alembic import op


revision = "0ca809014df7"
down_revision = "c0a1d0b5e11e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the first typed-locator slice of the evidence spine."""

    op.execute(
        """
        create table source_segments (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            document_id bigint not null,
            kind varchar(32) not null,
            exact_text text not null,
            content_sha256 varchar(64) not null,
            ordinal integer not null,
            sheet_name text not null,
            cell_range varchar(32) not null,
            created_at timestamptz not null default now(),
            constraint uq_source_segments_scope_id
                unique (project_id, document_id, id),
            constraint uq_source_segments_document_kind_ordinal
                unique (document_id, kind, ordinal),
            constraint uq_source_segments_spreadsheet_locator
                unique (document_id, kind, sheet_name, cell_range),
            constraint fk_source_segments_document_scope
                foreign key (project_id, document_id)
                references documents(project_id, id),
            constraint ck_source_segments_kind
                check (kind = 'spreadsheet_cell'),
            constraint ck_source_segments_exact_text
                check (length(exact_text) > 0),
            constraint ck_source_segments_content_sha256
                check (content_sha256 ~ '^[0-9a-f]{64}$'),
            constraint ck_source_segments_ordinal check (ordinal > 0),
            constraint ck_source_segments_spreadsheet_locator
                check (
                    length(sheet_name) > 0
                    and cell_range ~ '^[A-Z]+[1-9][0-9]*$'
                )
        );

        create index ix_source_segments_project_id
            on source_segments (project_id);
        create index ix_source_segments_document_id
            on source_segments (document_id);

        create function enforce_source_segments_append_only() returns trigger
        language plpgsql as $$
        begin
            raise exception 'source segments are append-only';
        end;
        $$;

        create trigger trg_source_segments_append_only
            before update or delete on source_segments
            for each row execute function enforce_source_segments_append_only();

        create trigger trg_source_segments_no_truncate
            before truncate on source_segments
            for each statement execute function enforce_source_segments_append_only();
        """
    )


def downgrade() -> None:
    """Class A record rows have no destructive migration path."""

    raise RuntimeError("source segment migration downgrade is unsupported")
