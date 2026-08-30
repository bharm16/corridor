"""Retain declared bounded Coordination Summary configurations and receipts.

Revision ID: c355a7d9e2f1
Revises: b4d1e2f3a5c6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c355a7d9e2f1"
down_revision: Union[str, Sequence[str], None] = "b4d1e2f3a5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "coordination_summary_configurations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source_scope", sa.String(32), nullable=False),
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source_scope in ('all_sources', 'documents_only')", name="ck_summary_config_source_scope"),
        sa.CheckConstraint("max_input_tokens between 1 and 200000", name="ck_summary_config_input_budget"),
        sa.CheckConstraint("max_output_tokens between 1 and 20000", name="ck_summary_config_output_budget"),
        sa.CheckConstraint("timeout_seconds between 1 and 600", name="ck_summary_config_timeout"),
        sa.CheckConstraint("max_requests = 1", name="ck_summary_config_one_request"),
        sa.CheckConstraint("retry_policy = 'none'", name="ck_summary_config_no_retry"),
        sa.CheckConstraint("retention_policy = 'retained_indefinitely'", name="ck_summary_config_retention"),
        sa.CheckConstraint("length(trim(model)) > 0", name="ck_summary_config_model"),
        sa.CheckConstraint("length(trim(prompt_version)) > 0", name="ck_summary_config_prompt"),
        sa.CheckConstraint("length(trim(observation_context)) > 0", name="ck_summary_config_context"),
        sa.CheckConstraint("length(trim(created_by)) > 0", name="ck_summary_config_actor"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_coordination_summary_configurations_project_id", "coordination_summary_configurations", ["project_id"])
    op.create_table(
        "coordination_summary_requests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("configuration_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("reading_sha256", sa.String(64), nullable=False),
        sa.Column("project_reading_json", postgresql.JSONB(), nullable=False),
        sa.Column("evaluated_on", sa.Date(), nullable=False),
        sa.Column("ruleset_version", sa.String(32), nullable=False),
        sa.Column("statement_publication_fingerprint", sa.String(64), nullable=False),
        sa.Column("provenance_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("summary_markdown", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status in ('completed', 'empty_input', 'budget_exhausted', 'timeout', 'transport_failure', 'validation_refused')", name="ck_summary_request_status"),
        sa.CheckConstraint("reading_sha256 ~ '^[0-9a-f]{64}$'", name="ck_summary_request_reading_sha"),
        sa.CheckConstraint("length(trim(requested_by)) > 0", name="ck_summary_request_actor"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["configuration_id"], ["coordination_summary_configurations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("configuration_id", "reading_sha256", name="uq_summary_request_reading"),
    )
    op.create_index("ix_coordination_summary_requests_project_id", "coordination_summary_requests", ["project_id"])
    for table in ("coordination_summary_configurations", "coordination_summary_requests"):
        op.execute(f"""
            create function reject_{table}_mutation() returns trigger language plpgsql as $$
            begin raise exception '{table} are append-only'; end; $$;
            create trigger {table}_are_immutable before update or delete on {table}
            for each row execute function reject_{table}_mutation();
            create trigger {table}_reject_truncate before truncate on {table}
            for each statement execute function reject_{table}_mutation();
        """)


def downgrade() -> None:
    for table in ("coordination_summary_requests", "coordination_summary_configurations"):
        op.execute(f"""
            do $$ begin
              if exists (select 1 from {table}) then raise exception 'cannot erase retained {table}'; end if;
            end $$;
            drop trigger if exists {table}_are_immutable on {table};
            drop trigger if exists {table}_reject_truncate on {table};
            drop function if exists reject_{table}_mutation();
        """)
    op.drop_index("ix_coordination_summary_requests_project_id", table_name="coordination_summary_requests")
    op.drop_table("coordination_summary_requests")
    op.drop_index("ix_coordination_summary_configurations_project_id", table_name="coordination_summary_configurations")
    op.drop_table("coordination_summary_configurations")
