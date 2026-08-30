"""Add grouped Follow-up Plan receipts and reversals for Constraints.

The Constraint page previously saved Assigned To and Next Action as two
independent submits.  One roster-backed Save now commits both Coordination
Decisions atomically (#333, ADR-0035, ADR-0038); this receipt states which
exact Work Decisions one Save grouped and which predecessors the screen had
read, and the reversal names the appended compensating decisions of one
grouped Undo.  No statement row is manufactured for the grouping.

Revision ID: c9e4f2a7b153
Revises: b4d1e2f3a5c6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "c9e4f2a7b153"
down_revision: Union[str, Sequence[str], None] = "b5d1e2f3a5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "follow_up_plan_receipts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        sa.Column("internal_owner_roster_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("internal_owner_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("next_action_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("resumed_deferral_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("audit_log_id", sa.BigInteger(), nullable=False),
        sa.Column("expected_predecessors_json", JSONB(), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "internal_owner_decision_id is not null "
            "or next_action_decision_id is not null",
            name="ck_follow_up_plan_receipt_one_result",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_predecessors_json) = 'object'",
            name="ck_follow_up_plan_receipt_predecessors_object",
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_follow_up_plan_receipt_actor",
        ),
        sa.ForeignKeyConstraint(["dependency_id"], ["dependencies.id"]),
        sa.ForeignKeyConstraint(
            ["internal_owner_roster_entry_id"], ["project_roster_entries.id"]
        ),
        sa.ForeignKeyConstraint(["internal_owner_decision_id"], ["work_decisions.id"]),
        sa.ForeignKeyConstraint(["next_action_decision_id"], ["work_decisions.id"]),
        sa.ForeignKeyConstraint(
            ["resumed_deferral_decision_id"], ["work_decisions.id"]
        ),
        sa.ForeignKeyConstraint(["audit_log_id"], ["audit_log.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("internal_owner_decision_id"),
        sa.UniqueConstraint("next_action_decision_id"),
        sa.UniqueConstraint("resumed_deferral_decision_id"),
        sa.UniqueConstraint("audit_log_id"),
    )
    op.create_index(
        op.f("ix_follow_up_plan_receipts_dependency_id"),
        "follow_up_plan_receipts",
        ["dependency_id"],
    )
    op.create_table(
        "follow_up_plan_reversals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "internal_owner_reversal_decision_id", sa.BigInteger(), nullable=True
        ),
        sa.Column("next_action_reversal_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("deferral_reversal_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("audit_log_id", sa.BigInteger(), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_follow_up_plan_reversal_actor",
        ),
        sa.ForeignKeyConstraint(["receipt_id"], ["follow_up_plan_receipts.id"]),
        sa.ForeignKeyConstraint(
            ["internal_owner_reversal_decision_id"], ["work_decisions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["next_action_reversal_decision_id"], ["work_decisions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["deferral_reversal_decision_id"], ["work_decisions.id"]
        ),
        sa.ForeignKeyConstraint(["audit_log_id"], ["audit_log.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_id"),
        sa.UniqueConstraint("internal_owner_reversal_decision_id"),
        sa.UniqueConstraint("next_action_reversal_decision_id"),
        sa.UniqueConstraint("deferral_reversal_decision_id"),
        sa.UniqueConstraint("audit_log_id"),
    )


def downgrade() -> None:
    op.drop_table("follow_up_plan_reversals")
    op.drop_index(
        op.f("ix_follow_up_plan_receipts_dependency_id"),
        table_name="follow_up_plan_receipts",
    )
    op.drop_table("follow_up_plan_receipts")
