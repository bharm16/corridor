"""add immutable legacy Ledger archives

Revision ID: 6e1c4a9f2b70
Revises: b3c7e1a9f204
Create Date: 2026-08-06 01:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "6e1c4a9f2b70"
down_revision: Union[str, Sequence[str], None] = "b3c7e1a9f204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "legacy_ledger_archives",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("format_version", sa.String(length=64), nullable=False),
        sa.Column("content_json", postgresql.JSONB(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("dependency_count", sa.Integer(), nullable=False),
        sa.Column("assertion_count", sa.Integer(), nullable=False),
        sa.Column("evidence_link_count", sa.Integer(), nullable=False),
        sa.Column("audit_log_count", sa.Integer(), nullable=False),
        sa.Column("ref_code_high_watermark", sa.Integer(), nullable=False),
        sa.Column("retired_by", sa.Text(), nullable=False),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content_json) = 'object'",
            name="ck_legacy_ledger_archive_content_object",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_legacy_ledger_archive_sha256",
        ),
        sa.CheckConstraint(
            "dependency_count >= 0 and assertion_count >= 0 "
            "and evidence_link_count >= 0 and audit_log_count >= 0",
            name="ck_legacy_ledger_archive_counts",
        ),
        sa.CheckConstraint(
            "ref_code_high_watermark >= 0",
            name="ck_legacy_ledger_archive_ref_high_watermark",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id"),
    )
    op.execute(
        """
        create function reject_legacy_ledger_archive_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            raise exception 'Legacy Ledger archives are immutable'
                using errcode = '23514';
        end;
        $$;

        create trigger legacy_ledger_archives_are_immutable
        before insert or update or delete on legacy_ledger_archives
        for each row execute function reject_legacy_ledger_archive_mutation();

        create trigger legacy_ledger_archives_reject_truncate
        before truncate on legacy_ledger_archives
        for each statement execute function reject_legacy_ledger_archive_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "drop trigger if exists legacy_ledger_archives_reject_truncate "
        "on legacy_ledger_archives"
    )
    op.execute(
        "drop trigger if exists legacy_ledger_archives_are_immutable "
        "on legacy_ledger_archives"
    )
    op.execute("drop function if exists reject_legacy_ledger_archive_mutation()")
    op.drop_table("legacy_ledger_archives")
