"""reject a supersession attested by its own predecessor

Revision ID: a1c8f3e27b64
Revises: 9d4f2a7c1e83
Create Date: 2026-08-07 02:10:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a1c8f3e27b64"
down_revision: Union[str, Sequence[str], None] = "9d4f2a7c1e83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A document cannot be the authority for its own replacement.

    The replacement postdates the document, so the page a reader would
    check to confirm the edge predates the fact it confirms (ADR-0015).
    The registration boundary already refuses this; the constraint holds
    for a direct write, which is where an invariant of the record belongs.
    """
    op.create_check_constraint(
        "ck_documents_no_self_attested_supersession",
        "documents",
        "supersession_source_document_id is null "
        "or supersession_source_document_id <> id",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_documents_no_self_attested_supersession", "documents", type_="check"
    )
