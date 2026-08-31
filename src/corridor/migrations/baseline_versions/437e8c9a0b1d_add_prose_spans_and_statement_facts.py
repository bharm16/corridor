"""Add prose spans and pending statement wording Facts.

Revision ID: 437e8c9a0b1d
Revises: 961bd259310f
"""

from alembic import op


revision = "437e8c9a0b1d"
down_revision = "961bd259310f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Extend the evidence spine to exact page spans and statement wording."""

    op.execute(
        """
        alter table source_segments
            alter column sheet_name drop not null,
            alter column cell_range drop not null,
            add column page_no integer,
            add column start_offset integer,
            add column end_offset integer,
            drop constraint ck_source_segments_kind,
            add constraint ck_source_segments_kind
                check (kind in ('spreadsheet_cell', 'prose_span')),
            drop constraint ck_source_segments_spreadsheet_locator,
            add constraint ck_source_segments_locator check (
                (kind = 'spreadsheet_cell'
                    and length(sheet_name) > 0
                    and cell_range ~ '^[A-Z]+[1-9][0-9]*$'
                    and page_no is null
                    and start_offset is null
                    and end_offset is null)
                or
                (kind = 'prose_span'
                    and sheet_name is null
                    and cell_range is null
                    and page_no > 0
                    and start_offset >= 0
                    and end_offset > start_offset)
            ),
            add constraint uq_source_segments_prose_locator
                unique (document_id, kind, page_no, start_offset, end_offset);

        create function enforce_prose_segment_non_overlap() returns trigger
        language plpgsql as $$
        begin
            if new.kind = 'prose_span' and exists (
                select 1 from source_segments existing
                where existing.document_id = new.document_id
                  and existing.kind = 'prose_span'
                  and existing.page_no = new.page_no
                  and int4range(existing.start_offset, existing.end_offset, '[)')
                      && int4range(new.start_offset, new.end_offset, '[)')
            ) then
                raise exception 'prose source segments cannot overlap';
            end if;
            return new;
        end;
        $$;
        create trigger trg_source_segments_prose_non_overlap
            before insert on source_segments
            for each row execute function enforce_prose_segment_non_overlap();

        alter table facts
            drop constraint ck_facts_type,
            add constraint ck_facts_type
                check (fact_type in ('station_from', 'station_to', 'statement_wording')),
            drop constraint ck_facts_subject,
            add constraint ck_facts_subject check (
                (fact_type in ('station_from', 'station_to')
                    and subject_kind = 'source_row'
                    and length(trim(subject_key)) > 0)
                or
                (fact_type = 'statement_wording'
                    and subject_kind = 'statement_candidate'
                    and length(trim(subject_key)) > 0)
            ),
            drop constraint ck_facts_typed_value,
            add constraint ck_facts_typed_value check (
                (fact_type in ('station_from', 'station_to')
                    and text_value is not null
                    and length(trim(text_value)) > 0
                    and date_value is null
                    and date_range_start is null
                    and date_range_end is null
                    and external_org_value_id is null
                    and document_value_id is null
                    and transformation = 'trim_cell_text_v1')
                or
                (fact_type = 'statement_wording'
                    and text_value is not null
                    and length(trim(text_value)) > 0
                    and date_value is null
                    and date_range_start is null
                    and date_range_end is null
                    and external_org_value_id is null
                    and document_value_id is null
                    and transformation = 'exact_prose_span_v1')
            );
        """
    )


def downgrade() -> None:
    """Class A prose spans and Facts have no destructive migration path."""

    raise RuntimeError("prose span migration downgrade is unsupported")
