"""criticality is nullable: silence is not normal

Revision ID: 830d564f5cbf
Revises: 0227a7651623
Create Date: 2026-08-04 02:51:42.768418

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '830d564f5cbf'
down_revision: Union[str, Sequence[str], None] = '0227a7651623'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Null means no document asserted a criticality.

    Dropping the server default is the substance of this migration, not
    housekeeping beside it. Nullable alone would leave every insert that
    omits the column still landing on `normal`, which is the hardcode
    ADR-0007 removes wearing a different hat — and it is silent.

    Existing rows keep the `normal` they were written with. They were
    adjudicated under the old rule and re-adjudication is what changes
    them; rewriting 141 conclusions to NULL here would be this migration
    asserting something about them that nobody read off a document.
    """
    op.alter_column(
        "dependencies",
        "criticality",
        existing_type=sa.VARCHAR(length=8),
        nullable=True,
        server_default=None,
    )


def downgrade() -> None:
    """Restore the default, backfilling the rows that have no assertion.

    The backfill is lossy in the way this ADR cares about — it turns "no
    document said" back into "normal" — but the column cannot be NOT NULL
    while those rows exist, and a downgrade that fails is worse than one
    that is honest about what it flattens.
    """
    op.execute(
        "UPDATE dependencies SET criticality = 'normal' WHERE criticality IS NULL"
    )
    op.alter_column(
        "dependencies",
        "criticality",
        existing_type=sa.VARCHAR(length=8),
        nullable=False,
        server_default=sa.text("'normal'::character varying"),
    )
