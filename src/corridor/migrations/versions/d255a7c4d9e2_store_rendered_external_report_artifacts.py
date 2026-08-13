"""Store a rendered PDF before a person releases it.

Revision ID: d255a7c4d9e2
Revises: b255a7c4d9e2

Rendering and release are separate acts.  A rendered artifact captures the
exact bytes and frozen Report context before the designated project person
later chooses that immutable artifact for external release.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d255a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "b255a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_report_artifacts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_name", sa.Text(), nullable=False),
        sa.Column("format", sa.String(length=16), server_default="pdf", nullable=False),
        sa.Column("pdf_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=False),
        sa.Column("evaluated_on", sa.Date(), nullable=False),
        sa.Column("ruleset_version", sa.String(length=64), nullable=False),
        sa.Column("evaluation_context_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provenance_mode", sa.String(length=32), nullable=False),
        sa.Column("record_context_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rendered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("format = 'pdf'", name="ck_external_report_artifacts_pdf_only"),
        sa.CheckConstraint("pdf_sha256 ~ '^[0-9a-f]{64}$'", name="ck_external_report_artifacts_pdf_sha256"),
        sa.CheckConstraint("octet_length(pdf_bytes) > 5", name="ck_external_report_artifacts_nonempty_pdf"),
        sa.CheckConstraint("provenance_mode in ('all-supported-sources', 'document-only')", name="ck_external_report_artifacts_provenance_mode"),
        sa.CheckConstraint("jsonb_typeof(evaluation_context_json) = 'object'", name="ck_external_report_artifacts_evaluation_object"),
        sa.CheckConstraint("jsonb_typeof(record_context_json) = 'object'", name="ck_external_report_artifacts_context_object"),
        sa.CheckConstraint("length(trim(artifact_name)) > 0", name="ck_external_report_artifacts_artifact_name"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_report_artifacts_project_id",
        "external_report_artifacts",
        ["project_id"],
    )
    op.add_column(
        "external_report_releases",
        sa.Column("artifact_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_external_report_releases_artifact",
        "external_report_releases",
        "external_report_artifacts",
        ["artifact_id"],
        ["id"],
    )
    op.execute(
        """
        create function prevent_external_report_artifact_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'rendered External Report artifacts are immutable'
                using errcode = '55000';
        end;
        $$;
        create function reject_external_report_artifact_truncate()
        returns trigger language plpgsql as $$
        begin
            raise exception 'rendered External Report artifacts are immutable'
                using errcode = '55000';
        end;
        $$;
        create trigger external_report_artifacts_are_immutable
        before update or delete on external_report_artifacts
        for each row execute function prevent_external_report_artifact_mutation();
        create trigger external_report_artifacts_reject_truncate
        before truncate on external_report_artifacts
        for each statement execute function reject_external_report_artifact_truncate();
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade rendered External Report artifacts: immutable PDF "
        "identity and release links would be lost"
    )
