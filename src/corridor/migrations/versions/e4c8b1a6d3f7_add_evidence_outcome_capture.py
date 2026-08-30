"""Retain declared capture contracts and cutoff-correct outcome associations.

Revision ID: e4c8b1a6d3f7
Revises: e361f1a2b3c4
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e4c8b1a6d3f7"
down_revision: Union[str, Sequence[str], None] = "e361f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_investigation_capture_contracts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("cohort_id", sa.String(128), nullable=False),
        sa.Column("contract_sha256", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("adapter_contract_version", sa.String(128), nullable=False),
        sa.Column("tool_contract_version", sa.String(128), nullable=False),
        sa.Column("validator_version", sa.String(128), nullable=False),
        sa.Column("baseline_identity", sa.String(128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("protection_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "history_retained_from", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("missing_label_policy", sa.String(32), nullable=False),
        sa.Column("member_case_public_ids_json", postgresql.JSONB(), nullable=False),
        sa.Column("contract_json", postgresql.JSONB(), nullable=False),
        sa.Column("declared_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "missing_label_policy = 'remain_missing'",
            name="ck_capture_contract_missing_label_policy",
        ),
        sa.CheckConstraint(
            "length(trim(declared_by)) > 0", name="ck_capture_contract_actor"
        ),
        sa.CheckConstraint(
            "window_start <= cutoff_at",
            name="ck_capture_contract_window_before_cutoff",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("contract_sha256"),
    )
    op.create_index(
        "ix_capture_contracts_project_id",
        "evidence_investigation_capture_contracts",
        ["project_id"],
    )
    op.create_table(
        "evidence_investigation_capture_results",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("capture_contract_id", sa.BigInteger(), nullable=False),
        sa.Column("shadow_case_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completeness", sa.String(16), nullable=False),
        sa.Column("incomplete_reason", sa.String(64), nullable=True),
        sa.Column("candidate_disposition", sa.String(32), nullable=True),
        sa.Column("scope_mode", sa.String(32), nullable=True),
        sa.Column(
            "selected_dependency_ids_json", postgresql.JSONB(), nullable=False
        ),
        sa.Column("correction", sa.Boolean(), nullable=False),
        sa.Column("undo", sa.Boolean(), nullable=False),
        sa.Column("unresolved", sa.Boolean(), nullable=False),
        sa.Column("human_outcome_identity", sa.String(64), nullable=True),
        sa.Column("outcome_identities_json", postgresql.JSONB(), nullable=False),
        sa.Column("strata_json", postgresql.JSONB(), nullable=False),
        sa.Column("review_seconds", sa.Float(), nullable=True),
        sa.Column("association_sha256", sa.String(64), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "completeness in ('complete', 'incomplete')",
            name="ck_capture_result_completeness",
        ),
        sa.CheckConstraint(
            "(completeness = 'complete' and incomplete_reason is null) or "
            "(completeness = 'incomplete' and incomplete_reason is not null)",
            name="ck_capture_result_incomplete_reason",
        ),
        sa.ForeignKeyConstraint(
            ["capture_contract_id"],
            ["evidence_investigation_capture_contracts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["shadow_case_id"], ["evidence_investigation_shadow_cases.id"]
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(
            ["run_id"], ["evidence_investigation_runs.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "capture_contract_id", "shadow_case_id", name="uq_capture_result_case"
        ),
    )
    op.create_index(
        "ix_capture_results_contract_id",
        "evidence_investigation_capture_results",
        ["capture_contract_id"],
    )
    op.create_index(
        "ix_capture_results_project_id",
        "evidence_investigation_capture_results",
        ["project_id"],
    )
    for table in (
        "evidence_investigation_capture_contracts",
        "evidence_investigation_capture_results",
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
        "evidence_investigation_capture_results",
        "evidence_investigation_capture_contracts",
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
        "ix_capture_results_project_id",
        table_name="evidence_investigation_capture_results",
    )
    op.drop_index(
        "ix_capture_results_contract_id",
        table_name="evidence_investigation_capture_results",
    )
    op.drop_table("evidence_investigation_capture_results")
    op.drop_index(
        "ix_capture_contracts_project_id",
        table_name="evidence_investigation_capture_contracts",
    )
    op.drop_table("evidence_investigation_capture_contracts")
