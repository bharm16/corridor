"""Add the durable Record Inclusion watermark.

Reconciling a project's Record Inclusion (``load_project``) appends a PolicyRun
on every call, so an unconditional call on every idle scheduled tick would grow
the receipt log without bound. This table is the recoverable handoff that gates
that work: a producer bumps ``dirty_seq`` inside its own transaction, and
reconciliation runs only while ``dirty_seq > reconciled_seq``. Unlike the
append-only receipt tables this is mutable operational state — a watermark, like
a due-work occurrence — so it carries no immutability trigger.

Revision ID: 86edb31fd81a
Revises: a364b7c9e2f1
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "86edb31fd81a"
down_revision: Union[str, Sequence[str], None] = "a364b7c9e2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "record_inclusion_requests",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "dirty_seq",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reconciled_seq",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_reason", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "dirty_seq >= 0 and reconciled_seq >= 0",
            name="ck_record_inclusion_requests_non_negative",
        ),
        sa.CheckConstraint(
            "reconciled_seq <= dirty_seq",
            name="ck_record_inclusion_requests_watermark_order",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("project_id"),
    )


def downgrade() -> None:
    op.drop_table("record_inclusion_requests")
