"""Make sealed External Report release history truncate-proof.

Revision ID: b255a7c4d9e2
Revises: a255a7c4d9e2

Row-level immutability guards updates and deletes, but PostgreSQL TRUNCATE
does not fire those triggers.  This successor closes that separate mutation
path for the sealed artifact history.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b255a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "a255a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        create function reject_external_report_release_truncate()
        returns trigger language plpgsql as $$
        begin
            raise exception 'released External Report receipts are immutable'
                using errcode = '55000';
        end;
        $$;
        """
    )
    op.execute(
        """
        create trigger external_report_releases_reject_truncate
        before truncate on external_report_releases
        for each statement execute function reject_external_report_release_truncate();
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade External Report release immutability: sealed history "
        "would regain a destructive mutation path"
    )
