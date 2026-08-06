"""add immutable Reconfirmation receipts

Revision ID: b3c7e1a9f204
Revises: e6f2a9c7d481
Create Date: 2026-08-05 22:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b3c7e1a9f204"
down_revision: Union[str, Sequence[str], None] = "e6f2a9c7d481"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reconfirmation_receipts",
        sa.Column("audit_log_id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        sa.Column("successor_candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("before_json", postgresql.JSONB(), nullable=False),
        sa.Column("after_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(before_json) = 'object'",
            name="ck_reconfirmation_receipt_before_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(after_json) = 'object'",
            name="ck_reconfirmation_receipt_after_object",
        ),
        sa.ForeignKeyConstraint(
            ["audit_log_id"], ["audit_log.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["dependency_id"], ["dependencies.id"]),
        sa.ForeignKeyConstraint(
            ["successor_candidate_id"], ["candidates.id"]
        ),
        sa.PrimaryKeyConstraint("audit_log_id"),
    )
    op.create_index(
        "ix_reconfirmation_receipts_dependency_id",
        "reconfirmation_receipts",
        ["dependency_id"],
    )
    op.execute(
        """
        create function enforce_reconfirmation_receipt_mutation()
        returns trigger
        language plpgsql
        as $$
        declare
            valid_binding boolean;
            demo_project boolean;
        begin
            if tg_op = 'INSERT' then
                begin
                    select exists (
                        select 1
                          from audit_log
                          join dependencies
                            on dependencies.id = new.dependency_id
                          join candidates
                            on candidates.id = new.successor_candidate_id
                         where audit_log.id = new.audit_log_id
                           and audit_log.action =
                               'reconfirm_operative_support'
                           and audit_log.entity_type = 'dependency'
                           and audit_log.entity_id = new.dependency_id
                           and audit_log.before_json = new.before_json
                           and audit_log.after_json = new.after_json
                           and candidates.project_id = dependencies.project_id
                           and jsonb_typeof(
                               audit_log.after_json ->
                               'successor_candidate_id'
                           ) = 'number'
                           and (audit_log.after_json ->>
                                'successor_candidate_id')::bigint =
                               new.successor_candidate_id
                    ) into valid_binding;
                exception
                    when invalid_text_representation
                        or numeric_value_out_of_range then
                        valid_binding := false;
                end;
                if not valid_binding then
                    raise exception 'Reconfirmation receipt binding is invalid'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from dependencies
                      join projects
                        on projects.id = dependencies.project_id
                     where dependencies.id = old.dependency_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_project;
                if demo_project then
                    return old;
                end if;
            end if;

            raise exception 'Reconfirmation receipts are append-only'
                using errcode = '23514';
        end;
        $$;

        create trigger reconfirmation_receipts_are_immutable
        before insert or update or delete on reconfirmation_receipts
        for each row execute function enforce_reconfirmation_receipt_mutation();

        create trigger reconfirmation_receipts_reject_truncate
        before truncate on reconfirmation_receipts
        for each statement execute function enforce_reconfirmation_receipt_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "drop trigger if exists reconfirmation_receipts_reject_truncate "
        "on reconfirmation_receipts"
    )
    op.execute(
        "drop trigger if exists reconfirmation_receipts_are_immutable "
        "on reconfirmation_receipts"
    )
    op.execute("drop function if exists enforce_reconfirmation_receipt_mutation()")
    op.drop_table("reconfirmation_receipts")
