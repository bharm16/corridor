"""Documents declare their numbering scheme (ADR-0030).

How a matrix names its rows is a registry fact, declared at registration
like the document's date — never inferred from repeated numbers. Every
existing document defaults to `project-unique`, which is the behavior
every reader already assumed.

Revision ID: c4e8f2a91d37
Revises: a7b31e6c9f52
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4e8f2a91d37"
down_revision: Union[str, Sequence[str], None] = "a7b31e6c9f52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "numbering_scheme",
            sa.String(32),
            nullable=False,
            server_default="project-unique",
        ),
    )
    op.create_check_constraint(
        "ck_documents_numbering_scheme",
        "documents",
        "numbering_scheme in ('project-unique', 'per-party')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_documents_numbering_scheme", "documents", type_="check"
    )
    op.drop_column("documents", "numbering_scheme")
