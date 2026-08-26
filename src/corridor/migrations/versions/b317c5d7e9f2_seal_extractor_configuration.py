"""Seal extractor-time configuration and token usage on Extraction Runs.

Revision ID: b317c5d7e9f2
Revises: a316c5d7e9f1

Historical attempts remain honestly unsealed.  Every producer after this
migration writes the complete receipt as one all-or-nothing shape.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b317c5d7e9f2"
down_revision: Union[str, Sequence[str], None] = "a316c5d7e9f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for name in (
        "prompt_sha256",
        "schema_sha256",
        "postprocessor_sha256",
        "extractor_config_sha256",
    ):
        op.add_column(
            "extraction_runs",
            sa.Column(name, sa.String(length=64), nullable=True),
        )
    op.add_column(
        "extraction_runs",
        sa.Column("extractor_config_json", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "extraction_runs",
        sa.Column("token_usage_json", postgresql.JSONB(), nullable=True),
    )

    op.execute(
        """
        create function extraction_token_usage_membership_is_valid(
            run_document_id bigint,
            usage jsonb
        ) returns boolean
        language sql
        immutable
        strict
        as $$
            select case
                when jsonb_typeof(usage -> 'document_ids') is distinct from 'array'
                then false
                else
                    not exists (
                        select 1
                        from jsonb_array_elements(usage -> 'document_ids') member(value)
                        where jsonb_typeof(value) <> 'number'
                           or value #>> '{}' !~ '^[1-9][0-9]*$'
                    )
                    and (
                        select count(*) = count(distinct value)
                        from jsonb_array_elements(usage -> 'document_ids') member(value)
                    )
                    and exists (
                        select 1
                        from jsonb_array_elements(usage -> 'document_ids') member(value)
                        where case
                            when value #>> '{}' ~ '^[1-9][0-9]*$'
                            then (value #>> '{}')::numeric = run_document_id
                            else false
                        end
                    )
            end
        $$
        """
    )

    op.create_check_constraint(
        "ck_extraction_runs_config_receipt_shape",
        "extraction_runs",
        """
        (
            prompt_sha256 is null
            and schema_sha256 is null
            and postprocessor_sha256 is null
            and extractor_config_json is null
            and extractor_config_sha256 is null
            and token_usage_json is null
        )
        or
        (
            prompt_sha256 is not null
            and schema_sha256 is not null
            and postprocessor_sha256 is not null
            and extractor_config_json is not null
            and extractor_config_sha256 is not null
            and token_usage_json is not null
            and prompt_sha256 ~ '^[0-9a-f]{64}$'
            and schema_sha256 ~ '^[0-9a-f]{64}$'
            and postprocessor_sha256 ~ '^[0-9a-f]{64}$'
            and extractor_config_sha256 ~ '^[0-9a-f]{64}$'
            and jsonb_typeof(extractor_config_json) = 'object'
            and extractor_config_json ?& array[
                'receipt_version', 'extractor', 'prompt_version', 'model',
                'schema_version', 'prompt_sha256', 'schema_sha256',
                'postprocessor_sha256', 'request_controls', 'runtime'
            ]
            and jsonb_typeof(extractor_config_json -> 'receipt_version') = 'number'
            and extractor_config_json ->> 'receipt_version' = '1'
            and jsonb_typeof(extractor_config_json -> 'extractor') = 'string'
            and length(trim(extractor_config_json ->> 'extractor')) > 0
            and jsonb_typeof(extractor_config_json -> 'prompt_version') = 'string'
            and jsonb_typeof(extractor_config_json -> 'schema_version') = 'string'
            and jsonb_typeof(extractor_config_json -> 'prompt_sha256') = 'string'
            and jsonb_typeof(extractor_config_json -> 'schema_sha256') = 'string'
            and jsonb_typeof(
                extractor_config_json -> 'postprocessor_sha256'
            ) = 'string'
            and jsonb_typeof(extractor_config_json -> 'request_controls') = 'object'
            and jsonb_typeof(extractor_config_json -> 'runtime') = 'object'
            and extractor_config_json -> 'runtime' ?& array[
                'python_implementation', 'python_version',
                'dependency_lock_sha256', 'packages'
            ]
            and jsonb_typeof(
                extractor_config_json -> 'runtime' -> 'python_implementation'
            ) = 'string'
            and length(trim(
                extractor_config_json -> 'runtime' ->> 'python_implementation'
            )) > 0
            and jsonb_typeof(
                extractor_config_json -> 'runtime' -> 'python_version'
            ) = 'string'
            and length(trim(
                extractor_config_json -> 'runtime' ->> 'python_version'
            )) > 0
            and jsonb_typeof(
                extractor_config_json -> 'runtime' -> 'dependency_lock_sha256'
            ) = 'string'
            and extractor_config_json -> 'runtime' ->> 'dependency_lock_sha256'
                ~ '^[0-9a-f]{64}$'
            and jsonb_typeof(
                extractor_config_json -> 'runtime' -> 'packages'
            ) = 'object'
            and extractor_config_json ->> 'prompt_version' = prompt_version
            and extractor_config_json ->> 'schema_version' = schema_version
            and extractor_config_json ->> 'prompt_sha256' = prompt_sha256
            and extractor_config_json ->> 'schema_sha256' = schema_sha256
            and extractor_config_json ->> 'postprocessor_sha256' = postprocessor_sha256
            and (
                (
                    model is null
                    and jsonb_typeof(extractor_config_json -> 'model') = 'null'
                )
                or (
                    model is not null
                    and jsonb_typeof(extractor_config_json -> 'model') = 'string'
                    and extractor_config_json ->> 'model' = model
                )
            )
            and jsonb_typeof(token_usage_json) = 'object'
            and token_usage_json ?& array['scope', 'document_ids', 'measurement']
            and jsonb_typeof(token_usage_json -> 'scope') = 'string'
            and jsonb_typeof(token_usage_json -> 'measurement') = 'string'
            and token_usage_json ->> 'scope' in ('run', 'batch')
            and jsonb_typeof(token_usage_json -> 'document_ids') = 'array'
            and jsonb_array_length(token_usage_json -> 'document_ids') > 0
            and (
                (
                    token_usage_json ->> 'scope' = 'run'
                    and jsonb_array_length(token_usage_json -> 'document_ids') = 1
                )
                or (
                    token_usage_json ->> 'scope' = 'batch'
                    and jsonb_array_length(token_usage_json -> 'document_ids') > 1
                )
            )
            and (
                (
                    token_usage_json ->> 'measurement' = 'unavailable'
                    and token_usage_json ? 'reason'
                    and jsonb_typeof(token_usage_json -> 'reason') = 'string'
                    and length(trim(token_usage_json ->> 'reason')) > 0
                )
                or (
                    token_usage_json ->> 'measurement' = 'exact'
                    and
                    token_usage_json ?& array[
                        'prompt_tokens', 'completion_tokens',
                        'reasoning_tokens', 'cached_tokens'
                    ]
                    and jsonb_typeof(token_usage_json -> 'prompt_tokens') = 'number'
                    and jsonb_typeof(token_usage_json -> 'completion_tokens') = 'number'
                    and jsonb_typeof(token_usage_json -> 'reasoning_tokens') = 'number'
                    and jsonb_typeof(token_usage_json -> 'cached_tokens') = 'number'
                    and token_usage_json ->> 'prompt_tokens' ~ '^[0-9]+$'
                    and token_usage_json ->> 'completion_tokens' ~ '^[0-9]+$'
                    and token_usage_json ->> 'reasoning_tokens' ~ '^[0-9]+$'
                    and token_usage_json ->> 'cached_tokens' ~ '^[0-9]+$'
                    and (token_usage_json ->> 'prompt_tokens')::numeric >= 0
                    and (token_usage_json ->> 'completion_tokens')::numeric >= 0
                    and (token_usage_json ->> 'reasoning_tokens')::numeric >= 0
                    and (token_usage_json ->> 'cached_tokens')::numeric >= 0
                )
            )
            and extraction_token_usage_membership_is_valid(
                document_id,
                token_usage_json
            )
        ) is true
        """,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_extraction_runs_config_receipt_shape",
        "extraction_runs",
        type_="check",
    )
    op.execute(
        "drop function if exists "
        "extraction_token_usage_membership_is_valid(bigint, jsonb)"
    )
    op.drop_column("extraction_runs", "token_usage_json")
    op.drop_column("extraction_runs", "extractor_config_json")
    for name in (
        "extractor_config_sha256",
        "postprocessor_sha256",
        "schema_sha256",
        "prompt_sha256",
    ):
        op.drop_column("extraction_runs", name)
