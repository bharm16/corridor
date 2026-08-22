"""Add immutable Evidence Investigation evaluation receipts.

Revision ID: c256e0f7a3b6
Revises: b256d9e6f2a5
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c256e0f7a3b6"
down_revision: Union[str, Sequence[str], None] = "b256d9e6f2a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_investigation_evaluation_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False, unique=True),
        sa.Column("evaluation_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("selected_run_ids_json", postgresql.JSONB(), nullable=False),
        sa.Column("identity_json", postgresql.JSONB(), nullable=False),
        sa.Column("metrics_json", postgresql.JSONB(), nullable=False),
        sa.Column("strata_json", postgresql.JSONB(), nullable=False),
        sa.Column("human_scores_json", postgresql.JSONB(), nullable=False),
        sa.Column("gates_json", postgresql.JSONB(), nullable=False),
        sa.Column("limitations_json", postgresql.JSONB(), nullable=False),
        sa.Column("summary_markdown", sa.Text(), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "create trigger evidence_investigation_evaluation_receipts_append_only "
        "before update or delete on evidence_investigation_evaluation_receipts "
        "for each row execute function refuse_evidence_investigation_receipt_mutation()"
    )


def downgrade() -> None:
    raise RuntimeError("cannot downgrade immutable Evidence Investigation evaluations")
