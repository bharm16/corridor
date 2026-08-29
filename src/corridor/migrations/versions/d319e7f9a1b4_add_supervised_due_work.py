"""Add the shared supervised due-work runtime and operational receipts.

Feature-local in-memory schedulers were rejected because they cannot coordinate
claims or retain crash evidence across processes. This successor adds one
durable schedule, occurrence, and append-only attempt-receipt contract.

Revision ID: d319e7f9a1b4
Revises: c318d6e8f0a3
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d319e7f9a1b4"
down_revision: Union[str, Sequence[str], None] = "c318d6e8f0a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "due_work_schedules",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("handler_key", sa.String(64), nullable=False),
        sa.Column("configuration_version", sa.String(64), nullable=False),
        sa.Column("scope_json", postgresql.JSONB(), nullable=False),
        sa.Column("configuration_json", postgresql.JSONB(), nullable=False),
        sa.Column("configuration_sha256", sa.String(64), nullable=False),
        sa.Column("input_identity_sha256", sa.String(64), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cadence", sa.String(32), nullable=False),
        sa.Column("timezone_name", sa.String(64), nullable=False),
        sa.Column("missed_run_policy", sa.String(32), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("backoff_seconds", sa.Integer(), nullable=False),
        sa.Column("claim_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("deadline_seconds", sa.Integer(), nullable=False),
        sa.Column("concurrency_limit", sa.Integer(), nullable=False),
        sa.Column("model_token_budget", sa.Integer(), nullable=False),
        sa.Column("notification_budget", sa.Integer(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "configuration_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_schedule_configuration_sha256",
        ),
        sa.CheckConstraint(
            "input_identity_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_schedule_input_identity_sha256",
        ),
        sa.CheckConstraint(
            "retention_days > 0 and max_attempts > 0 and backoff_seconds >= 0 "
            "and claim_ttl_seconds > 0 and deadline_seconds > 0 "
            "and concurrency_limit > 0 and model_token_budget >= 0 "
            "and notification_budget >= 0",
            name="ck_due_work_schedule_budgets",
        ),
        sa.CheckConstraint(
            "disabled_at is null or disabled_at >= enabled_at",
            name="ck_due_work_schedule_disable_order",
        ),
        sa.UniqueConstraint(
            "project_id",
            "handler_key",
            "configuration_version",
            "input_identity_sha256",
            name="uq_due_work_schedule_identity",
        ),
    )
    op.create_index("ix_due_work_schedules_project_id", "due_work_schedules", ["project_id"])
    op.create_table(
        "due_work_occurrences",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "scheduled_job_id",
            sa.BigInteger(),
            sa.ForeignKey("due_work_schedules.id"),
            nullable=False,
        ),
        sa.Column("occurrence_key", sa.String(64), nullable=False, unique=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner", sa.String(128), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state in ('pending', 'claimed', 'retry_due', 'completed', 'failed')",
            name="ck_due_work_occurrence_state",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_due_work_occurrence_attempt_count"),
        sa.CheckConstraint(
            "(state = 'claimed' and owner is not null and claim_token is not null "
            "and lease_expires_at is not null and claimed_at is not null "
            "and deadline_at is not null) or "
            "(state <> 'claimed' and owner is null and claim_token is null "
            "and lease_expires_at is null and claimed_at is null and deadline_at is null)",
            name="ck_due_work_occurrence_claim_shape",
        ),
        sa.CheckConstraint(
            "(state = 'retry_due' and next_attempt_at is not null) or "
            "(state <> 'retry_due' and next_attempt_at is null)",
            name="ck_due_work_occurrence_retry_shape",
        ),
    )
    op.create_index("ix_due_work_occurrences_job", "due_work_occurrences", ["scheduled_job_id"])
    op.create_index("ix_due_work_occurrences_due", "due_work_occurrences", ["due_at"])
    op.create_table(
        "due_work_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "occurrence_id",
            sa.BigInteger(),
            sa.ForeignKey("due_work_occurrences.id"),
            nullable=False,
        ),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("handler_key", sa.String(64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False, unique=True),
        sa.Column("runtime_owner", sa.String(128), nullable=False),
        sa.Column("execution_outcome", sa.String(16), nullable=False),
        sa.Column("handler_result_json", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("safe_next_step", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "execution_outcome in ('completed', 'retry_due', 'failed')",
            name="ck_due_work_receipt_outcome",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_receipt_content_sha256",
        ),
        sa.CheckConstraint(
            "attempt_number > 0 and finished_at >= started_at",
            name="ck_due_work_receipt_attempt",
        ),
        sa.CheckConstraint(
            "(execution_outcome = 'completed' and handler_result_json is not null "
            "and error_code is null) or "
            "(execution_outcome in ('retry_due', 'failed') "
            "and handler_result_json is null and error_code is not null)",
            name="ck_due_work_receipt_result_shape",
        ),
        sa.UniqueConstraint(
            "occurrence_id", "attempt_number", name="uq_due_work_receipt_attempt"
        ),
    )
    op.create_index("ix_due_work_receipts_occurrence", "due_work_receipts", ["occurrence_id"])
    op.create_index("ix_due_work_receipts_project", "due_work_receipts", ["project_id"])
    op.execute(
        """
        create function enforce_due_work_schedule_mutation()
        returns trigger language plpgsql as $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'Due Work schedule declarations are append-preserving';
            end if;
            if old.disabled_at is null and new.disabled_at is not null
               and (to_jsonb(new) - 'disabled_at') = (to_jsonb(old) - 'disabled_at') then
                return new;
            end if;
            raise exception 'Due Work schedule declarations are immutable';
        end
        $$;
        create trigger due_work_schedules_are_immutable
        before update or delete on due_work_schedules
        for each row execute function enforce_due_work_schedule_mutation();

        create function enforce_due_work_occurrence_identity()
        returns trigger language plpgsql as $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'Due Work occurrences cannot be deleted';
            end if;
            if new.id <> old.id or new.public_id <> old.public_id
               or new.scheduled_job_id <> old.scheduled_job_id
               or new.occurrence_key <> old.occurrence_key
               or new.due_at <> old.due_at or new.created_at <> old.created_at then
                raise exception 'Due Work occurrence identity is immutable';
            end if;
            new.updated_at = now();
            return new;
        end
        $$;
        create trigger due_work_occurrence_identity_is_immutable
        before update or delete on due_work_occurrences
        for each row execute function enforce_due_work_occurrence_identity();

        create function refuse_due_work_receipt_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'Due Work receipts are append-only';
        end
        $$;
        create trigger due_work_receipts_are_immutable
        before update or delete on due_work_receipts
        for each row execute function refuse_due_work_receipt_mutation();

        create function reject_due_work_truncate()
        returns trigger language plpgsql as $$
        begin
            raise exception 'Due Work operational history cannot be truncated';
        end
        $$;
        create trigger due_work_schedules_reject_truncate
        before truncate on due_work_schedules
        for each statement execute function reject_due_work_truncate();
        create trigger due_work_occurrences_reject_truncate
        before truncate on due_work_occurrences
        for each statement execute function reject_due_work_truncate();
        create trigger due_work_receipts_reject_truncate
        before truncate on due_work_receipts
        for each statement execute function reject_due_work_truncate();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from due_work_schedules)
               or exists (select 1 from due_work_occurrences)
               or exists (select 1 from due_work_receipts) then
                raise exception 'cannot erase retained Due Work operational history';
            end if;
        end
        $$;
        """
    )
    for trigger, table in (
        ("due_work_receipts_reject_truncate", "due_work_receipts"),
        ("due_work_occurrences_reject_truncate", "due_work_occurrences"),
        ("due_work_schedules_reject_truncate", "due_work_schedules"),
        ("due_work_receipts_are_immutable", "due_work_receipts"),
        ("due_work_occurrence_identity_is_immutable", "due_work_occurrences"),
        ("due_work_schedules_are_immutable", "due_work_schedules"),
    ):
        op.execute(f"drop trigger if exists {trigger} on {table}")
    op.execute("drop function if exists reject_due_work_truncate()")
    op.execute("drop function if exists refuse_due_work_receipt_mutation()")
    op.execute("drop function if exists enforce_due_work_occurrence_identity()")
    op.execute("drop function if exists enforce_due_work_schedule_mutation()")
    op.drop_table("due_work_receipts")
    op.drop_table("due_work_occurrences")
    op.drop_table("due_work_schedules")
