"""Prevent one human outcome from labeling multiple shadow cases.

Revision ID: d256f1a8b4c7
Revises: c256e0f7a3b6
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d256f1a8b4c7"
down_revision: Union[str, Sequence[str], None] = "c256e0f7a3b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _identity(candidate_id: int, identities: dict, unresolved: bool) -> str:
    content = {
        "candidate_id": candidate_id,
        "candidate_disposition_id": identities.get("candidate_disposition_id"),
        "statement_coordination_receipt_id": identities.get(
            "statement_coordination_receipt_id"
        ),
        "reversal_ids": identities.get("reversal_ids") or [],
        "unresolved": unresolved,
    }
    encoded = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def upgrade() -> None:
    op.add_column(
        "evidence_investigation_shadow_outcomes",
        sa.Column("human_outcome_identity", sa.String(64), nullable=True),
    )
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "select outcome.id, shadow_case.candidate_id, "
                "outcome.outcome_identities_json, outcome.unresolved "
                "from evidence_investigation_shadow_outcomes outcome "
                "join evidence_investigation_shadow_cases shadow_case "
                "on shadow_case.id = outcome.shadow_case_id"
            )
        ).mappings()
    )
    op.execute(
        "alter table evidence_investigation_shadow_outcomes disable trigger "
        "evidence_investigation_shadow_outcomes_append_only"
    )
    try:
        for row in rows:
            connection.execute(
                sa.text(
                    "update evidence_investigation_shadow_outcomes "
                    "set human_outcome_identity = :identity where id = :id"
                ),
                {
                    "id": row["id"],
                    "identity": _identity(
                        row["candidate_id"],
                        row["outcome_identities_json"],
                        row["unresolved"],
                    ),
                },
            )
    finally:
        op.execute(
            "alter table evidence_investigation_shadow_outcomes enable trigger "
            "evidence_investigation_shadow_outcomes_append_only"
        )
    op.alter_column(
        "evidence_investigation_shadow_outcomes",
        "human_outcome_identity",
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_evidence_investigation_shadow_human_outcome",
        "evidence_investigation_shadow_outcomes",
        ["human_outcome_identity"],
    )


def downgrade() -> None:
    raise RuntimeError("cannot downgrade immutable shadow outcome identities")
