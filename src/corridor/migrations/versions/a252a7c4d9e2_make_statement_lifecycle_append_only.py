"""make guided statement lifecycle receipts append-only

Revision ID: a252a7c4d9e2
Revises: f252a7c4d9e2

The initial lifecycle tables establish their relational shape.  This linear
successor makes their history authority enforceable: a correction or Undo can
only append a new record, never overwrite or remove an earlier one.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a252a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "f252a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = (
    "candidate_dispositions",
    "statement_coordination_receipts",
    "statement_coordination_reversals",
    "statement_coordination_reversal_effects",
)


def upgrade() -> None:
    op.execute(
        """
        create function reject_statement_lifecycle_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'Guided statement lifecycle history is append-only'
                using errcode = '23514';
        end;
        $$;
        """
    )
    for table in TABLES:
        op.execute(
            f"""
            create trigger {table}_are_immutable
            before update or delete on {table}
            for each row execute function reject_statement_lifecycle_mutation();
            """
        )
        op.execute(
            f"""
            create trigger {table}_reject_truncate
            before truncate on {table}
            for each statement execute function reject_statement_lifecycle_mutation();
            """
        )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade statement lifecycle: append-only history must remain intact"
    )
