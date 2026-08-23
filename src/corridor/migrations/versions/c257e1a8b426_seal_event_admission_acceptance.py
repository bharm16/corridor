"""Seal Event Admission acceptance and activation history.

Revision ID: c257e1a8b426
Revises: b257d0f7a315
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "c257e1a8b426"
down_revision: Union[str, Sequence[str], None] = "b257d0f7a315"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        create function refuse_event_admission_acceptance_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'Event Admission acceptance history is immutable';
        end
        $$;

        create trigger event_admission_acceptance_receipts_are_immutable
        before update or delete on event_admission_acceptance_receipts
        for each row execute function refuse_event_admission_acceptance_mutation();

        create trigger event_admission_activations_are_immutable
        before update or delete on event_admission_activations
        for each row execute function refuse_event_admission_acceptance_mutation();
        """
    )


def downgrade() -> None:
    raise RuntimeError("cannot unseal immutable Event Admission acceptance history")
