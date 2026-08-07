"""add document quarantines and the quarantined extraction outcome

Revision ID: d5e9c47a1b83
Revises: c3f8a25d7e91
Create Date: 2026-08-07 03:50:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e9c47a1b83"
down_revision: Union[str, Sequence[str], None] = "c3f8a25d7e91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Out of scope means unsupported, never lossy (#149).

    The quarantine row is the durable project-level outcome; the
    'quarantined' run outcome is the extraction-side receipt for a
    document refused whole on detected sequencing semantics.
    """
    op.create_table(
        "document_quarantines",
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id"),
            primary_key=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # The outcome vocabulary is a VARCHAR plus a CHECK, not a native
    # PG type (models._enum) — a value change is a one-line constraint swap.
    op.drop_constraint("extraction_outcome", "extraction_runs", type_="check")
    # The VARCHAR was sized to the longest value at creation; 'quarantined'
    # is one character longer than 'unreadable'.
    op.alter_column(
        "extraction_runs", "outcome", type_=sa.String(length=11)
    )
    op.create_check_constraint(
        "extraction_outcome",
        "extraction_runs",
        "outcome in ('completed', 'failed', 'unreadable', 'no_matrix', "
        "'quarantined')",
    )


def downgrade() -> None:
    op.drop_constraint("extraction_outcome", "extraction_runs", type_="check")
    op.create_check_constraint(
        "extraction_outcome",
        "extraction_runs",
        "outcome in ('completed', 'failed', 'unreadable', 'no_matrix')",
    )
    op.alter_column(
        "extraction_runs", "outcome", type_=sa.String(length=10)
    )
    op.drop_table("document_quarantines")
