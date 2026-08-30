"""Add selector-driven documentation fields and immutable confirmations.

ADR-0052 retires the free-text closure requirement from public behavior.  It
does not rewrite the old per-passage sufficiency history: the new checklist
reader decides when that history remains effective.  This migration adds only
the source-derived cost selector and the append-only cited human confirmation
needed for the one interpretation field.

Revision ID: c347a5c6d7e8
Revises: b4d1e2f3a5c6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c347a5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "c346a6d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dependencies",
        sa.Column("cost_responsibility", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "documentation_field_confirmations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        sa.Column("evidence_link_id", sa.BigInteger(), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=64), nullable=False),
        sa.Column("conclusion", sa.String(length=64), nullable=False),
        sa.Column("confirmed_by", sa.Text(), nullable=False),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "field_name = 'approval_interpretation'",
            name="ck_documentation_confirmation_known_field",
        ),
        sa.CheckConstraint(
            "classification in ('approved', 'conditional')",
            name="ck_documentation_confirmation_known_classification",
        ),
        sa.CheckConstraint(
            "conclusion = 'approved'",
            name="ck_documentation_confirmation_known_conclusion",
        ),
        sa.CheckConstraint(
            "length(trim(confirmed_by)) > 0",
            name="ck_documentation_confirmation_actor",
        ),
        sa.ForeignKeyConstraint(["dependency_id"], ["dependencies.id"]),
        sa.ForeignKeyConstraint(
            ["dependency_id", "evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_documentation_confirmation_owned_evidence",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_documentation_confirmation_dependency_field",
        "documentation_field_confirmations",
        ["dependency_id", "field_name", "id"],
    )
    op.execute(
        """
        create function reject_documentation_confirmation_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'documentation field confirmations are append-only';
        end;
        $$;
        create trigger documentation_field_confirmations_are_immutable
        before update or delete on documentation_field_confirmations
        for each row execute function reject_documentation_confirmation_mutation();
        create trigger documentation_field_confirmations_reject_truncate
        before truncate on documentation_field_confirmations
        for each statement execute function reject_documentation_confirmation_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from documentation_field_confirmations) then
                raise exception 'cannot erase documentation confirmation history';
            end if;
        end
        $$;
        drop trigger if exists documentation_field_confirmations_are_immutable
            on documentation_field_confirmations;
        drop trigger if exists documentation_field_confirmations_reject_truncate
            on documentation_field_confirmations;
        drop function if exists reject_documentation_confirmation_mutation();
        """
    )
    op.drop_index(
        "ix_documentation_confirmation_dependency_field",
        table_name="documentation_field_confirmations",
    )
    op.drop_table("documentation_field_confirmations")
    op.drop_column("dependencies", "cost_responsibility")
