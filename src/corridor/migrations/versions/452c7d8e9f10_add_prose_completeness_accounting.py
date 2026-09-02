"""Add durable prose completeness accounting.

Revision ID: 452c7d8e9f10
Revises: 20c7d970be63
"""

from alembic import op


revision = "452c7d8e9f10"
down_revision = "453a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Accept strict segment/subject completeness beside matrix accounting."""

    op.execute(
        """
        alter table extraction_runs
            drop constraint ck_extraction_runs_row_accounting_shape,
            add constraint ck_extraction_runs_row_accounting_shape check (
                row_accounting_json is null or (
                    jsonb_typeof(row_accounting_json) = 'object'
                    and row_accounting_json ->> 'reader_version' = prompt_version
                    and (
                        (
                            row_accounting_json ?& array[
                                'schema_version', 'reader_version', 'reader_path',
                                'detected_row_count', 'accounted_row_count',
                                'extracted_row_count', 'blank_row_count',
                                'skipped_row_count', 'unaccounted_rows', 'rows'
                            ]
                            and row_accounting_json ->> 'schema_version' =
                                'matrix-row-accounting-v1'
                            and jsonb_typeof(row_accounting_json -> 'rows') = 'array'
                            and jsonb_typeof(
                                row_accounting_json -> 'unaccounted_rows'
                            ) = 'array'
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
                            and jsonb_array_length(
                                row_accounting_json -> 'unaccounted_rows'
                            ) =
                                (row_accounting_json ->> 'detected_row_count')::integer -
                                (row_accounting_json ->> 'accounted_row_count')::integer
                        ) or (
                            row_accounting_json ?& array[
                                'schema_version', 'reader_version', 'reader_path',
                                'document_id', 'detected_segment_count',
                                'read_segment_count', 'proposed_fact_count',
                                'unread_segment_ids',
                                'proposed_subject_candidate_ids',
                                'unproposed_subject_candidate_ids'
                            ]
                            and row_accounting_json ->> 'schema_version' =
                                'prose-segment-accounting-v1'
                            and row_accounting_json ->> 'reader_path' =
                                'prose_interpretation'
                            and row_accounting_json ->> 'document_id' ~ '^[0-9]+$'
                            and (row_accounting_json ->> 'document_id')::bigint = document_id
                            and row_accounting_json ->> 'detected_segment_count' ~ '^[0-9]+$'
                            and row_accounting_json ->> 'read_segment_count' ~ '^[0-9]+$'
                            and row_accounting_json ->> 'proposed_fact_count' ~ '^[0-9]+$'
                            and jsonb_typeof(
                                row_accounting_json -> 'unread_segment_ids'
                            ) = 'array'
                            and jsonb_typeof(
                                row_accounting_json -> 'proposed_subject_candidate_ids'
                            ) = 'array'
                            and jsonb_typeof(
                                row_accounting_json -> 'unproposed_subject_candidate_ids'
                            ) = 'array'
                            and (row_accounting_json ->> 'read_segment_count')::integer +
                                jsonb_array_length(
                                    row_accounting_json -> 'unread_segment_ids'
                                ) =
                                (row_accounting_json ->> 'detected_segment_count')::integer
                        )
                    )
                )
            ),
            drop constraint ck_extraction_runs_completed_row_accounting,
            add constraint ck_extraction_runs_completed_row_accounting check (
                not (
                    outcome = 'completed'
                    and prompt_version in (
                        'sheet_native_v2', 'matrix_tiered_v4',
                        'prose_interpretation_v1'
                    )
                ) or (
                    row_accounting_json is not null
                    and (
                        (
                            row_accounting_json ->> 'schema_version' =
                                'matrix-row-accounting-v1'
                            and jsonb_array_length(
                                row_accounting_json -> 'unaccounted_rows'
                            ) = 0
                            and (row_accounting_json ->> 'accounted_row_count')::integer =
                                (row_accounting_json ->> 'detected_row_count')::integer
                            and (row_accounting_json ->> 'extracted_row_count')::integer =
                                candidate_count
                        ) or (
                            row_accounting_json ->> 'schema_version' =
                                'prose-segment-accounting-v1'
                            and (row_accounting_json ->> 'proposed_fact_count')::integer =
                                candidate_count
                        )
                    )
                )
            );
        """
    )


def downgrade() -> None:
    """Durable completeness receipts have no destructive downgrade path."""

    raise RuntimeError("prose completeness migration downgrade is unsupported")
