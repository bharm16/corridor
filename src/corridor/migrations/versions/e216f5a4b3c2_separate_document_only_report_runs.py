"""Keep document-only report snapshots in their own comparison lineage.

Revision ID: e216f5a4b3c2
Revises: d216e4f3a2b1
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e216f5a4b3c2"
down_revision: Union[str, Sequence[str], None] = "d216e4f3a2b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Mark ordinary historical runs explicitly before adding doc-only runs."""
    op.add_column(
        "report_runs",
        sa.Column(
            "document_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("report_runs", "document_only")
