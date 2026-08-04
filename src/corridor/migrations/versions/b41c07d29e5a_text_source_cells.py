"""text_source gains 'cells' for spreadsheet sources

Revision ID: b41c07d29e5a
Revises: 7a3b88e4003a
Create Date: 2026-08-04 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b41c07d29e5a"
down_revision: Union[str, Sequence[str], None] = "7a3b88e4003a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT = "text_source"
TABLE = "doc_pages"


def upgrade() -> None:
    """Where a page's text came from, for a source that has no page.

    ADR-0005 makes the structured original the Document of Record, so
    ingestion stops being PDF-only (#60). A worksheet's text is generated
    from its cells rather than recovered from a layout, and the difference
    is not cosmetic: it is what lets a citation against a spreadsheet
    verify **exactly**. The 0.9 threshold exists for print damage —
    separators lost between spans, cells clipped at their boundaries —
    and none of that can happen to a value read straight out of a cell.

    So the reliability ordering this column already encodes gains a third
    step above the other two, rather than a spreadsheet borrowing
    `text_layer` and becoming indistinguishable from a PDF's.

    A CHECK rather than a native type, so this is one line (`_enum`).
    """
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(
        CONSTRAINT, TABLE, sa.column("text_source").in_(["text_layer", "ocr", "cells"])
    )


def downgrade() -> None:
    """Narrow the constraint back, and refuse if it would lie.

    A `cells` row cannot be re-labelled `text_layer` on the way down: its
    text was never a text layer, and a downgrade that rewrites data to fit
    a narrower constraint destroys the distinction the column exists for.
    Delete the spreadsheet documents first if this is really wanted.
    """
    rows = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE text_source = 'cells'")
    ).scalar()
    if rows:
        raise RuntimeError(
            f"{rows} doc_pages rows read from cells; downgrading would have "
            "to relabel them as something they are not. Delete the "
            "spreadsheet documents first."
        )
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(
        CONSTRAINT, TABLE, sa.column("text_source").in_(["text_layer", "ocr"])
    )
