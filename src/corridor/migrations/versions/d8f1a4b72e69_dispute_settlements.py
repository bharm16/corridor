"""Disputes can be settled (ADR-0031).

A Dispute is two revisions asserting different verified values for one
field. It rides on the row rather than withholding it, so the record
needs somewhere to say what a reviewer concluded — without erasing the
losing claim, which stays an Assertion like every other.

Revision ID: d8f1a4b72e69
Revises: c4e8f2a91d37
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8f1a4b72e69"
down_revision: Union[str, Sequence[str], None] = "c4e8f2a91d37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dispute_settlements",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "dependency_id",
            sa.BigInteger(),
            sa.ForeignKey("dependencies.id"),
            nullable=False,
        ),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("settled_value", sa.Text(), nullable=True),
        sa.Column("settled_by", sa.Text(), nullable=False),
        sa.Column("covers_assertion_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "settled_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(settled_by)) > 0",
            name="ck_dispute_settlements_attributable",
        ),
    )
    op.create_index(
        "ix_dispute_settlements_dependency_field",
        "dispute_settlements",
        ["dependency_id", "field_name"],
    )
    # Settlements are decisions, and decisions append. The same trigger
    # shape the policy tables use, so a settlement cannot be edited into
    # having covered a claim it never saw.
    op.execute(
        """
        create function reject_dispute_settlement_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            raise exception 'dispute_settlements is append-only'
                using errcode = '23514';
        end;
        $$;

        create trigger dispute_settlements_are_immutable
        before update or delete on dispute_settlements
        for each row execute function reject_dispute_settlement_mutation();

        create trigger dispute_settlements_reject_truncate
        before truncate on dispute_settlements
        execute function reject_dispute_settlement_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        drop trigger if exists dispute_settlements_are_immutable
            on dispute_settlements;
        drop trigger if exists dispute_settlements_reject_truncate
            on dispute_settlements;
        """
    )
    op.drop_index(
        "ix_dispute_settlements_dependency_field",
        table_name="dispute_settlements",
    )
    op.drop_table("dispute_settlements")
    op.execute("drop function if exists reject_dispute_settlement_mutation()")
