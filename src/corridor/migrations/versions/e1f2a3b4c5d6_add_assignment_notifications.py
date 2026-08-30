"""Add durable new-assignment notification occurrences and delivery records.

A committed roster-backed assignment must reliably reach the assigned person,
and an in-memory or feature-local delivery queue was rejected because it cannot
survive a crash, coordinate competing workers, or retain provider evidence.
This successor adds the immutable notification occurrence, its mutable delivery
dispatch, an append-only per-attempt receipt, and append-only wrong-assignment
feedback.  Delivery itself rides the shared supervised Due Work runtime (#351,
#332); these tables are the durable domain record it reconciles.

Revision ID: e1f2a3b4c5d6
Revises: e7a2f4c9d1b6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "e7a2f4c9d1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assignment_notifications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=False),
        sa.Column(
            "dependency_id", sa.BigInteger(), sa.ForeignKey("dependencies.id"), nullable=True
        ),
        sa.Column(
            "commitment_lineage_id",
            sa.BigInteger(),
            sa.ForeignKey("commitment_lineages.id"),
            nullable=True,
        ),
        sa.Column(
            "assignment_decision_id",
            sa.BigInteger(),
            sa.ForeignKey("work_decisions.id"),
            nullable=False,
        ),
        sa.Column(
            "recipient_roster_entry_id",
            sa.BigInteger(),
            sa.ForeignKey("project_roster_entries.id"),
            nullable=False,
        ),
        sa.Column("recipient_principal_subject", sa.String(128), nullable=False),
        sa.Column("occurrence_key", sa.String(64), nullable=False, unique=True),
        sa.Column("registered_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category = 'new_assignment'", name="ck_assignment_notification_category"
        ),
        sa.CheckConstraint(
            "subject_kind in ('constraint', 'statement')",
            name="ck_assignment_notification_subject_kind",
        ),
        sa.CheckConstraint(
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)",
            name="ck_assignment_notification_subject_shape",
        ),
        sa.CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_assignment_notification_key_hex",
        ),
        sa.CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_assignment_notification_actor",
        ),
        sa.CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_assignment_notification_recipient",
        ),
    )
    op.create_index(
        "ix_assignment_notifications_project",
        "assignment_notifications",
        ["project_id"],
    )
    op.create_index(
        "ix_assignment_notifications_decision",
        "assignment_notifications",
        ["assignment_decision_id"],
    )
    op.create_index(
        "ix_assignment_notifications_recipient",
        "assignment_notifications",
        ["project_id", "recipient_principal_subject"],
    )

    op.create_table(
        "assignment_notification_dispatches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "notification_id",
            sa.BigInteger(),
            sa.ForeignKey("assignment_notifications.id"),
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
        sa.CheckConstraint("channel = 'email'", name="ck_assignment_dispatch_channel"),
        sa.CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_assignment_dispatch_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_assignment_dispatch_attempt_count"
        ),
        sa.CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_assignment_dispatch_retry_shape",
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_assignment_dispatch_idempotency_hex",
        ),
    )
    op.create_index(
        "ix_assignment_dispatches_project",
        "assignment_notification_dispatches",
        ["project_id"],
    )

    op.create_table(
        "assignment_notification_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "dispatch_id",
            sa.BigInteger(),
            sa.ForeignKey("assignment_notification_dispatches.id"),
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
            name="ck_assignment_attempt_outcome",
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name="ck_assignment_attempt_positive"
        ),
        sa.CheckConstraint(
            "length(trim(runtime_owner)) > 0", name="ck_assignment_attempt_owner"
        ),
        sa.UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_assignment_attempt_number"
        ),
    )
    op.create_index(
        "ix_assignment_attempts_dispatch",
        "assignment_notification_attempts",
        ["dispatch_id"],
    )
    op.create_index(
        "ix_assignment_attempts_project",
        "assignment_notification_attempts",
        ["project_id"],
    )

    op.create_table(
        "assignment_notification_feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "notification_id",
            sa.BigInteger(),
            sa.ForeignKey("assignment_notifications.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("flagged_by", sa.String(128), nullable=False),
        sa.Column("feedback_kind", sa.String(24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "audit_log_id",
            sa.BigInteger(),
            sa.ForeignKey("audit_log.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "feedback_kind = 'incorrect_assignment'",
            name="ck_assignment_feedback_kind",
        ),
        sa.CheckConstraint(
            "length(trim(flagged_by)) > 0", name="ck_assignment_feedback_actor"
        ),
        sa.UniqueConstraint(
            "notification_id", "flagged_by", name="uq_assignment_feedback_person"
        ),
    )
    op.create_index(
        "ix_assignment_feedback_notification",
        "assignment_notification_feedback",
        ["notification_id"],
    )
    op.create_index(
        "ix_assignment_feedback_project",
        "assignment_notification_feedback",
        ["project_id"],
    )

    op.execute(
        """
        create function enforce_assignment_notification_immutable()
        returns trigger language plpgsql as $$
        begin
            raise exception 'assignment notification occurrences are immutable';
        end
        $$;
        create trigger assignment_notifications_are_immutable
        before update or delete on assignment_notifications
        for each row execute function enforce_assignment_notification_immutable();

        create function enforce_assignment_dispatch_identity()
        returns trigger language plpgsql as $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'assignment notification dispatches cannot be deleted';
            end if;
            if new.id <> old.id or new.public_id <> old.public_id
               or new.notification_id <> old.notification_id
               or new.channel <> old.channel
               or new.idempotency_key <> old.idempotency_key
               or new.created_at <> old.created_at then
                raise exception 'assignment notification dispatch identity is immutable';
            end if;
            new.updated_at = now();
            return new;
        end
        $$;
        create trigger assignment_dispatch_identity_is_immutable
        before update or delete on assignment_notification_dispatches
        for each row execute function enforce_assignment_dispatch_identity();

        create function refuse_assignment_attempt_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'assignment notification attempts are append-only';
        end
        $$;
        create trigger assignment_attempts_are_immutable
        before update or delete on assignment_notification_attempts
        for each row execute function refuse_assignment_attempt_mutation();

        create function refuse_assignment_feedback_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'assignment notification feedback is append-only';
        end
        $$;
        create trigger assignment_feedback_are_immutable
        before update or delete on assignment_notification_feedback
        for each row execute function refuse_assignment_feedback_mutation();

        create function reject_assignment_notification_truncate()
        returns trigger language plpgsql as $$
        begin
            raise exception 'assignment notification history cannot be truncated';
        end
        $$;
        create trigger assignment_notifications_reject_truncate
        before truncate on assignment_notifications
        for each statement execute function reject_assignment_notification_truncate();
        create trigger assignment_dispatches_reject_truncate
        before truncate on assignment_notification_dispatches
        for each statement execute function reject_assignment_notification_truncate();
        create trigger assignment_attempts_reject_truncate
        before truncate on assignment_notification_attempts
        for each statement execute function reject_assignment_notification_truncate();
        create trigger assignment_feedback_reject_truncate
        before truncate on assignment_notification_feedback
        for each statement execute function reject_assignment_notification_truncate();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from assignment_notifications)
               or exists (select 1 from assignment_notification_dispatches)
               or exists (select 1 from assignment_notification_attempts)
               or exists (select 1 from assignment_notification_feedback) then
                raise exception 'cannot erase retained assignment notification history';
            end if;
        end
        $$;
        """
    )
    for trigger, table in (
        ("assignment_feedback_reject_truncate", "assignment_notification_feedback"),
        ("assignment_attempts_reject_truncate", "assignment_notification_attempts"),
        ("assignment_dispatches_reject_truncate", "assignment_notification_dispatches"),
        ("assignment_notifications_reject_truncate", "assignment_notifications"),
        ("assignment_feedback_are_immutable", "assignment_notification_feedback"),
        ("assignment_attempts_are_immutable", "assignment_notification_attempts"),
        ("assignment_dispatch_identity_is_immutable", "assignment_notification_dispatches"),
        ("assignment_notifications_are_immutable", "assignment_notifications"),
    ):
        op.execute(f"drop trigger if exists {trigger} on {table}")
    op.execute("drop function if exists reject_assignment_notification_truncate()")
    op.execute("drop function if exists refuse_assignment_feedback_mutation()")
    op.execute("drop function if exists refuse_assignment_attempt_mutation()")
    op.execute("drop function if exists enforce_assignment_dispatch_identity()")
    op.execute("drop function if exists enforce_assignment_notification_immutable()")
    op.drop_table("assignment_notification_feedback")
    op.drop_table("assignment_notification_attempts")
    op.drop_table("assignment_notification_dispatches")
    op.drop_table("assignment_notifications")
