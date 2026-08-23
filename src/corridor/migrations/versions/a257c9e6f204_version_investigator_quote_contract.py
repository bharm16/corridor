"""Version the fixed Evidence Investigator quote contract.

Revision ID: a257c9e6f204
Revises: f256b8d5e1f3
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a257c9e6f204"
down_revision: Union[str, Sequence[str], None] = "f256b8d5e1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Null preserves legacy cases whose earlier tool behavior cannot be
    # retroactively relabelled as the fixed contract.
    op.add_column(
        "evidence_investigation_shadow_cases",
        sa.Column("tool_contract_version", sa.String(128), nullable=True),
    )
    op.drop_constraint(
        "uq_evidence_investigation_shadow_case_identity",
        "evidence_investigation_shadow_cases",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_evidence_investigation_shadow_case_identity",
        "evidence_investigation_shadow_cases",
        [
            "candidate_id",
            "read_fingerprint",
            "model",
            "prompt_version",
            "prompt_sha256",
            "adapter_contract_version",
            "tool_contract_version",
        ],
    )


def downgrade() -> None:
    raise RuntimeError("cannot erase immutable investigator tool-contract lineage")
