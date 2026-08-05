"""add extraction runs

Revision ID: 4b94d3f5b8b1
Revises: 9a8c2e4c1b7f
Create Date: 2026-08-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4b94d3f5b8b1"
down_revision: Union[str, Sequence[str], None] = "9a8c2e4c1b7f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column(
            "page_errors",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_extraction_runs_completed_prompt_document",
        "extraction_runs",
        ["prompt_version", "document_id"],
        unique=False,
        postgresql_where=sa.text("page_errors = 0"),
    )

    op.execute(
        sa.text(
            """
            insert into extraction_runs (
                document_id,
                prompt_version,
                candidate_count,
                page_errors,
                completed_at
            )
            select
                c.source_document_id,
                c.prompt_version,
                count(*)::integer,
                0,
                coalesce(max(c.created_at), now())
            from candidates as c
            where c.prompt_version is not null
            group by c.source_document_id, c.prompt_version
            """
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        sa.text("drop index if exists ix_extraction_runs_completed_prompt_document")
    )
    op.drop_table("extraction_runs")
