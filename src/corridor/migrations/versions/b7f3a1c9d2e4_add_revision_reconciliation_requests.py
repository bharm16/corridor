"""Add the durable revision-reconciliation watermark.

Registered document revisions and committed eligibility changes must advance
through Document Revision Processing and Automatic Support Update without an
operator transcribing run pairs, but re-running that pass on every idle
scheduled tick would create a Revision Comparison nobody asked for and append a
Carry-Forward PolicyRun without bound. This table is the recoverable handoff
that gates the work, exactly like ``record_inclusion_requests``: a producer
bumps ``dirty_seq`` inside its own transaction, and reconciliation runs only
while ``dirty_seq > reconciled_seq``. Like that watermark it is mutable
operational state — not an append-only receipt — so it carries no immutability
trigger.

Revision ID: b7f3a1c9d2e4
Revises: 86edb31fd81a
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7f3a1c9d2e4"
down_revision: Union[str, Sequence[str], None] = "86edb31fd81a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "revision_reconciliation_requests",
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
            name="ck_revision_reconciliation_requests_non_negative",
        ),
        sa.CheckConstraint(
            "reconciled_seq <= dirty_seq",
            name="ck_revision_reconciliation_requests_watermark_order",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("project_id"),
    )


def downgrade() -> None:
    op.drop_table("revision_reconciliation_requests")
