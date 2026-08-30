"""Retain declared bounded revision-change explanation configs and receipts.

Revision ID: f360a1b2c3d4
Revises: e4c8b1a6d3f7
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f360a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "e4c8b1a6d3f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "revision_change_explanation_configurations",
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
            name="ck_rev_change_expl_cfg_input_budget",
        ),
        sa.CheckConstraint(
            "max_output_tokens between 1 and 20000",
            name="ck_rev_change_expl_cfg_output_budget",
        ),
        sa.CheckConstraint(
            "timeout_seconds between 1 and 600",
            name="ck_rev_change_expl_cfg_timeout",
        ),
        sa.CheckConstraint(
            "max_requests = 1", name="ck_rev_change_expl_cfg_one_request"
        ),
        sa.CheckConstraint(
            "retry_policy = 'none'", name="ck_rev_change_expl_cfg_no_retry"
        ),
        sa.CheckConstraint(
            "retention_policy = 'retained_indefinitely'",
            name="ck_rev_change_expl_cfg_retention",
        ),
        sa.CheckConstraint(
            "length(trim(model)) > 0", name="ck_rev_change_expl_cfg_model"
        ),
        sa.CheckConstraint(
            "length(trim(prompt_version)) > 0", name="ck_rev_change_expl_cfg_prompt"
        ),
        sa.CheckConstraint(
            "length(trim(observation_context)) > 0",
            name="ck_rev_change_expl_cfg_context",
        ),
        sa.CheckConstraint(
            "length(trim(created_by)) > 0", name="ck_rev_change_expl_cfg_actor"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rev_change_expl_cfg_project_id",
        "revision_change_explanation_configurations",
        ["project_id"],
    )
    op.create_table(
        "revision_change_explanation_requests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        sa.Column("comparison_id", sa.BigInteger(), nullable=False),
        sa.Column("finding_id", sa.BigInteger(), nullable=False),
        sa.Column("finding_state", sa.String(32), nullable=False),
        sa.Column("configuration_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("comparison_sha256", sa.String(64), nullable=False),
        sa.Column("state_token", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("adapter_contract_version", sa.String(128)),
        sa.Column("tool_contract_version", sa.String(128), nullable=False),
        sa.Column("validator_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("comparison_json", postgresql.JSONB(), nullable=False),
        sa.Column("explanation_json", postgresql.JSONB()),
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
            name="ck_rev_change_expl_req_status",
        ),
        sa.CheckConstraint(
            "comparison_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rev_change_expl_req_comparison_sha",
        ),
        sa.CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_rev_change_expl_req_state_token",
        ),
        sa.CheckConstraint(
            "length(trim(requested_by)) > 0", name="ck_rev_change_expl_req_actor"
        ),
        sa.CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_rev_change_expl_req_adapter"
        ),
        sa.CheckConstraint(
            "non_authoritative", name="ck_rev_change_expl_req_non_auth"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["dependency_id"], ["dependencies.id"]),
        sa.ForeignKeyConstraint(["comparison_id"], ["revision_comparison_runs.id"]),
        sa.ForeignKeyConstraint(
            ["finding_id"], ["revision_comparison_findings.id"]
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id"],
            ["revision_change_explanation_configurations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "configuration_id",
            "comparison_sha256",
            name="uq_rev_change_expl_req_comparison",
        ),
    )
    op.create_index(
        "ix_rev_change_expl_req_project_id",
        "revision_change_explanation_requests",
        ["project_id"],
    )
    op.create_index(
        "ix_rev_change_expl_req_dependency_id",
        "revision_change_explanation_requests",
        ["dependency_id"],
    )
    for table in (
        "revision_change_explanation_configurations",
        "revision_change_explanation_requests",
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
        "revision_change_explanation_requests",
        "revision_change_explanation_configurations",
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
        "ix_rev_change_expl_req_dependency_id",
        table_name="revision_change_explanation_requests",
    )
    op.drop_index(
        "ix_rev_change_expl_req_project_id",
        table_name="revision_change_explanation_requests",
    )
    op.drop_table("revision_change_explanation_requests")
    op.drop_index(
        "ix_rev_change_expl_cfg_project_id",
        table_name="revision_change_explanation_configurations",
    )
    op.drop_table("revision_change_explanation_configurations")
