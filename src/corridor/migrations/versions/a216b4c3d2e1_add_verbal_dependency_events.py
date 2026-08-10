"""Add explicit, immutable verbal DependencyEvents.

Revision ID: a216b4c3d2e1
Revises: e3a97c15b402
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a216b4c3d2e1"
down_revision: Union[str, Sequence[str], None] = "e3a97c15b402"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    source_kind = sa.Enum(
        "cited",
        "verbal",
        name="event_source_kind",
        native_enum=False,
        create_constraint=True,
    )
    op.add_column(
        "dependency_events",
        sa.Column(
            "source_kind",
            source_kind,
            nullable=False,
            server_default="cited",
        ),
    )
    op.add_column(
        "dependency_events", sa.Column("stated_party", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("dependency_events", "stated_party")
    op.drop_column("dependency_events", "source_kind")
