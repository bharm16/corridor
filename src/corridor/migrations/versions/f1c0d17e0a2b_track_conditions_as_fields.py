"""Track approval conditions as fields in their own words (#373, ADR-0060).

Revision ID: f1c0d17e0a2b
Revises: d359a1b2c3e4

ADR-0060 makes a condition a field in its own words.  A conditional letter
already records as conditional and stays not ready with no click; #347 stored
that decision on ``documentation_field_confirmations``.  This revision adds the
two pieces #373 needs on top of that model:

- ``condition_resolutions``: the append-only record of the acts that move an
  open condition toward Ready — a person clearing it against a cited later
  passage or a recorded verbal, the exact-and-mechanical automatic clear, and a
  person dismissing a misdetection with a reason.  A condition entry itself is
  derived at read time from the conditional letter (never a stored checkmark,
  exactly like every other machine field), so this table holds only the
  attributable *clears* and *dismissals*, never the condition.  The composite
  foreign key to ``evidence_links(dependency_id, id)`` is the database-level
  guarantee that a resolution can only be about its own Constraint's letter.
- Two columns on ``documentation_field_confirmations`` so the optional
  full-approval override (ADR-0060) durably records the exact hedge a person
  chose to treat as immaterial, beside who did it and when.

Both are append-only: a clear or dismissal is history, and the override is the
deposition answer for why a hedged letter counted as approval.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f1c0d17e0a2b"
down_revision: Union[str, Sequence[str], None] = "d359a1b2c3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documentation_field_confirmations",
        sa.Column(
            "condition_immaterial",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "documentation_field_confirmations",
        sa.Column("overridden_condition_text", sa.Text()),
    )
    # The override is only meaningful on a conditional letter, and it must
    # carry the exact hedge it counted as immaterial (ADR-0060 / #373 AC).
    op.create_check_constraint(
        "ck_documentation_confirmation_override_records_hedge",
        "documentation_field_confirmations",
        "condition_immaterial = false or ("
        "classification = 'conditional' and overridden_condition_text is not null "
        "and length(trim(overridden_condition_text)) > 0)",
    )

    op.create_table(
        "condition_resolutions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        # The source conditional letter passage this resolution is about.  The
        # condition's stable identity is (dependency_id, evidence_link_id): one
        # cited conditional sentence is one condition.
        sa.Column("evidence_link_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        # A durable copy of the exact quoted words this act resolved, so the
        # history is legible even if the source is later superseded, and so a
        # hostile condition's directive-looking text is only ever stored data.
        sa.Column("condition_text", sa.Text(), nullable=False),
        # A clear cites its basis: a later verified passage, a recorded verbal
        # statement, or both.  A dismissal carries no basis, only a reason.
        sa.Column("basis_evidence_link_id", sa.BigInteger()),
        sa.Column("basis_event_id", sa.BigInteger()),
        sa.Column("reason", sa.Text()),
        # The exact-and-mechanical automatic clear (ADR-0050 replay-gated)
        # retains its reproducible receipt here; a human act leaves it null.
        sa.Column("receipt_json", postgresql.JSONB()),
        sa.Column("resolved_by", sa.String(128), nullable=False),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind in ('cleared', 'dismissed')",
            name="ck_condition_resolution_kind",
        ),
        sa.CheckConstraint(
            "length(trim(condition_text)) > 0",
            name="ck_condition_resolution_text",
        ),
        sa.CheckConstraint(
            "length(trim(resolved_by)) > 0",
            name="ck_condition_resolution_actor",
        ),
        # Every clear names what it stands on; a dismissal names a reason.
        sa.CheckConstraint(
            "kind <> 'cleared' or basis_evidence_link_id is not null "
            "or basis_event_id is not null",
            name="ck_condition_resolution_clear_has_basis",
        ),
        sa.CheckConstraint(
            "kind <> 'dismissed' or (reason is not null and length(trim(reason)) > 0)",
            name="ck_condition_resolution_dismissal_has_reason",
        ),
        sa.ForeignKeyConstraint(
            ["dependency_id", "evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_condition_resolution_owned_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["basis_evidence_link_id"], ["evidence_links.id"],
            name="fk_condition_resolution_basis_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["basis_event_id"], ["dependency_events.id"],
            name="fk_condition_resolution_basis_event",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_condition_resolutions_dependency",
        "condition_resolutions",
        ["dependency_id", "evidence_link_id"],
    )
    op.execute("""
        create function reject_condition_resolutions_mutation() returns trigger language plpgsql as $$
        begin raise exception 'condition_resolutions are append-only'; end; $$;
        create trigger condition_resolutions_are_immutable before update or delete on condition_resolutions
        for each row execute function reject_condition_resolutions_mutation();
        create trigger condition_resolutions_reject_truncate before truncate on condition_resolutions
        for each statement execute function reject_condition_resolutions_mutation();
    """)


def downgrade() -> None:
    op.execute("""
        do $$ begin
          if exists (select 1 from condition_resolutions) then
            raise exception 'cannot erase condition resolution history';
          end if;
        end $$;
        drop trigger if exists condition_resolutions_are_immutable on condition_resolutions;
        drop trigger if exists condition_resolutions_reject_truncate on condition_resolutions;
        drop function if exists reject_condition_resolutions_mutation();
    """)
    op.drop_index(
        "ix_condition_resolutions_dependency", table_name="condition_resolutions"
    )
    op.drop_table("condition_resolutions")
    op.drop_constraint(
        "ck_documentation_confirmation_override_records_hedge",
        "documentation_field_confirmations",
    )
    op.drop_column("documentation_field_confirmations", "overridden_condition_text")
    op.drop_column("documentation_field_confirmations", "condition_immaterial")
