"""add dependency foreign keys

Revision ID: 9a8c2e4c1b7f
Revises: 3f1c8a04b7d2
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9a8c2e4c1b7f"
down_revision: Union[str, Sequence[str], None] = "3f1c8a04b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_foreign_key(
        "fk_dependencies_external_org_id_external_orgs_id",
        "dependencies",
        "external_orgs",
        ["external_org_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_dependencies_milestone_id_milestones_id",
        "dependencies",
        "milestones",
        ["milestone_id"],
        ["id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "fk_dependencies_milestone_id_milestones_id",
        "dependencies",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_dependencies_external_org_id_external_orgs_id",
        "dependencies",
        type_="foreignkey",
    )
