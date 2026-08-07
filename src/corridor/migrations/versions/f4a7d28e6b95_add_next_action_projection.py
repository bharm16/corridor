"""add the Next Action projection columns

Revision ID: f4a7d28e6b95
Revises: e8b3f61c9d24
Create Date: 2026-08-07 13:55:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a7d28e6b95"
down_revision: Union[str, Sequence[str], None] = "e8b3f61c9d24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Queryable current values; the Work Decision receipts are the record."""
    op.add_column("dependencies", sa.Column("next_action", sa.Text(), nullable=True))
    op.add_column(
        "dependencies", sa.Column("action_due_date", sa.Date(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("dependencies", "action_due_date")
    op.drop_column("dependencies", "next_action")
