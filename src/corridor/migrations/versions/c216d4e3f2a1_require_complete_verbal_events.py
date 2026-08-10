"""Require every verbal event to carry the facts it claims to preserve.

Revision ID: c216d4e3f2a1
Revises: b216c4d3e2f1
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c216d4e3f2a1"
down_revision: Union[str, Sequence[str], None] = "b216c4d3e2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_verbal_events_require_call_facts",
        "dependency_events",
        """
        source_kind <> 'verbal' or (
            event_type in ('commitment', 'slip')
            and event_date is not null
            and committed_date is not null
            and length(trim(stated_party)) > 0
            and length(trim(description)) > 0
            and length(trim(created_by)) > 0
        )
        """,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_verbal_events_require_call_facts",
        "dependency_events",
        type_="check",
    )
