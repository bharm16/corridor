"""Freeze the project-language actor on External Report release receipts.

Revision ID: f255b7c4d9e3
Revises: e255a7c4d9e2

The raw principal remains the audit identity.  This nullable companion stores
the roster label seen at release time so later roster edits cannot rewrite
history; null is reserved for receipts created before this context existed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f255b7c4d9e3"
down_revision: Union[str, Sequence[str], None] = "e255a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "external_report_releases",
        sa.Column("released_by_display", sa.Text(), nullable=True),
    )
    op.execute(
        """
        alter table external_report_releases
        add constraint ck_external_report_releases_released_by_display
        check (
            released_by_display is not null
            and length(trim(released_by_display)) > 0
        ) not valid
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade frozen External Report release actors: immutable "
        "project-language history would lose its release-time identity"
    )
