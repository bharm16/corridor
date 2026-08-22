"""Record ordinary Candidate review before any shadow freeze.

Revision ID: f256b8d5e1f3
Revises: e256a7c4d9e2
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f256b8d5e1f3"
down_revision: Union[str, Sequence[str], None] = "e256a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_investigation_candidate_review_starts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "candidate_id",
            sa.BigInteger(),
            sa.ForeignKey("candidates.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("principal", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_evidence_investigation_candidate_review_starts_project_id",
        "evidence_investigation_candidate_review_starts",
        ["project_id"],
    )
    op.create_index(
        "ix_evidence_investigation_candidate_review_starts_candidate_id",
        "evidence_investigation_candidate_review_starts",
        ["candidate_id"],
        unique=True,
    )
    op.execute(
        "create trigger evidence_investigation_candidate_review_starts_append_only "
        "before update or delete on evidence_investigation_candidate_review_starts "
        "for each row execute function refuse_evidence_investigation_receipt_mutation()"
    )


def downgrade() -> None:
    raise RuntimeError("cannot erase immutable pre-shadow review starts")
