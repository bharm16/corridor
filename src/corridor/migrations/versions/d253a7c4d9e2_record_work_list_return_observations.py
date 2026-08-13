"""Record which statement state made a delayed Work Item safe to defer.

Revision ID: d253a7c4d9e2
Revises: c253a7c4d9e2

Current Coordination Plans can leave immediate work only while the External
Party fact they answered has not changed.  These immutable receipt fields name
the statement, scope, and Milestone Impact state observed at that decision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d253a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "c253a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("work_decisions", sa.Column("observed_statement_event_id", sa.BigInteger()))
    op.add_column("work_decisions", sa.Column("observed_scope_decision_id", sa.BigInteger()))
    op.add_column(
        "work_decisions", sa.Column("observed_milestone_impact_decision_id", sa.BigInteger())
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade work list: return observations preserve the factual "
        "state a Coordination Plan answered"
    )
