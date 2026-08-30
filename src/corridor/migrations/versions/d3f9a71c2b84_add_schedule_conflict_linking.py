"""Link conflicts to schedule dates by the schedule's own data.

ADR-0057, ticket #371. Three append-only tables carry the schedule matcher:
``schedule_governing_derivations`` records which activity codes and names flag
themselves as the governing set (or the one-time human pick when coding is too
poor to read); ``schedule_link_receipts`` retains the deciding values behind
each Constraint-to-key-date link, verbatim from both sources; and
``schedule_link_activations`` is the ADR-0050 replay gate for the automatic
location-link rule — a new automatic matching class that ships inactive.

Revision ID: d3f9a71c2b84
Revises: a364b7c9e2f1
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d3f9a71c2b84"
down_revision: Union[str, Sequence[str], None] = "86edb31fd81a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedule_governing_derivations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("method", sa.String(length=24), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column("matches_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "method in ('coded', 'awaiting_pick', 'human_pick')",
            name="ck_schedule_governing_method",
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_schedule_governing_recorded_by",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(matches_json) = 'array'",
            name="ck_schedule_governing_matches",
        ),
        sa.CheckConstraint(
            "source_sha256 is null or source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_schedule_governing_sha256",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_schedule_governing_derivations_project_id",
        "schedule_governing_derivations",
        ["project_id"],
    )

    op.create_table(
        "schedule_link_receipts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        sa.Column("milestone_id", sa.BigInteger(), nullable=False),
        sa.Column("milestone_registration_id", sa.BigInteger(), nullable=False),
        sa.Column("audit_log_id", sa.BigInteger(), nullable=False),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("policy_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "deciding_values_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "basis in ('exact_station_containment', 'human_choice', 'flow_through')",
            name="ck_schedule_link_receipts_basis",
        ),
        sa.CheckConstraint(
            "length(trim(decided_by)) > 0",
            name="ck_schedule_link_receipts_decided_by",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(deciding_values_json) = 'object'",
            name="ck_schedule_link_receipts_values",
        ),
        sa.CheckConstraint(
            "policy_sha256 is null or policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_schedule_link_receipts_sha256",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["dependency_id"], ["dependencies.id"]),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"]),
        sa.ForeignKeyConstraint(
            ["milestone_registration_id"], ["milestone_registrations.id"]
        ),
        sa.ForeignKeyConstraint(["audit_log_id"], ["audit_log.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("audit_log_id", name="uq_schedule_link_receipts_audit"),
    )
    op.create_index(
        "ix_schedule_link_receipts_project_id",
        "schedule_link_receipts",
        ["project_id"],
    )
    op.create_index(
        "ix_schedule_link_receipts_dependency_id",
        "schedule_link_receipts",
        ["dependency_id"],
    )

    op.create_table(
        "schedule_link_activations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("replay_case_count", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=160), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action in ('activate', 'suspend')",
            name="ck_schedule_link_activation_action",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_schedule_link_activation_reason",
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_schedule_link_activation_actor",
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_schedule_link_activation_sha256",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_schedule_link_activations_project_id",
        "schedule_link_activations",
        ["project_id"],
    )

    # Every schedule-linking record is append-only history (ADR-0057, ADR-0050):
    # nothing is deleted and no answer is rewritten in place.
    op.execute(
        """
        create function reject_schedule_linking_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'schedule linking records are append-only';
        end;
        $$;
        create trigger schedule_governing_derivations_are_immutable
        before update or delete on schedule_governing_derivations
        for each row execute function reject_schedule_linking_mutation();
        create trigger schedule_governing_derivations_reject_truncate
        before truncate on schedule_governing_derivations
        for each statement execute function reject_schedule_linking_mutation();
        create trigger schedule_link_receipts_are_immutable
        before update or delete on schedule_link_receipts
        for each row execute function reject_schedule_linking_mutation();
        create trigger schedule_link_receipts_reject_truncate
        before truncate on schedule_link_receipts
        for each statement execute function reject_schedule_linking_mutation();
        create trigger schedule_link_activations_are_immutable
        before update or delete on schedule_link_activations
        for each row execute function reject_schedule_linking_mutation();
        create trigger schedule_link_activations_reject_truncate
        before truncate on schedule_link_activations
        for each statement execute function reject_schedule_linking_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from schedule_link_receipts)
               or exists (select 1 from schedule_link_activations)
               or exists (select 1 from schedule_governing_derivations) then
                raise exception 'cannot erase retained schedule linking history';
            end if;
        end
        $$;
        drop trigger if exists schedule_governing_derivations_are_immutable
            on schedule_governing_derivations;
        drop trigger if exists schedule_governing_derivations_reject_truncate
            on schedule_governing_derivations;
        drop trigger if exists schedule_link_receipts_are_immutable
            on schedule_link_receipts;
        drop trigger if exists schedule_link_receipts_reject_truncate
            on schedule_link_receipts;
        drop trigger if exists schedule_link_activations_are_immutable
            on schedule_link_activations;
        drop trigger if exists schedule_link_activations_reject_truncate
            on schedule_link_activations;
        drop function if exists reject_schedule_linking_mutation();
        """
    )
    op.drop_index(
        "ix_schedule_link_activations_project_id",
        table_name="schedule_link_activations",
    )
    op.drop_table("schedule_link_activations")
    op.drop_index(
        "ix_schedule_link_receipts_dependency_id",
        table_name="schedule_link_receipts",
    )
    op.drop_index(
        "ix_schedule_link_receipts_project_id",
        table_name="schedule_link_receipts",
    )
    op.drop_table("schedule_link_receipts")
    op.drop_index(
        "ix_schedule_governing_derivations_project_id",
        table_name="schedule_governing_derivations",
    )
    op.drop_table("schedule_governing_derivations")
