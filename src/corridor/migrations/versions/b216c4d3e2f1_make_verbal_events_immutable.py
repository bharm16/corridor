"""Make verbally sourced DependencyEvents append-only.

Revision ID: b216c4d3e2f1
Revises: a216b4c3d2e1
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b216c4d3e2f1"
down_revision: Union[str, Sequence[str], None] = "a216b4c3d2e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        create function reject_verbal_dependency_event_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            if tg_op = 'TRUNCATE' then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            if tg_op = 'DELETE' and old.source_kind = 'verbal' then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            if tg_op = 'UPDATE' and (
                old.source_kind = 'verbal' or new.source_kind = 'verbal'
            ) then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

        create trigger verbal_dependency_events_are_immutable
        before update or delete on dependency_events
        for each row execute function reject_verbal_dependency_event_mutation();

        create trigger verbal_dependency_events_reject_truncate
        before truncate on dependency_events
        for each statement execute function reject_verbal_dependency_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        drop trigger if exists verbal_dependency_events_are_immutable
            on dependency_events;
        drop trigger if exists verbal_dependency_events_reject_truncate
            on dependency_events;
        drop function if exists reject_verbal_dependency_event_mutation();
        """
    )
