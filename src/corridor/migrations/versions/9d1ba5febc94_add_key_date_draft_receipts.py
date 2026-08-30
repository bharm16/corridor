"""add key date draft receipts

Revision ID: 9d1ba5febc94
Revises: b4d1e2f3a5c6
Create Date: 2026-08-29 21:53:47.087315

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "9d1ba5febc94"
down_revision: Union[str, Sequence[str], None] = "b4d1e2f3a5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Retain source-bound non-authoritative Key date draft receipts."""
    op.create_table(
        "key_date_draft_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source_document_id", sa.BigInteger(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("allowed_pages_json", postgresql.JSONB(), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(128)),
        sa.Column("detail", sa.Text()),
        sa.Column("configuration_json", postgresql.JSONB(), nullable=False),
        sa.Column("budget_json", postgresql.JSONB(), nullable=False),
        sa.Column("usage_json", postgresql.JSONB(), nullable=False),
        sa.Column("unresolved_json", postgresql.JSONB(), nullable=False),
        sa.Column("sequencing_json", postgresql.JSONB(), nullable=False),
        sa.Column("non_authoritative", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "source_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_key_date_draft_receipts_source_same_project",
        ),
        sa.CheckConstraint(
            "status in ('drafted', 'abstained', 'failed')",
            name="ck_key_date_draft_receipts_status",
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_key_date_draft_receipts_source_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_pages_json) = 'array' and "
            "jsonb_typeof(configuration_json) = 'object' and "
            "jsonb_typeof(budget_json) = 'object' and "
            "jsonb_typeof(usage_json) = 'object' and "
            "jsonb_typeof(unresolved_json) = 'array' and "
            "jsonb_typeof(sequencing_json) = 'array'",
            name="ck_key_date_draft_receipts_json",
        ),
    )
    op.create_index(
        "ix_key_date_draft_receipts_project_id",
        "key_date_draft_receipts",
        ["project_id"],
    )
    op.create_index(
        "ix_key_date_draft_receipts_source_document_id",
        "key_date_draft_receipts",
        ["source_document_id"],
    )
    op.create_table(
        "key_date_draft_row_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("need_date", sa.Date(), nullable=False),
        sa.Column("precision", sa.String(16), nullable=False),
        sa.Column("source_page", sa.Integer(), nullable=False),
        sa.Column("source_quote", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["receipt_id"], ["key_date_draft_receipts.id"]),
        sa.UniqueConstraint("receipt_id", "ordinal", name="uq_key_date_draft_row_ordinal"),
        sa.CheckConstraint("precision = 'day'", name="ck_key_date_draft_row_precision"),
        sa.CheckConstraint("source_page > 0", name="ck_key_date_draft_row_source_page"),
    )
    op.create_index(
        "ix_key_date_draft_row_receipts_receipt_id",
        "key_date_draft_row_receipts",
        ["receipt_id"],
    )
    op.execute(
        """
        create function refuse_key_date_draft_receipt_mutation()
        returns trigger language plpgsql as $$
        begin
          raise exception 'Key date draft receipts are append-only';
        end $$
        """
    )
    for table in ("key_date_draft_receipts", "key_date_draft_row_receipts"):
        op.execute(
            f"create trigger {table}_append_only before update or delete on {table} "
            "for each row execute function refuse_key_date_draft_receipt_mutation()"
        )


def downgrade() -> None:
    raise RuntimeError("cannot downgrade immutable Key date draft receipts")
