"""add role-scoped operative publication support

Revision ID: c4e9a61d2b73
Revises: a8c3d72e1f59
Create Date: 2026-08-05 16:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4e9a61d2b73"
down_revision: Union[str, Sequence[str], None] = "a8c3d72e1f59"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Store publication designations without guessing from link order."""
    op.create_unique_constraint(
        "uq_evidence_links_dependency_id_id",
        "evidence_links",
        ["dependency_id", "id"],
    )
    op.create_table(
        "operative_support",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        sa.Column("evidence_link_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=11), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=True),
        sa.Column("designated_by", sa.Text(), nullable=False),
        sa.Column(
            "designated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role in ('publication')", name="operative_support_role"
        ),
        sa.ForeignKeyConstraint(["dependency_id"], ["dependencies.id"]),
        sa.ForeignKeyConstraint(
            ["dependency_id", "evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_operative_support_dependency_evidence",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_operative_support_record_role",
        "operative_support",
        ["dependency_id", "role"],
        unique=True,
        postgresql_where=sa.text("field_name is null"),
    )
    op.create_index(
        "uq_operative_support_field_role",
        "operative_support",
        ["dependency_id", "role", "field_name"],
        unique=True,
        postgresql_where=sa.text("field_name is not null"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_operative_support_field_role", table_name="operative_support"
    )
    op.drop_index(
        "uq_operative_support_record_role", table_name="operative_support"
    )
    op.drop_table("operative_support")
    op.drop_constraint(
        "uq_evidence_links_dependency_id_id", "evidence_links", type_="unique"
    )
