"""Bind Dependency Admission Abstentions to their exact evaluated inputs.

Historical outcomes remain readable with null eligibility fields. New policy
runs use the fields to suppress only an unchanged Candidate/group verdict;
the append-only outcome table still refuses every update and delete.

Revision ID: e314a3d8c6f2
Revises: d257f2b9c537
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "e314a3d8c6f2"
down_revision: Union[str, Sequence[str], None] = "d257f2b9c537"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dependency_admission_outcomes",
        sa.Column("eligibility_json", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.add_column(
        "dependency_admission_outcomes",
        sa.Column("eligibility_sha256", sa.String(length=64)),
    )


def downgrade() -> None:
    raise RuntimeError("cannot discard Dependency Admission eligibility receipts")
