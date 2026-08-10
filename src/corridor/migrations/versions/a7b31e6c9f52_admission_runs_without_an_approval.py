"""Admission runs need no approval row (ADR-0029).

The admission families no longer wait for a human authorization: the
policies run as a pipeline stage when documents land, and the run's own
``policy_version`` and ``policy_sha256`` carry the replay claim that the
approval used to carry alongside them.

Automatic Carry-Forward still authorizes, and its own binding triggers
still read ``policy_approval_id`` — so the column stays, and only its
NOT NULL goes. The composite foreign key is MATCH SIMPLE, so a null
approval simply is not checked against ``policy_approvals``, while every
row that does name one is constrained exactly as before.

Revision ID: a7b31e6c9f52
Revises: f2c8d94ab371
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7b31e6c9f52"
down_revision: Union[str, Sequence[str], None] = "f2c8d94ab371"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "policy_runs",
        "policy_approval_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )


def downgrade() -> None:
    # A run recorded without an approval cannot be given one after the
    # fact, so the column can only go back to NOT NULL where none exists.
    op.alter_column(
        "policy_runs",
        "policy_approval_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
