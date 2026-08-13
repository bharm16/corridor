"""Seal immutable External Report PDF release receipts.

Revision ID: f255a7c4d9e2
Revises: f253a7c4d9e2

The working ``report_runs`` history is intentionally regenerable.  This
successor adds a separate byte-retaining receipt and a database trigger that
refuses later edits or deletion of a released artifact.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f255a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "f253a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_report_releases",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_name", sa.Text(), nullable=False),
        sa.Column("format", sa.String(length=16), server_default="pdf", nullable=False),
        sa.Column("pdf_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=False),
        sa.Column("evaluated_on", sa.Date(), nullable=False),
        sa.Column("ruleset_version", sa.String(length=64), nullable=False),
        sa.Column("provenance_mode", sa.String(length=32), nullable=False),
        sa.Column("record_context_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("released_by", sa.String(length=128), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("format = 'pdf'", name="ck_external_report_releases_pdf_only"),
        sa.CheckConstraint("pdf_sha256 ~ '^[0-9a-f]{64}$'", name="ck_external_report_releases_pdf_sha256"),
        sa.CheckConstraint("octet_length(pdf_bytes) > 5", name="ck_external_report_releases_nonempty_pdf"),
        sa.CheckConstraint("provenance_mode in ('all-supported-sources', 'document-only')", name="ck_external_report_releases_provenance_mode"),
        sa.CheckConstraint("jsonb_typeof(record_context_json) = 'object'", name="ck_external_report_releases_context_object"),
        sa.CheckConstraint("length(trim(artifact_name)) > 0", name="ck_external_report_releases_artifact_name"),
        sa.CheckConstraint("length(trim(released_by)) > 0", name="ck_external_report_releases_released_by"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_report_releases_project_id",
        "external_report_releases",
        ["project_id"],
    )
    op.execute(
        """
        create function prevent_external_report_release_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'released External Report receipts are immutable'
                using errcode = '55000';
        end;
        $$;
        """
    )
    op.execute(
        """
        create trigger prevent_external_report_release_mutation
        before update or delete on external_report_releases
        for each row execute function prevent_external_report_release_mutation();
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade External Report releases: sealed artifacts and their "
        "immutable receipts would be lost"
    )
