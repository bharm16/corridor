"""Add durable document-change and lost-support notification occurrences.

The two remaining #196 immediate-notification categories (ADR-0034 decision 39,
ADR-0037) — a previously affirmative Documentation Review that lost applicable
current support, and an authentic registered source transition affecting a
current Commitment or a relocation/removal/abandonment Constraint — need the same
crash-durable, competing-worker-safe, provider-evidence-retaining delivery record
the new-assignment occurrence uses (#351), but never the assignment-shaped row: a
loss preserves the earlier review *and its author*, whose recipient may hold no
current roster entry.  This adds the immutable occurrence, its mutable delivery
dispatch, and an append-only per-attempt receipt.  Delivery rides the shared
supervised Due Work runtime (#332); these tables are the durable record it
reconciles.

Revision ID: b7d3f9a1c2e5
Revises: a1b2c3d4e5f6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7d3f9a1c2e5"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_notifications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=False),
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
        sa.Column("recipient_principal_subject", sa.String(128), nullable=False),
        sa.Column("recipient_role", sa.String(48), nullable=False),
        sa.Column(
            "review_confirmation_id",
            sa.BigInteger(),
            sa.ForeignKey("documentation_field_confirmations.id"),
            nullable=True,
        ),
        sa.Column("requirement_field", sa.String(64), nullable=True),
        sa.Column("reviewed_evidence_link_id", sa.BigInteger(), nullable=True),
        sa.Column("original_reviewer_subject", sa.String(128), nullable=True),
        sa.Column(
            "predecessor_document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id"),
            nullable=True,
        ),
        sa.Column(
            "successor_document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id"),
            nullable=True,
        ),
        sa.Column("comparison_id", sa.BigInteger(), nullable=True),
        sa.Column("finding_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "statement_event_id",
            sa.BigInteger(),
            sa.ForeignKey("dependency_events.id"),
            nullable=True,
        ),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column(
            "change_uncertain",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("source_context_json", postgresql.JSONB(), nullable=True),
        sa.Column("occurrence_key", sa.String(64), nullable=False, unique=True),
        sa.Column("registered_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category in ('documentation_loss', 'document_change')",
            name="ck_document_notification_category",
        ),
        sa.CheckConstraint(
            "subject_kind in ('constraint', 'statement')",
            name="ck_document_notification_subject_kind",
        ),
        sa.CheckConstraint(
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)",
            name="ck_document_notification_subject_shape",
        ),
        sa.CheckConstraint(
            "recipient_role in "
            "('current_assignee', 'original_reviewer', "
            "'current_assignee_and_original_reviewer')",
            name="ck_document_notification_recipient_role",
        ),
        sa.CheckConstraint(
            "(category = 'documentation_loss' and review_confirmation_id is not null) "
            "or (category = 'document_change' and "
            "(comparison_id is not null or statement_event_id is not null))",
            name="ck_document_notification_authentic_source",
        ),
        sa.CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_document_notification_key_hex",
        ),
        sa.CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_document_notification_actor",
        ),
        sa.CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_document_notification_recipient",
        ),
    )
    op.create_index(
        "ix_document_notifications_project",
        "document_notifications",
        ["project_id"],
    )
    op.create_index(
        "ix_document_notifications_recipient",
        "document_notifications",
        ["project_id", "recipient_principal_subject"],
    )

    op.create_table(
        "document_notification_dispatches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "notification_id",
            sa.BigInteger(),
            sa.ForeignKey("document_notifications.id"),
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
        sa.CheckConstraint("channel = 'email'", name="ck_document_dispatch_channel"),
        sa.CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_document_dispatch_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_document_dispatch_attempt_count"
        ),
        sa.CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_document_dispatch_retry_shape",
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_document_dispatch_idempotency_hex",
        ),
    )
    op.create_index(
        "ix_document_dispatches_project",
        "document_notification_dispatches",
        ["project_id"],
    )

    op.create_table(
        "document_notification_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "dispatch_id",
            sa.BigInteger(),
            sa.ForeignKey("document_notification_dispatches.id"),
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
            name="ck_document_attempt_outcome",
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name="ck_document_attempt_positive"
        ),
        sa.CheckConstraint(
            "length(trim(runtime_owner)) > 0", name="ck_document_attempt_owner"
        ),
        sa.UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_document_attempt_number"
        ),
    )
    op.create_index(
        "ix_document_attempts_dispatch",
        "document_notification_attempts",
        ["dispatch_id"],
    )
    op.create_index(
        "ix_document_attempts_project",
        "document_notification_attempts",
        ["project_id"],
    )

    op.execute(
        """
        create function enforce_document_notification_immutable()
        returns trigger language plpgsql as $$
        begin
            raise exception 'document notification occurrences are immutable';
        end
        $$;
        create trigger document_notifications_are_immutable
        before update or delete on document_notifications
        for each row execute function enforce_document_notification_immutable();

        create function enforce_document_dispatch_identity()
        returns trigger language plpgsql as $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'document notification dispatches cannot be deleted';
            end if;
            if new.id <> old.id or new.public_id <> old.public_id
               or new.notification_id <> old.notification_id
               or new.channel <> old.channel
               or new.idempotency_key <> old.idempotency_key
               or new.created_at <> old.created_at then
                raise exception 'document notification dispatch identity is immutable';
            end if;
            new.updated_at = now();
            return new;
        end
        $$;
        create trigger document_dispatch_identity_is_immutable
        before update or delete on document_notification_dispatches
        for each row execute function enforce_document_dispatch_identity();

        create function refuse_document_attempt_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'document notification attempts are append-only';
        end
        $$;
        create trigger document_attempts_are_immutable
        before update or delete on document_notification_attempts
        for each row execute function refuse_document_attempt_mutation();

        create function reject_document_notification_truncate()
        returns trigger language plpgsql as $$
        begin
            raise exception 'document notification history cannot be truncated';
        end
        $$;
        create trigger document_notifications_reject_truncate
        before truncate on document_notifications
        for each statement execute function reject_document_notification_truncate();
        create trigger document_dispatches_reject_truncate
        before truncate on document_notification_dispatches
        for each statement execute function reject_document_notification_truncate();
        create trigger document_attempts_reject_truncate
        before truncate on document_notification_attempts
        for each statement execute function reject_document_notification_truncate();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from document_notifications)
               or exists (select 1 from document_notification_dispatches)
               or exists (select 1 from document_notification_attempts) then
                raise exception 'cannot erase retained document notification history';
            end if;
        end
        $$;
        """
    )
    for trigger, table in (
        ("document_attempts_reject_truncate", "document_notification_attempts"),
        ("document_dispatches_reject_truncate", "document_notification_dispatches"),
        ("document_notifications_reject_truncate", "document_notifications"),
        ("document_attempts_are_immutable", "document_notification_attempts"),
        ("document_dispatch_identity_is_immutable", "document_notification_dispatches"),
        ("document_notifications_are_immutable", "document_notifications"),
    ):
        op.execute(f"drop trigger if exists {trigger} on {table}")
    op.execute("drop function if exists reject_document_notification_truncate()")
    op.execute("drop function if exists refuse_document_attempt_mutation()")
    op.execute("drop function if exists enforce_document_dispatch_identity()")
    op.execute("drop function if exists enforce_document_notification_immutable()")
    op.drop_table("document_notification_attempts")
    op.drop_table("document_notification_dispatches")
    op.drop_table("document_notifications")
