"""Retain the configured inputs of each External Report Evaluation.

Revision ID: a255a7c4d9e2
Revises: f255a7c4d9e2

The initial PDF receipt has separate date and ruleset columns.  This linear
successor adds the exact threshold map as well, so an External Report can be
read without guessing which Evaluation configuration produced it.  Any
pre-successor receipt is honestly marked as having no recoverable threshold
map rather than being silently assigned current defaults.
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
    op.execute(
        """
        update external_report_releases
           set evaluation_context_json = jsonb_build_object(
               'evaluated_on', evaluated_on::text,
               'ruleset_version', ruleset_version,
               'thresholds', null,
               'legacy_unrecorded', true
           )
         where evaluation_context_json is null
        """
    )
    op.alter_column("external_report_releases", "evaluation_context_json", nullable=False)
    op.create_check_constraint(
        "ck_external_report_releases_evaluation_object",
        "external_report_releases",
        "jsonb_typeof(evaluation_context_json) = 'object'",
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade External Report release evaluation context: "
        "immutable receipt inputs would be lost"
    )
