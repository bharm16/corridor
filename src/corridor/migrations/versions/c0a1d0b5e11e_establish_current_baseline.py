"""Establish the current baseline after the released head.

Revision ID: c0a1d0b5e11e
Revises: b7d3f9a1c2e5
"""

from pathlib import Path

from alembic import op


revision = "c0a1d0b5e11e"
down_revision = "b7d3f9a1c2e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Canonicalize equivalent checks on released-head databases."""

    sql = Path(__file__).with_name("c0a1d0b5e11e_normalize_checks.sql").read_text(
        encoding="utf-8"
    )
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    """The consolidated baseline has no supported predecessor."""

    raise RuntimeError("baseline downgrade is unsupported")
