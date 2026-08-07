"""add event cohort receipts

Revision ID: b3e8a92f4c17
Revises: f4a7d28e6b95
Create Date: 2026-08-07 17:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b3e8a92f4c17"
down_revision: Union[str, Sequence[str], None] = "f4a7d28e6b95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """The pinned boundary of a cohort no Revision Comparison selects."""
    op.create_table(
        "event_cohort_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("input_run_ids", JSONB(), nullable=False),
        sa.Column("members", JSONB(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id",
            "rule_version",
            name="uq_event_cohort_receipts_one_per_rule",
        ),
    )


def downgrade() -> None:
    op.drop_table("event_cohort_receipts")
