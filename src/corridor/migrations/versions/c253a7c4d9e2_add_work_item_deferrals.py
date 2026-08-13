"""Make Coordinator Work Item deferrals explicit and return-bound.

Revision ID: c253a7c4d9e2
Revises: b252a7c4d9e2

An unknown Action Due Date cannot hide current work.  This additive revision
instead gives a Work Decision its own deferral chain and keeps the current
reason and exact return date on the Coordination Subject projection.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c253a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "b252a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("dependencies", "commitment_lineages"):
        op.add_column(table, sa.Column("deferral_reason", sa.String(length=64)))
        op.add_column(table, sa.Column("deferral_return_date", sa.Date()))
    op.add_column("work_decisions", sa.Column("deferral_reason", sa.String(length=64)))
    op.add_column("work_decisions", sa.Column("deferral_return_date", sa.Date()))
    op.drop_constraint("ck_work_decisions_field", "work_decisions", type_="check")
    op.create_check_constraint(
        "ck_work_decisions_field",
        "work_decisions",
        "field in ('internal_owner', 'next_action', 'milestone_impact', 'deferral')",
    )
    op.create_check_constraint(
        "ck_work_decisions_deferral_shape",
        "work_decisions",
        "(field <> 'deferral' and deferral_reason is null and deferral_return_date is null) "
        "or (field = 'deferral' and ((after_value is null and deferral_reason is null "
        "and deferral_return_date is null) or (after_value is not null "
        "and deferral_reason in ('waiting_for_information', 'waiting_for_external_party', "
        "'assigned_to_someone_else') and deferral_return_date is not null)))",
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade work list: an explicit deferral is an attributable "
        "Coordination Plan decision"
    )
