"""A record can be dismissed with a reason (ADR-0032).

Junk reaches the record — a duplicate, a row that is not a conflict at
all — and a reviewer needs it off their list without it leaving history.
Dismissal is append-only like every other decision, and the column on
`dependencies` is the projection readers filter on.

Revision ID: e3a97c15b402
Revises: d8f1a4b72e69
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3a97c15b402"
down_revision: Union[str, Sequence[str], None] = "d8f1a4b72e69"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dependencies",
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "dependency_dismissals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "dependency_id",
            sa.BigInteger(),
            sa.ForeignKey("dependencies.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("dismissed_by", sa.Text(), nullable=False),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason in ('duplicate', 'not-a-conflict', 'wrong')",
            name="ck_dependency_dismissals_reason",
        ),
        sa.CheckConstraint(
            "length(trim(dismissed_by)) > 0",
            name="ck_dependency_dismissals_attributable",
        ),
    )
    op.create_index(
        "ix_dependency_dismissals_dependency",
        "dependency_dismissals",
        ["dependency_id"],
    )
    op.execute(
        """
        create function reject_dependency_dismissal_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            raise exception 'dependency_dismissals is append-only'
                using errcode = '23514';
        end;
        $$;

        create trigger dependency_dismissals_are_immutable
        before update or delete on dependency_dismissals
        for each row execute function reject_dependency_dismissal_mutation();

        create trigger dependency_dismissals_reject_truncate
        before truncate on dependency_dismissals
        execute function reject_dependency_dismissal_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        drop trigger if exists dependency_dismissals_are_immutable
            on dependency_dismissals;
        drop trigger if exists dependency_dismissals_reject_truncate
            on dependency_dismissals;
        """
    )
    op.drop_index(
        "ix_dependency_dismissals_dependency",
        table_name="dependency_dismissals",
    )
    op.drop_table("dependency_dismissals")
    op.execute("drop function if exists reject_dependency_dismissal_mutation()")
    op.drop_column("dependencies", "dismissed_at")
