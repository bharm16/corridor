"""A recorded-verbal-statement source segment: the recorder's exact words.

Revision ID: 5b6c7d8e9f01
Revises: 4a5b6c7d8e9f
"""

from alembic import op


revision = "5b6c7d8e9f01"
down_revision = "4a5b6c7d8e9f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Let a Recorded Verbal Statement's exact words be a citable segment (#451).

    A verbal has no source Document to replay against (ADR-0033/0068): the named
    recorder's words are the source. So the segment carries `statement_id`
    instead of `document_id`, and its integrity is the digest's self-consistency
    with those words. `document_id` becomes nullable for this kind only; the
    document-scoped kinds keep their non-null document and locator.
    """

    op.execute(
        """
        alter table source_segments
            alter column document_id drop not null,
            add column statement_id bigint references dependency_events(id),
            drop constraint ck_source_segments_kind,
            add constraint ck_source_segments_kind
                check (kind in
                    ('spreadsheet_cell', 'prose_span', 'recorded_verbal_statement')),
            drop constraint ck_source_segments_locator,
            add constraint ck_source_segments_locator check (
                (kind = 'spreadsheet_cell'
                    and document_id is not null and statement_id is null
                    and length(sheet_name) > 0
                    and cell_range ~ '^[A-Z]+[1-9][0-9]*$'
                    and page_no is null
                    and start_offset is null
                    and end_offset is null)
                or
                (kind = 'prose_span'
                    and document_id is not null and statement_id is null
                    and sheet_name is null
                    and cell_range is null
                    and page_no > 0
                    and start_offset >= 0
                    and end_offset > start_offset)
                or
                (kind = 'recorded_verbal_statement'
                    and document_id is null and statement_id is not null
                    and sheet_name is null
                    and cell_range is null
                    and page_no is null
                    and start_offset is null
                    and end_offset is null)
            );

        create unique index uq_source_segments_statement
            on source_segments (statement_id)
            where kind = 'recorded_verbal_statement';
        """
    )


def downgrade() -> None:
    """Segment kinds are append-only history; a downgrade is unsupported."""

    raise RuntimeError("recorded verbal segment migration downgrade is unsupported")
