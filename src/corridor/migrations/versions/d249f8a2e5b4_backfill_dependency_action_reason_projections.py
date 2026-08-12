"""backfill Dependency Action Due Date reason projections

Revision ID: d249f8a2e5b4
Revises: c249d7e1f4a3

``b249`` already recorded a structured unknown-date reason on every new Next
Action receipt, but its Dependency projection had no matching column.  The
``c249`` column addition must therefore reconstruct current values from the
append-only chain before later writes can enforce projection-tail equality.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "d249f8a2e5b4"
down_revision: Union[str, Sequence[str], None] = "c249d7e1f4a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        update dependencies dependency
        set action_due_date_reason = tail.action_due_date_reason
        from work_decisions tail
        where tail.dependency_id = dependency.id
          and tail.field = 'next_action'
          and tail.after_value is not null
          and not exists (
              select 1 from work_decisions successor
              where successor.predecessor_decision_id = tail.id
          )
          and dependency.next_action is not null
          and dependency.action_due_date is null
          and tail.action_due_date_reason is not null
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade statement contract: reconstructed Dependency "
        "unknown-date projections cannot be round-tripped safely"
    )
