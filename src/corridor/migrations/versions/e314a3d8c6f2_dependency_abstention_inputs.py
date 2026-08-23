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
    op.create_check_constraint(
        "ck_dependency_admission_outcome_eligibility_sha256",
        "dependency_admission_outcomes",
        "eligibility_sha256 is null or "
        "eligibility_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_dependency_admission_outcome_eligibility_shape",
        "dependency_admission_outcomes",
        "(outcome = 'abstained' and "
        "((eligibility_json is null and eligibility_sha256 is null) or "
        "(eligibility_json is not null and eligibility_sha256 is not null))) "
        "or (outcome in ('admitted', 'merged') and "
        "eligibility_json is null and eligibility_sha256 is null)",
    )


def downgrade() -> None:
    raise RuntimeError("cannot discard Dependency Admission eligibility receipts")
