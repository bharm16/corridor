"""Keep event source kinds as an evolvable checked vocabulary.

Revision ID: d216e4f3a2b1
Revises: c216d4e3f2a1
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d216e4f3a2b1"
down_revision: Union[str, Sequence[str], None] = "c216d4e3f2a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Correct the first deployed migration's accidental native enum."""
    op.drop_constraint(
        "ck_verbal_events_require_call_facts",
        "dependency_events",
        type_="check",
    )
    op.alter_column(
        "dependency_events",
        "source_kind",
        existing_type=sa.Enum("cited", "verbal", name="event_source_kind"),
        server_default=None,
    )
    op.alter_column(
        "dependency_events",
        "source_kind",
        existing_type=sa.Enum("cited", "verbal", name="event_source_kind"),
        type_=sa.String(),
        postgresql_using="source_kind::text",
    )
    op.execute("drop type if exists event_source_kind")
    op.alter_column(
        "dependency_events",
        "source_kind",
        existing_type=sa.String(),
        server_default="cited",
    )
    op.create_check_constraint(
        "ck_dependency_events_source_kind",
        "dependency_events",
        "source_kind in ('cited', 'verbal')",
    )
    op.create_check_constraint(
        "ck_verbal_events_require_call_facts",
        "dependency_events",
        """
        source_kind <> 'verbal' or (
            event_type in ('commitment', 'slip')
            and event_date is not null
            and committed_date is not null
            and length(trim(stated_party)) > 0
            and length(trim(description)) > 0
            and length(trim(created_by)) > 0
        )
        """,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_verbal_events_require_call_facts",
        "dependency_events",
        type_="check",
    )
    op.drop_constraint(
        "ck_dependency_events_source_kind",
        "dependency_events",
        type_="check",
    )
    op.alter_column(
        "dependency_events",
        "source_kind",
        existing_type=sa.String(),
        server_default=None,
    )
    source_kind = sa.Enum("cited", "verbal", name="event_source_kind")
    source_kind.create(op.get_bind(), checkfirst=True)
    op.alter_column(
        "dependency_events",
        "source_kind",
        existing_type=sa.String(),
        type_=source_kind,
        postgresql_using="source_kind::event_source_kind",
    )
    op.alter_column(
        "dependency_events",
        "source_kind",
        existing_type=source_kind,
        server_default="cited",
    )
    op.create_check_constraint(
        "ck_verbal_events_require_call_facts",
        "dependency_events",
        """
        source_kind <> 'verbal' or (
            event_type in ('commitment', 'slip')
            and event_date is not null
            and committed_date is not null
            and length(trim(stated_party)) > 0
            and length(trim(description)) > 0
            and length(trim(created_by)) > 0
        )
        """,
    )
