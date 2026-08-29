"""Retain complete row accounting on matrix Extraction Run receipts.

Candidate counts were rejected as proof of completeness because a reader can
drop a row before Candidate creation. This successor leaves historical runs
untouched and requires the bumped spreadsheet/page readers to seal every
detected row as extracted, blank, skipped, or a retained failure discrepancy.

Revision ID: e319f8a0b2c5
Revises: d319e7f9a1b4
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e319f8a0b2c5"
down_revision: Union[str, Sequence[str], None] = "d319e7f9a1b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("row_accounting_json", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "ck_extraction_runs_row_accounting_shape",
        "extraction_runs",
        """
        row_accounting_json is null or (
            jsonb_typeof(row_accounting_json) = 'object'
            and row_accounting_json ?& array[
                'schema_version', 'reader_version', 'reader_path',
                'detected_row_count', 'accounted_row_count',
                'extracted_row_count', 'blank_row_count',
                'skipped_row_count', 'unaccounted_rows', 'rows'
            ]
            and row_accounting_json ->> 'schema_version' =
                'matrix-row-accounting-v1'
            and row_accounting_json ->> 'reader_version' = prompt_version
            and jsonb_typeof(row_accounting_json -> 'rows') = 'array'
            and jsonb_typeof(row_accounting_json -> 'unaccounted_rows') = 'array'
            and row_accounting_json ->> 'detected_row_count' ~ '^[0-9]+$'
            and row_accounting_json ->> 'accounted_row_count' ~ '^[0-9]+$'
            and row_accounting_json ->> 'extracted_row_count' ~ '^[0-9]+$'
            and row_accounting_json ->> 'blank_row_count' ~ '^[0-9]+$'
            and row_accounting_json ->> 'skipped_row_count' ~ '^[0-9]+$'
            and jsonb_array_length(row_accounting_json -> 'rows') =
                (row_accounting_json ->> 'detected_row_count')::integer
            and (row_accounting_json ->> 'accounted_row_count')::integer =
                (row_accounting_json ->> 'extracted_row_count')::integer +
                (row_accounting_json ->> 'blank_row_count')::integer +
                (row_accounting_json ->> 'skipped_row_count')::integer
            and jsonb_array_length(row_accounting_json -> 'unaccounted_rows') =
                (row_accounting_json ->> 'detected_row_count')::integer -
                (row_accounting_json ->> 'accounted_row_count')::integer
        )
        """,
    )
    op.create_check_constraint(
        "ck_extraction_runs_completed_row_accounting",
        "extraction_runs",
        """
        not (
            outcome = 'completed'
            and prompt_version in ('sheet_native_v2', 'matrix_tiered_v4')
        ) or (
            row_accounting_json is not null
            and jsonb_array_length(row_accounting_json -> 'unaccounted_rows') = 0
            and (row_accounting_json ->> 'accounted_row_count')::integer =
                (row_accounting_json ->> 'detected_row_count')::integer
            and (row_accounting_json ->> 'extracted_row_count')::integer =
                candidate_count
        )
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (
                select 1 from extraction_runs where row_accounting_json is not null
            ) then
                raise exception 'cannot erase retained matrix row accounting';
            end if;
        end
        $$;
        """
    )
    op.drop_constraint(
        "ck_extraction_runs_completed_row_accounting",
        "extraction_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_extraction_runs_row_accounting_shape",
        "extraction_runs",
        type_="check",
    )
    op.drop_column("extraction_runs", "row_accounting_json")
