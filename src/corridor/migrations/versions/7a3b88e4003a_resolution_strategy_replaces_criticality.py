"""resolution_strategy replaces criticality

Revision ID: 7a3b88e4003a
Revises: 830d564f5cbf
Create Date: 2026-08-04 11:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7a3b88e4003a"
down_revision: Union[str, Sequence[str], None] = "830d564f5cbf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGIES = (
    "relocate",
    "remove",
    "abandon_deactivate",
    "adjust_vertical",
    "protect_in_place",
    "change_design",
    "exception",
)
CRITICALITIES = ("critical", "high", "normal")


def upgrade() -> None:
    """Drop the scale, add the strategy the document actually asserts.

    Nothing is carried across, and that is the migration's substance
    rather than laziness. `criticality` and `resolution_strategy` are not
    the same claim in different words: the first was a severity scale no
    document in the corpus fills in and no published method defines, the
    second is one of SHRP2 R15B's resolution alternatives (ADR-0009).
    There is no value of the old column that tells you a value of the new
    one — all 141 live rows read `normal`, which was the hardcode
    ADR-0007 removed, and means only "nobody ever set this".

    So every existing Dependency lands on NULL: no document asserted a
    strategy for it, which is true. Re-adjudication is what fills them,
    and Project A's inventory will never fill them at all because its
    document does not record a resolution.

    No server default, for the same reason the last migration dropped
    one: a default is this column claiming something no document said.
    """
    op.add_column(
        "dependencies",
        sa.Column(
            "resolution_strategy",
            sa.Enum(
                *STRATEGIES,
                name="resolution_strategy",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
    )
    op.drop_column("dependencies", "criticality")


def downgrade() -> None:
    """Restore `criticality`, nullable and undefaulted.

    Restored as it stood at 830d564f5cbf — nullable, no server default —
    not as the v0 schema had it. Reinstating `server_default='normal'`
    here would resurrect the hardcode two migrations after it was
    deliberately removed.

    The strategy values are dropped rather than mapped back. A downgrade
    is honest about that: `remove` and `adjust_vertical` both collapse to
    `normal` under the old vocabulary, so the mapping loses the very
    distinction the column was added to record.
    """
    op.add_column(
        "dependencies",
        sa.Column(
            "criticality",
            sa.Enum(
                *CRITICALITIES,
                name="criticality",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
    )
    op.drop_column("dependencies", "resolution_strategy")
