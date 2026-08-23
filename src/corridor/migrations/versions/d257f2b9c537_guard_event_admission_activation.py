"""Require every Event Admission activation to name a passing exact receipt.

Revision ID: d257f2b9c537
Revises: c257e1a8b426
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "d257f2b9c537"
down_revision: Union[str, Sequence[str], None] = "c257e1a8b426"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        create function require_passing_event_admission_activation()
        returns trigger language plpgsql as $$
        begin
            if not exists (
                select 1
                from event_admission_acceptance_receipts receipt
                where receipt.id = new.acceptance_receipt_id
                  and receipt.project_id = new.project_id
                  and receipt.policy_version = new.policy_version
                  and receipt.status = 'passed'
            ) then
                raise exception 'Event Admission activation requires its passing exact receipt';
            end if;
            return new;
        end
        $$;

        create trigger event_admission_activations_require_passing_receipt
        before insert on event_admission_activations
        for each row execute function require_passing_event_admission_activation();
        """
    )


def downgrade() -> None:
    raise RuntimeError("cannot weaken the Event Admission activation gate")
