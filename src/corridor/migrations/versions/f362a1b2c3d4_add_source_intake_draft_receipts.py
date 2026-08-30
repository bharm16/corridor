"""Retain declared bounded source-intake-draft configs and receipts (#362).

Revision ID: f362a1b2c3d4
Revises: f360a1b2c3d4
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f362a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "e360b7c1d2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_intake_draft_configurations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_requests", sa.Integer(), nullable=False),
        sa.Column("retry_policy", sa.String(32), nullable=False),
        sa.Column("retention_policy", sa.String(64), nullable=False),
        sa.Column("observation_context", sa.String(128), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_input_tokens between 1 and 200000",
            name="ck_intake_draft_config_input_budget",
        ),
        sa.CheckConstraint(
            "max_output_tokens between 1 and 20000",
            name="ck_intake_draft_config_output_budget",
        ),
        sa.CheckConstraint(
            "timeout_seconds between 1 and 600",
            name="ck_intake_draft_config_timeout",
        ),
        sa.CheckConstraint(
            "max_requests = 1", name="ck_intake_draft_config_one_request"
        ),
        sa.CheckConstraint(
            "retry_policy = 'none'", name="ck_intake_draft_config_no_retry"
        ),
        sa.CheckConstraint(
            "retention_policy = 'retained_indefinitely'",
            name="ck_intake_draft_config_retention",
        ),
        sa.CheckConstraint(
            "length(trim(model)) > 0", name="ck_intake_draft_config_model"
        ),
        sa.CheckConstraint(
            "length(trim(prompt_version)) > 0", name="ck_intake_draft_config_prompt"
        ),
        sa.CheckConstraint(
            "length(trim(observation_context)) > 0",
            name="ck_intake_draft_config_context",
        ),
        sa.CheckConstraint(
            "length(trim(created_by)) > 0", name="ck_intake_draft_config_actor"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_intake_draft_configurations_project_id",
        "source_intake_draft_configurations",
        ["project_id"],
    )
    op.create_table(
        "source_intake_draft_requests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("staged_sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("declared_doc_type", sa.String(64), nullable=False),
        sa.Column("configuration_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("state_token", sa.String(64), nullable=False),
        sa.Column("permitted_pages_json", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("adapter_contract_version", sa.String(128)),
        sa.Column("tool_contract_version", sa.String(128), nullable=False),
        sa.Column("validator_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("source_json", postgresql.JSONB(), nullable=False),
        sa.Column("proposals_json", postgresql.JSONB()),
        sa.Column("execution_lineage_json", postgresql.JSONB()),
        sa.Column("read_fingerprint", sa.String(64)),
        sa.Column("budget_json", postgresql.JSONB(), nullable=False),
        sa.Column("usage_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "non_authoritative",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_intake_draft_request_status",
        ),
        sa.CheckConstraint(
            "staged_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_staged_sha",
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_source_sha",
        ),
        sa.CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_state_token",
        ),
        sa.CheckConstraint(
            "length(trim(requested_by)) > 0", name="ck_intake_draft_request_actor"
        ),
        sa.CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_intake_draft_request_adapter"
        ),
        sa.CheckConstraint(
            "non_authoritative", name="ck_intake_draft_request_non_auth"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["source_intake_draft_configurations.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "configuration_id",
            "source_sha256",
            name="uq_intake_draft_request_source",
        ),
    )
    op.create_index(
        "ix_intake_draft_requests_project_id",
        "source_intake_draft_requests",
        ["project_id"],
    )
    op.create_index(
        "ix_intake_draft_requests_staged_sha256",
        "source_intake_draft_requests",
        ["staged_sha256"],
    )
    for table in (
        "source_intake_draft_configurations",
        "source_intake_draft_requests",
    ):
        op.execute(f"""
            create function reject_{table}_mutation() returns trigger language plpgsql as $$
            begin raise exception '{table} are append-only'; end; $$;
            create trigger {table}_are_immutable before update or delete on {table}
            for each row execute function reject_{table}_mutation();
            create trigger {table}_reject_truncate before truncate on {table}
            for each statement execute function reject_{table}_mutation();
        """)


def downgrade() -> None:
    for table in (
        "source_intake_draft_requests",
        "source_intake_draft_configurations",
    ):
        op.execute(f"""
            do $$ begin
              if exists (select 1 from {table}) then raise exception 'cannot erase retained {table}'; end if;
            end $$;
            drop trigger if exists {table}_are_immutable on {table};
            drop trigger if exists {table}_reject_truncate on {table};
            drop function if exists reject_{table}_mutation();
        """)
    op.drop_index(
        "ix_intake_draft_requests_staged_sha256",
        table_name="source_intake_draft_requests",
    )
    op.drop_index(
        "ix_intake_draft_requests_project_id",
        table_name="source_intake_draft_requests",
    )
    op.drop_table("source_intake_draft_requests")
    op.drop_index(
        "ix_intake_draft_configurations_project_id",
        table_name="source_intake_draft_configurations",
    )
    op.drop_table("source_intake_draft_configurations")
