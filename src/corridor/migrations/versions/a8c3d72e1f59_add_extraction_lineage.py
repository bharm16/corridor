"""add immutable extraction lineage and explicit Active Run

Revision ID: a8c3d72e1f59
Revises: f2b7c91a6d40
Create Date: 2026-08-05 15:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8c3d72e1f59"
down_revision: Union[str, Sequence[str], None] = "f2b7c91a6d40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Represent every attempt and link new Candidates to exactly one run."""
    op.add_column(
        "extraction_runs",
        sa.Column(
            "outcome",
            sa.String(length=10),
            nullable=False,
            server_default="completed",
        ),
    )
    op.create_check_constraint(
        "extraction_outcome",
        "extraction_runs",
        "outcome in ('completed', 'failed', 'unreadable', 'no_matrix')",
    )
    op.add_column(
        "extraction_runs", sa.Column("model", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "extraction_runs",
        sa.Column("schema_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "extraction_runs", sa.Column("error_detail", sa.Text(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_extraction_runs_document_id_id",
        "extraction_runs",
        ["document_id", "id"],
    )

    op.add_column(
        "candidates", sa.Column("extraction_run_id", sa.BigInteger(), nullable=True)
    )

    # Historical rows are linked only when both sides describe exactly one
    # cohort. Multiple receipts, or a count disagreement, remain null rather
    # than manufacturing provenance from ordering or timestamps.
    op.execute(
        sa.text(
            """
            with candidate_cohorts as (
                select
                    source_document_id as document_id,
                    prompt_version,
                    count(*)::integer as candidate_count
                from candidates
                where prompt_version is not null
                group by source_document_id, prompt_version
            ),
            run_cohorts as (
                select
                    document_id,
                    prompt_version,
                    count(*)::integer as run_count,
                    min(id) as run_id,
                    min(candidate_count)::integer as recorded_count
                from extraction_runs
                where outcome = 'completed'
                  and page_errors = 0
                group by document_id, prompt_version
            ),
            unambiguous as (
                select c.document_id, c.prompt_version, r.run_id
                from candidate_cohorts c
                join run_cohorts r
                  on r.document_id = c.document_id
                 and r.prompt_version = c.prompt_version
                where r.run_count = 1
                  and r.recorded_count = c.candidate_count
            )
            update candidates c
               set extraction_run_id = u.run_id
              from unambiguous u
             where c.source_document_id = u.document_id
               and c.prompt_version = u.prompt_version
            """
        )
    )
    op.create_foreign_key(
        "fk_candidates_document_extraction_run",
        "candidates",
        "extraction_runs",
        ["source_document_id", "extraction_run_id"],
        ["document_id", "id"],
    )

    op.create_table(
        "active_extraction_runs",
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("extraction_run_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "declared_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(
            ["document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
            name="fk_active_run_document_extraction_run",
        ),
        sa.PrimaryKeyConstraint("document_id"),
        sa.UniqueConstraint("extraction_run_id"),
    )

    op.drop_index(
        "ix_extraction_runs_completed_prompt_document",
        table_name="extraction_runs",
    )
    op.create_index(
        "ix_extraction_runs_completed_prompt_document",
        "extraction_runs",
        ["prompt_version", "document_id"],
        unique=False,
        postgresql_where=sa.text("outcome = 'completed' and page_errors = 0"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_extraction_runs_completed_prompt_document",
        table_name="extraction_runs",
    )
    op.create_index(
        "ix_extraction_runs_completed_prompt_document",
        "extraction_runs",
        ["prompt_version", "document_id"],
        unique=False,
        postgresql_where=sa.text("page_errors = 0"),
    )
    op.drop_table("active_extraction_runs")
    op.drop_constraint(
        "fk_candidates_document_extraction_run", "candidates", type_="foreignkey"
    )
    op.drop_column("candidates", "extraction_run_id")
    op.drop_constraint(
        "uq_extraction_runs_document_id_id", "extraction_runs", type_="unique"
    )
    op.drop_column("extraction_runs", "error_detail")
    op.drop_column("extraction_runs", "schema_version")
    op.drop_column("extraction_runs", "model")
    op.drop_constraint("extraction_outcome", "extraction_runs", type_="check")
    op.drop_column("extraction_runs", "outcome")
