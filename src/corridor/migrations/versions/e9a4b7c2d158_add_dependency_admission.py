"""add dependency admission policy, runs, and outcomes

Revision ID: e9a4b7c2d158
Revises: c7d2f5a83b46
Create Date: 2026-08-07 19:55:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "e9a4b7c2d158"
down_revision: Union[str, Sequence[str], None] = "c7d2f5a83b46"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """ADR-0027: dependencies enter by policy when revisions agree exactly."""
    op.create_table(
        "dependency_admission_policy_approvals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("policy_json", JSONB(), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id",
            "id",
            name="uq_dependency_admission_policy_project_id",
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_dependency_admission_policy_sha256",
        ),
    )

    op.create_table(
        "dependency_admission_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("policy_approval_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "abstention_reason_version", sa.String(length=64), nullable=False
        ),
        sa.Column("admitted_count", sa.Integer(), nullable=False),
        sa.Column("abstained_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "policy_approval_id"],
            [
                "dependency_admission_policy_approvals.project_id",
                "dependency_admission_policy_approvals.id",
            ],
            name="fk_dependency_admission_run_policy_project",
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_dependency_admission_run_sha256",
        ),
        sa.CheckConstraint(
            "admitted_count >= 0 and abstained_count >= 0",
            name="ck_dependency_admission_run_counts",
        ),
    )

    op.create_table(
        "dependency_admission_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "dependency_admission_run_id",
            sa.BigInteger(),
            sa.ForeignKey("dependency_admission_runs.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "candidate_id",
            sa.BigInteger(),
            sa.ForeignKey("candidates.id"),
            nullable=False,
        ),
        sa.Column("outcome", sa.String(length=9), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column(
            "dependency_id",
            sa.BigInteger(),
            sa.ForeignKey("dependencies.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome in ('admitted', 'merged', 'abstained')",
            name="ck_dependency_admission_outcome_value",
        ),
        sa.CheckConstraint(
            "("
            "outcome in ('admitted', 'merged') and reason is null "
            "and dependency_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and dependency_id is null"
            ")",
            name="ck_dependency_admission_outcome_kind",
        ),
    )

    for table in (
        "dependency_admission_policy_approvals",
        "dependency_admission_runs",
        "dependency_admission_outcomes",
    ):
        op.execute(
            f"""
            create function enforce_{table}()
            returns trigger
            language plpgsql
            as $$
            begin
                if tg_op = 'INSERT' then
                    return new;
                end if;
                raise exception '{table} are immutable'
                    using errcode = '23514';
            end;
            $$;

            create trigger {table}_are_immutable
            before update or delete
            on {table}
            for each row execute function enforce_{table}();

            create trigger {table}_reject_truncate
            before truncate on {table}
            for each statement execute function enforce_{table}();
            """
        )


def downgrade() -> None:
    for table in (
        "dependency_admission_outcomes",
        "dependency_admission_runs",
        "dependency_admission_policy_approvals",
    ):
        op.execute(
            f"""
            drop trigger {table}_reject_truncate on {table};
            drop trigger {table}_are_immutable on {table};
            drop function enforce_{table}();
            """
        )
        op.drop_table(table)
