"""Add immutable eligibility and protection history for statement suggestions.

Deterministic scope suggestions are a read aid, never a scope decision.  Their
visibility must not contaminate either an investigator shadow cohort or its
no-agent baseline, so both the positive eligibility declaration and every
protection window are retained.  An outcome capture cannot silently end a
window; that conclusion is a separate append-only row.

Revision ID: b5d1e2f3a5c6
Revises: b4d1e2f3a5c6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5d1e2f3a5c6"
down_revision: Union[str, Sequence[str], None] = "c347a5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "statement_suggestion_eligibility_declarations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("contract_version", sa.String(length=128), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(trim(contract_version)) > 0", name="ck_statement_suggestion_eligibility_contract"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", name="uq_statement_suggestion_eligibility_candidate"),
    )
    op.create_index("ix_statement_suggestion_eligibility_declarations_project_id", "statement_suggestion_eligibility_declarations", ["project_id"])
    op.create_index("ix_statement_suggestion_eligibility_declarations_candidate_id", "statement_suggestion_eligibility_declarations", ["candidate_id"])
    op.create_table(
        "statement_suggestion_protections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("observation_contract", sa.String(length=128), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind in ('shadow_cohort', 'no_agent_baseline')", name="ck_statement_suggestion_protection_kind"),
        sa.CheckConstraint("length(trim(observation_contract)) > 0", name="ck_statement_suggestion_protection_contract"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "kind", "observation_contract", name="uq_statement_suggestion_protection_window"),
    )
    op.create_index("ix_statement_suggestion_protections_project_id", "statement_suggestion_protections", ["project_id"])
    op.create_index("ix_statement_suggestion_protections_candidate_id", "statement_suggestion_protections", ["candidate_id"])
    op.create_table(
        "statement_suggestion_protection_ends",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("protection_id", sa.BigInteger(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["protection_id"], ["statement_suggestion_protections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("protection_id", name="uq_statement_suggestion_protection_end"),
    )
    op.create_index("ix_statement_suggestion_protection_ends_protection_id", "statement_suggestion_protection_ends", ["protection_id"])
    op.execute(
        """
        create function reject_statement_suggestion_protection_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'statement suggestion protection history is append-only';
        end;
        $$
        """
    )
    for table in (
        "statement_suggestion_eligibility_declarations",
        "statement_suggestion_protections",
        "statement_suggestion_protection_ends",
    ):
        op.execute(
            f"create trigger {table}_immutable before update or delete on {table} "
            "for each row execute function reject_statement_suggestion_protection_mutation()"
        )
        op.execute(
            f"create trigger {table}_reject_truncate before truncate on {table} "
            "for each statement execute function reject_statement_suggestion_protection_mutation()"
        )


def downgrade() -> None:
    raise RuntimeError("cannot erase statement-suggestion eligibility or protection history")
