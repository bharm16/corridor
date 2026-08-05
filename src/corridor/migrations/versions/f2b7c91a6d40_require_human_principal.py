"""require a stable human principal for Admission

Revision ID: f2b7c91a6d40
Revises: 4b94d3f5b8b1
Create Date: 2026-08-05 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2b7c91a6d40"
down_revision: Union[str, Sequence[str], None] = "4b94d3f5b8b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Preserve legacy actor labels while recording new human identity exactly."""
    op.add_column("audit_log", sa.Column("human_principal", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "human_principal")
