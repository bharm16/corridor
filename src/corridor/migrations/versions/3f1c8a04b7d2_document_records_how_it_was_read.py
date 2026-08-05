"""a document records how it was read

Revision ID: 3f1c8a04b7d2
Revises: b41c07d29e5a
Create Date: 2026-08-04 20:41:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '3f1c8a04b7d2'
down_revision: Union[str, Sequence[str], None] = 'b41c07d29e5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Nullable with no default: null means "not extracted at this version",
    which is what every existing row is. An empty dict would claim the
    document was read and produced no tiers.
    """
    op.add_column(
        'documents',
        sa.Column('extraction_tiers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'documents',
        sa.Column('header_disagreements', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'header_disagreements')
    op.drop_column('documents', 'extraction_tiers')
