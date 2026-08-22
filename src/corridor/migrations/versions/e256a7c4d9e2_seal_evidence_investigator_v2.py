"""Seal Evidence Investigator v2 configuration lineage.

Revision ID: e256a7c4d9e2
Revises: d256f1a8b4c7
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e256a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "d256f1a8b4c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Null means immutable legacy configuration whose exact bytes cannot be
    # reconstructed. New application writes always populate these columns.
    op.add_column(
        "evidence_investigation_runs",
        sa.Column("adapter_contract_version", sa.String(128), nullable=True),
    )
    op.add_column(
        "evidence_investigation_runs",
        sa.Column("prompt_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        "evidence_investigation_shadow_cases",
        sa.Column("prompt_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        "evidence_investigation_shadow_cases",
        sa.Column("adapter_contract_version", sa.String(128), nullable=True),
    )
    op.add_column(
        "evidence_investigation_shadow_cases",
        sa.Column("transport_gate_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        "evidence_investigation_shadow_cases",
        sa.Column("budget_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        ],
    )


def downgrade() -> None:
    raise RuntimeError("cannot erase immutable Evidence Investigator configuration lineage")
