"""add immutable cohort receipts

Revision ID: e8b3f61c9d24
Revises: d5e9c47a1b83
Create Date: 2026-08-07 13:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "e8b3f61c9d24"
down_revision: Union[str, Sequence[str], None] = "d5e9c47a1b83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """The rehearsal cohort is a pinned set with a checkable digest (#173)."""
    op.create_table(
        "cohort_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column(
            "revision_comparison_run_id",
            sa.BigInteger(),
            sa.ForeignKey("revision_comparison_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "predecessor_extraction_run_id", sa.BigInteger(), nullable=False
        ),
        sa.Column(
            "successor_extraction_run_id", sa.BigInteger(), nullable=False
        ),
        sa.Column("external_org", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("matcher_version", sa.String(length=64), nullable=False),
        sa.Column("members", JSONB(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "revision_comparison_run_id",
            "rule_version",
            "external_org",
            name="uq_cohort_receipts_one_per_rule",
        ),
    )
    op.execute(
        """
        create function enforce_cohort_receipt()
        returns trigger
        language plpgsql
        as $$
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            raise exception 'Cohort receipts are immutable'
                using errcode = '23514';
        end;
        $$;

        create trigger cohort_receipts_are_immutable
        before update or delete
        on cohort_receipts
        for each row execute function enforce_cohort_receipt();

        create trigger cohort_receipts_reject_truncate
        before truncate on cohort_receipts
        for each statement execute function enforce_cohort_receipt();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        drop trigger cohort_receipts_reject_truncate on cohort_receipts;
        drop trigger cohort_receipts_are_immutable on cohort_receipts;
        drop function enforce_cohort_receipt();
        """
    )
    op.drop_table("cohort_receipts")
