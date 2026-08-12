"""Restore the caller role for authorized statement retirement.

Revision ID: b230e4f5a6b7
Revises: a230c4d3e2f1

The purge function deliberately relies on the narrowly granted
``corridor_statement_retirement`` role so immutable-row triggers can distinguish
authorized retirement from ordinary writes.  A security-definer function changes
``current_user`` before those triggers run and therefore defeats that boundary.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b230e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "a230c4d3e2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        grant usage on schema public to corridor_statement_retirement;
        grant select, delete on table projects, legacy_ledger_archives,
            dependency_events, dependency_event_scopes,
            dependency_event_scope_decisions, dependency_event_timings,
            dependency_event_evidence, evidence_links
            to corridor_statement_retirement;
        alter function public.purge_external_party_statement_rows(bigint, text)
            security invoker;
        """
    )


def downgrade() -> None:
    # a230 now also uses SECURITY INVOKER, so the parent representation is
    # already the truthful one.  Privilege grants are intentionally retained.
    pass
