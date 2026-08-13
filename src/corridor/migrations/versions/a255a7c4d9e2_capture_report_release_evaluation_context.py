"""Retain the configured inputs of each External Report Evaluation.

Revision ID: a255a7c4d9e2
Revises: f255a7c4d9e2

The initial PDF receipt has separate date and ruleset columns.  This linear
successor adds a nullable threshold map without rewriting those immutable
receipts.  Future releases record the exact Evaluation inputs; a receipt
created before this successor remains honestly unexpanded rather than being
silently assigned current defaults.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a255a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "f255a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "external_report_releases",
        sa.Column(
            "evaluation_context_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_external_report_releases_evaluation_object",
        "external_report_releases",
        "evaluation_context_json is null or "
        "jsonb_typeof(evaluation_context_json) = 'object'",
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade External Report release evaluation context: "
        "immutable receipt inputs would be lost"
    )
