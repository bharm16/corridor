"""add event admission policy, runs, outcomes, and project-side parties

Revision ID: c7d2f5a83b46
Revises: b3e8a92f4c17
Create Date: 2026-08-07 19:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "c7d2f5a83b46"
down_revision: Union[str, Sequence[str], None] = "b3e8a92f4c17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """ADR-0026's machinery: an authorized policy, receipts, abstention."""
    op.add_column(
        "projects",
        sa.Column(
            "project_side_parties",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # When they said it and what they promised are two dates; only the
    # second can project a Committed Date.
    op.add_column(
        "dependency_events", sa.Column("committed_date", sa.Date(), nullable=True)
    )

    op.create_table(
        "event_admission_policy_approvals",
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
            "project_id", "id", name="uq_event_admission_policy_project_id"
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_event_admission_policy_sha256",
        ),
    )

    op.create_table(
        "event_admission_runs",
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
                "event_admission_policy_approvals.project_id",
                "event_admission_policy_approvals.id",
            ],
            name="fk_event_admission_run_policy_project",
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_event_admission_run_sha256",
        ),
        sa.CheckConstraint(
            "admitted_count >= 0 and abstained_count >= 0",
            name="ck_event_admission_run_counts",
        ),
    )

    op.create_table(
        "event_admission_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "event_admission_run_id",
            sa.BigInteger(),
            sa.ForeignKey("event_admission_runs.id"),
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
            "dependency_event_id",
            sa.BigInteger(),
            sa.ForeignKey("dependency_events.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome in ('admitted', 'abstained')",
            name="ck_event_admission_outcome_value",
        ),
        sa.CheckConstraint(
            "("
            "outcome = 'admitted' and reason is null "
            "and dependency_event_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and dependency_event_id is null"
            ")",
            name="ck_event_admission_outcome_kind",
        ),
    )

    # The approvals, receipts and outcomes are history: immutable at the
    # database, exactly as every sibling receipt table is.
    for table in (
        "event_admission_policy_approvals",
        "event_admission_runs",
        "event_admission_outcomes",
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
        "event_admission_outcomes",
        "event_admission_runs",
        "event_admission_policy_approvals",
    ):
        op.execute(
            f"""
            drop trigger {table}_reject_truncate on {table};
            drop trigger {table}_are_immutable on {table};
            drop function enforce_{table}();
            """
        )
        op.drop_table(table)
    op.drop_column("dependency_events", "committed_date")
    op.drop_column("projects", "project_side_parties")
