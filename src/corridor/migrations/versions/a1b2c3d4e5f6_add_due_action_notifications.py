"""Add durable due-action notification occurrences and delivery records.

Reminders for a soon-due or past-due Next Action, urgent-overdue escalation to
the assigned person and one configured escalation contact, and a
non-interrupting per-recipient daily summary (#352, ADR-0034 decisions 39/48)
ride the same supervised Due Work runtime and the same delivery machinery as the
#351 new-assignment notification.  This successor adds the immutable derived
occurrence, its mutable delivery dispatch, and an append-only per-attempt
receipt; delivery itself reuses the runtime (#332) and the shared adapter seam.
These conditions are derived on each tick from the subject's current
authoritative plan, so the occurrence is keyed to converge an unchanged
condition rather than becoming a new event on every poll.

Revision ID: a1b2c3d4e5f6
Revises: f362a1b2c3d4
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f362a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "due_action_notifications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=True),
        sa.Column(
            "dependency_id",
            sa.BigInteger(),
            sa.ForeignKey("dependencies.id"),
            nullable=True,
        ),
        sa.Column(
            "commitment_lineage_id",
            sa.BigInteger(),
            sa.ForeignKey("commitment_lineages.id"),
            nullable=True,
        ),
        sa.Column(
            "plan_decision_id",
            sa.BigInteger(),
            sa.ForeignKey("work_decisions.id"),
            nullable=True,
        ),
        sa.Column("urgency", sa.String(16), nullable=True),
        sa.Column("action_due_date", sa.Date(), nullable=True),
        sa.Column("check_identity", sa.String(128), nullable=True),
        sa.Column("observation_start", sa.Date(), nullable=True),
        sa.Column("observation_end", sa.Date(), nullable=True),
        sa.Column("summary_json", postgresql.JSONB(), nullable=True),
        sa.Column("recipient_role", sa.String(16), nullable=False),
        sa.Column(
            "recipient_roster_entry_id",
            sa.BigInteger(),
            sa.ForeignKey("project_roster_entries.id"),
            nullable=False,
        ),
        sa.Column("recipient_principal_subject", sa.String(128), nullable=False),
        sa.Column("configuration_version", sa.String(64), nullable=False),
        sa.Column("occurrence_key", sa.String(64), nullable=False, unique=True),
        sa.Column("registered_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category in "
            "('next_action_due', 'next_action_escalation', 'daily_summary')",
            name="ck_due_action_notification_category",
        ),
        sa.CheckConstraint(
            "recipient_role in ('assignee', 'escalation', 'summary')",
            name="ck_due_action_notification_role",
        ),
        sa.CheckConstraint(
            "urgency is null or urgency in ('soon', 'overdue', 'urgent_overdue')",
            name="ck_due_action_notification_urgency",
        ),
        sa.CheckConstraint(
            "("
            "category = 'daily_summary' and subject_kind is null "
            "and dependency_id is null and commitment_lineage_id is null "
            "and plan_decision_id is null and urgency is null "
            "and action_due_date is null and recipient_role = 'summary' "
            "and observation_start is not null and observation_end is not null"
            ") or ("
            "category in ('next_action_due', 'next_action_escalation') "
            "and subject_kind in ('constraint', 'statement') "
            "and plan_decision_id is not null and urgency is not null "
            "and recipient_role in ('assignee', 'escalation') "
            "and ("
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)"
            ")"
            ")",
            name="ck_due_action_notification_shape",
        ),
        sa.CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_due_action_notification_key_hex",
        ),
        sa.CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_due_action_notification_actor",
        ),
        sa.CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_due_action_notification_recipient",
        ),
    )
    op.create_index(
        "ix_due_action_notifications_project",
        "due_action_notifications",
        ["project_id"],
    )
    op.create_index(
        "ix_due_action_notifications_recipient",
        "due_action_notifications",
        ["project_id", "recipient_principal_subject"],
    )

    op.create_table(
        "due_action_notification_dispatches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "notification_id",
            sa.BigInteger(),
            sa.ForeignKey("due_action_notifications.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("delivery_state", sa.String(16), nullable=False),
        sa.Column("recipient_contact", sa.Text(), nullable=True),
        sa.Column("delivery_limitation", sa.String(64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("provider_result_json", postgresql.JSONB(), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("channel = 'email'", name="ck_due_action_dispatch_channel"),
        sa.CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_due_action_dispatch_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_due_action_dispatch_attempt_count"
        ),
        sa.CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_due_action_dispatch_retry_shape",
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_due_action_dispatch_idempotency_hex",
        ),
    )
    op.create_index(
        "ix_due_action_dispatches_project",
        "due_action_notification_dispatches",
        ["project_id"],
    )

    op.create_table(
        "due_action_notification_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "dispatch_id",
            sa.BigInteger(),
            sa.ForeignKey("due_action_notification_dispatches.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("recipient_contact", sa.Text(), nullable=True),
        sa.Column("delivery_limitation", sa.String(64), nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("provider_result_json", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("runtime_owner", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome in ('completed', 'retry_due', 'failed', 'uncertain', 'skipped')",
            name="ck_due_action_attempt_outcome",
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name="ck_due_action_attempt_positive"
        ),
        sa.CheckConstraint(
            "length(trim(runtime_owner)) > 0", name="ck_due_action_attempt_owner"
        ),
        sa.UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_due_action_attempt_number"
        ),
    )
    op.create_index(
        "ix_due_action_attempts_dispatch",
        "due_action_notification_attempts",
        ["dispatch_id"],
    )
    op.create_index(
        "ix_due_action_attempts_project",
        "due_action_notification_attempts",
        ["project_id"],
    )

    op.execute(
        """
        create function enforce_due_action_notification_immutable()
        returns trigger language plpgsql as $$
        begin
            raise exception 'due action notification occurrences are immutable';
        end
        $$;
        create trigger due_action_notifications_are_immutable
        before update or delete on due_action_notifications
        for each row execute function enforce_due_action_notification_immutable();

        create function enforce_due_action_dispatch_identity()
        returns trigger language plpgsql as $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'due action notification dispatches cannot be deleted';
            end if;
            if new.id <> old.id or new.public_id <> old.public_id
               or new.notification_id <> old.notification_id
               or new.channel <> old.channel
               or new.idempotency_key <> old.idempotency_key
               or new.created_at <> old.created_at then
                raise exception 'due action notification dispatch identity is immutable';
            end if;
            new.updated_at = now();
            return new;
        end
        $$;
        create trigger due_action_dispatch_identity_is_immutable
        before update or delete on due_action_notification_dispatches
        for each row execute function enforce_due_action_dispatch_identity();

        create function refuse_due_action_attempt_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'due action notification attempts are append-only';
        end
        $$;
        create trigger due_action_attempts_are_immutable
        before update or delete on due_action_notification_attempts
        for each row execute function refuse_due_action_attempt_mutation();

        create function reject_due_action_notification_truncate()
        returns trigger language plpgsql as $$
        begin
            raise exception 'due action notification history cannot be truncated';
        end
        $$;
        create trigger due_action_notifications_reject_truncate
        before truncate on due_action_notifications
        for each statement execute function reject_due_action_notification_truncate();
        create trigger due_action_dispatches_reject_truncate
        before truncate on due_action_notification_dispatches
        for each statement execute function reject_due_action_notification_truncate();
        create trigger due_action_attempts_reject_truncate
        before truncate on due_action_notification_attempts
        for each statement execute function reject_due_action_notification_truncate();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from due_action_notifications)
               or exists (select 1 from due_action_notification_dispatches)
               or exists (select 1 from due_action_notification_attempts) then
                raise exception 'cannot erase retained due action notification history';
            end if;
        end
        $$;
        """
    )
    for trigger, table in (
        ("due_action_attempts_reject_truncate", "due_action_notification_attempts"),
        ("due_action_dispatches_reject_truncate", "due_action_notification_dispatches"),
        ("due_action_notifications_reject_truncate", "due_action_notifications"),
        ("due_action_attempts_are_immutable", "due_action_notification_attempts"),
        ("due_action_dispatch_identity_is_immutable", "due_action_notification_dispatches"),
        ("due_action_notifications_are_immutable", "due_action_notifications"),
    ):
        op.execute(f"drop trigger if exists {trigger} on {table}")
    op.execute("drop function if exists reject_due_action_notification_truncate()")
    op.execute("drop function if exists refuse_due_action_attempt_mutation()")
    op.execute("drop function if exists enforce_due_action_dispatch_identity()")
    op.execute("drop function if exists enforce_due_action_notification_immutable()")
    op.drop_table("due_action_notification_attempts")
    op.drop_table("due_action_notification_dispatches")
    op.drop_table("due_action_notifications")
