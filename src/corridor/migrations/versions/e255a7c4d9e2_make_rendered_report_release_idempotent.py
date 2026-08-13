"""Allow each rendered External Report artifact to be released only once.

Revision ID: e255a7c4d9e2
Revises: d255a7c4d9e2

The release service treats a retried authorization as the original receipt.
This successor lets pre-existing artifacts remain unreleased while the unique
index makes that idempotence durable under concurrent requests.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e255a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "d255a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            "select artifact_id, count(*) as receipt_count "
            "from external_report_releases "
            "where artifact_id is not null "
            "group by artifact_id having count(*) > 1 "
            "order by artifact_id limit 1"
        )
    ).one_or_none()
    if duplicate is not None:
        raise RuntimeError(
            "cannot add External Report release idempotence: rendered artifact "
            f"{duplicate.artifact_id} has {duplicate.receipt_count} immutable "
            "release receipts; preserve and adjudicate that history before retrying"
        )
    op.create_unique_constraint(
        "uq_external_report_releases_artifact_id",
        "external_report_releases",
        ["artifact_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade External Report release idempotence: immutable "
        "authorization history would lose its one-artifact boundary"
    )
