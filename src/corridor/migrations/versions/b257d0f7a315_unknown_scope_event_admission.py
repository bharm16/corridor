"""Add exact unknown-scope Event Admission and activation receipts.

Revision ID: b257d0f7a315
Revises: a257c9e6f204
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b257d0f7a315"
down_revision: Union[str, Sequence[str], None] = "a257c9e6f204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "event_admission_outcomes",
        sa.Column("commitment_lineage_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "event_admission_outcomes",
        sa.Column("scope_decision_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "event_admission_outcomes",
        sa.Column("candidate_disposition_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "event_admission_outcomes",
        sa.Column("audit_log_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "event_admission_outcomes",
        sa.Column("eligibility_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "event_admission_outcomes",
        sa.Column("eligibility_sha256", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_event_admission_outcome_lineage",
        "event_admission_outcomes",
        "commitment_lineages",
        ["commitment_lineage_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_event_admission_outcome_scope_decision",
        "event_admission_outcomes",
        "dependency_event_scope_decisions",
        ["scope_decision_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_event_admission_outcome_candidate_disposition",
        "event_admission_outcomes",
        "candidate_dispositions",
        ["candidate_disposition_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_event_admission_outcome_audit",
        "event_admission_outcomes",
        "audit_log",
        ["audit_log_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_event_admission_outcome_eligibility_sha256",
        "event_admission_outcomes",
        "eligibility_sha256 is null or eligibility_sha256 ~ '^[0-9a-f]{64}$'",
    )

    op.create_table(
        "event_admission_acceptance_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("migration_head", sa.String(length=64), nullable=False),
        sa.Column("predecessor_policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("reason_version", sa.String(length=64), nullable=False),
        sa.Column("selection_rule", sa.String(length=128), nullable=False),
        sa.Column("receipt_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status in ('passed', 'failed')", name="ck_event_admission_acceptance_status"),
        sa.CheckConstraint("policy_sha256 ~ '^[0-9a-f]{64}$'", name="ck_event_admission_acceptance_policy_sha256"),
        sa.CheckConstraint("receipt_sha256 ~ '^[0-9a-f]{64}$'", name="ck_event_admission_acceptance_receipt_sha256"),
    )
    op.create_index(
        "ix_event_admission_acceptance_project",
        "event_admission_acceptance_receipts",
        ["project_id", "id"],
    )
    op.create_table(
        "event_admission_activations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column(
            "acceptance_receipt_id",
            sa.BigInteger(),
            sa.ForeignKey("event_admission_acceptance_receipts.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("action in ('activate', 'suspend')", name="ck_event_admission_activation_action"),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_event_admission_activation_reason"),
        sa.CheckConstraint("length(trim(recorded_by)) > 0", name="ck_event_admission_activation_actor"),
    )
    op.create_index(
        "ix_event_admission_activation_project",
        "event_admission_activations",
        ["project_id", "id"],
    )


def downgrade() -> None:
    raise RuntimeError("cannot erase immutable Event Admission receipts")
