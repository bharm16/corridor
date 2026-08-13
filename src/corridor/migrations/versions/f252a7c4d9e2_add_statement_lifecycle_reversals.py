"""add append-only guided statement reversals

Revision ID: f252a7c4d9e2
Revises: e251a7c4d9e2

Guided Save receipts established the exact grouping identity.  This successor
adds the independent Candidate disposition and compensating reversal records
needed to make that identity reversible without editing any source fact.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f252a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "e251a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_dispositions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "disposition in ('accepted', 'not_relevant')",
            name="ck_candidate_dispositions_kind",
        ),
        sa.CheckConstraint(
            "(disposition = 'accepted' and reason is null) or "
            "(disposition = 'not_relevant' and reason is not null)",
            name="ck_candidate_dispositions_reason",
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_candidate_dispositions_actor",
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column(
        "statement_coordination_receipts",
        sa.Column("candidate_disposition_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_statement_coordination_receipt_disposition",
        "statement_coordination_receipts",
        "candidate_dispositions",
        ["candidate_disposition_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_statement_coordination_receipt_disposition",
        "statement_coordination_receipts",
        ["candidate_disposition_id"],
    )
    op.drop_constraint(
        "statement_coordination_receipts_candidate_id_key",
        "statement_coordination_receipts",
        type_="unique",
    )
    op.create_table(
        "statement_coordination_reversals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=True),
        sa.Column("candidate_disposition_id", sa.BigInteger(), nullable=True),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("audit_log_id", sa.BigInteger(), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(receipt_id is not null and candidate_disposition_id is null) or "
            "(receipt_id is null and candidate_disposition_id is not null)",
            name="ck_statement_coordination_reversals_one_source",
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_statement_coordination_reversals_actor",
        ),
        sa.ForeignKeyConstraint(["audit_log_id"], ["audit_log.id"]),
        sa.ForeignKeyConstraint(["candidate_disposition_id"], ["candidate_dispositions.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["receipt_id"], ["statement_coordination_receipts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("audit_log_id"),
        sa.UniqueConstraint("candidate_disposition_id"),
        sa.UniqueConstraint("receipt_id"),
    )
    op.create_table(
        "statement_coordination_reversal_effects",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("reversal_id", sa.BigInteger(), nullable=False),
        sa.Column("effect_kind", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "effect_kind in ("
            "'statement', 'scope_decision', 'work_decision', 'milestone_link', "
            "'candidate_disposition', 'candidate_projection', 'lineage_projection', "
            "'audit_pointer', 'grouping_receipt'"
            ")",
            name="ck_statement_coordination_reversal_effects_kind",
        ),
        sa.ForeignKeyConstraint(
            ["reversal_id"], ["statement_coordination_reversals.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reversal_id",
            "effect_kind",
            "target_id",
            name="uq_statement_coordination_reversal_effect",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade statement lifecycle: append-only reversal history "
        "cannot be safely removed"
    )
