"""add work decision receipts

Revision ID: c3f8a25d7e91
Revises: b7d4e91f3a52
Create Date: 2026-08-07 03:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f8a25d7e91"
down_revision: Union[str, Sequence[str], None] = "b7d4e91f3a52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A Work Decision is a typed immutable receipt (ADR-0025).

    The generic audit log is explicitly insufficient: the receipt carries
    the recording principal, the timestamp, the exact before/after values
    and its predecessor, as one linear chain per Dependency and field.
    """
    op.create_table(
        "work_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "dependency_id",
            sa.BigInteger(),
            sa.ForeignKey("dependencies.id"),
            nullable=False,
        ),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("before_value", sa.Text(), nullable=True),
        sa.Column("after_value", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("predecessor_decision_id", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint(
            "dependency_id", "id", name="uq_work_decisions_dependency_id_id"
        ),
        sa.ForeignKeyConstraint(
            ["dependency_id", "predecessor_decision_id"],
            ["work_decisions.dependency_id", "work_decisions.id"],
            name="fk_work_decisions_predecessor",
        ),
        sa.UniqueConstraint(
            "predecessor_decision_id", name="uq_work_decisions_predecessor"
        ),
    )
    op.create_index(
        "uq_work_decisions_one_root",
        "work_decisions",
        ["dependency_id", "field"],
        unique=True,
        postgresql_where=sa.text("predecessor_decision_id is null"),
    )
    op.execute(
        """
        create function enforce_work_decision()
        returns trigger
        language plpgsql
        as $$
        declare
            demo_dependency boolean;
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            -- The one sanctioned escape, identical to Candidate lineage
            -- and Active Run declarations: the isolated demo project
            -- resets itself, and only itself.
            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from dependencies
                      join projects on projects.id = dependencies.project_id
                     where dependencies.id = old.dependency_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_dependency;
                if demo_dependency then
                    return old;
                end if;
            end if;
            raise exception 'Work Decision receipts are immutable'
                using errcode = '23514';
        end;
        $$;

        create trigger work_decisions_are_immutable
        before update or delete
        on work_decisions
        for each row execute function enforce_work_decision();

        create trigger work_decisions_reject_truncate
        before truncate on work_decisions
        for each statement execute function enforce_work_decision();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        drop trigger work_decisions_reject_truncate on work_decisions;
        drop trigger work_decisions_are_immutable on work_decisions;
        drop function enforce_work_decision();
        """
    )
    op.drop_index("uq_work_decisions_one_root", table_name="work_decisions")
    op.drop_table("work_decisions")
