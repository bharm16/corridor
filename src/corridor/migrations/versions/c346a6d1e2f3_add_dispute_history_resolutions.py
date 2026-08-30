"""Record ADR-0061 chronology outcomes without impersonating a human decision.

Revision ID: c346a6d1e2f3
Revises: b4d1e2f3a5c6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c346a6d1e2f3"
down_revision: Union[str, Sequence[str], None] = "b4d1e2f3a5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dispute_history_resolutions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "dependency_id",
            sa.BigInteger(),
            sa.ForeignKey("dependencies.id"),
            nullable=False,
        ),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column(
            "older_assertion_id",
            sa.BigInteger(),
            sa.ForeignKey("assertions.id"),
            nullable=False,
        ),
        sa.Column(
            "newer_assertion_id",
            sa.BigInteger(),
            sa.ForeignKey("assertions.id"),
            nullable=False,
        ),
        sa.Column("covers_assertion_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome in ('physical_superseded', 'contractual_amendment')",
            name="ck_dispute_history_resolutions_outcome",
        ),
        sa.CheckConstraint(
            "older_assertion_id <> newer_assertion_id",
            name="ck_dispute_history_resolutions_distinct_assertions",
        ),
        sa.CheckConstraint(
            "length(trim(rule_version)) > 0",
            name="ck_dispute_history_resolutions_rule_version",
        ),
        sa.UniqueConstraint(
            "dependency_id",
            "field_name",
            "covers_assertion_id",
            name="uq_dispute_history_resolutions_coverage",
        ),
    )
    op.create_index(
        "ix_dispute_history_resolutions_dependency_field",
        "dispute_history_resolutions",
        ["dependency_id", "field_name"],
    )
    op.execute(
        """
        create function reject_dispute_history_resolution_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'dispute_history_resolutions is append-only';
        end;
        $$;
        create trigger dispute_history_resolutions_are_immutable
        before update or delete on dispute_history_resolutions
        for each row execute function reject_dispute_history_resolution_mutation();
        create trigger dispute_history_resolutions_reject_truncate
        before truncate on dispute_history_resolutions
        for each statement execute function reject_dispute_history_resolution_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from dispute_history_resolutions) then
                raise exception 'cannot erase dispute chronology history';
            end if;
        end
        $$;
        drop trigger if exists dispute_history_resolutions_are_immutable
            on dispute_history_resolutions;
        drop trigger if exists dispute_history_resolutions_reject_truncate
            on dispute_history_resolutions;
        drop function if exists reject_dispute_history_resolution_mutation();
        """
    )
    op.drop_index(
        "ix_dispute_history_resolutions_dependency_field",
        table_name="dispute_history_resolutions",
    )
    op.drop_table("dispute_history_resolutions")
