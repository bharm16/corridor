"""Add prospective hidden Evidence Investigation shadow cohort.

Revision ID: b256d9e6f2a5
Revises: a256c8d5e1f4
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b256d9e6f2a5"
down_revision: Union[str, Sequence[str], None] = "a256c8d5e1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_investigation_shadow_cases",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False, unique=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column("extraction_run_id", sa.BigInteger(), sa.ForeignKey("extraction_runs.id")),
        sa.Column("candidate_payload_sha256", sa.String(64), nullable=False),
        sa.Column("read_fingerprint", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("case_json", postgresql.JSONB(), nullable=False),
        sa.Column("registered_evidence_json", postgresql.JSONB(), nullable=False),
        sa.Column("option_population_json", postgresql.JSONB(), nullable=False),
        sa.Column("option_population_sha256", sa.String(64), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", "read_fingerprint", "model", "prompt_version", name="uq_evidence_investigation_shadow_case_identity"),
    )
    op.create_index("ix_evidence_investigation_shadow_cases_project_id", "evidence_investigation_shadow_cases", ["project_id"])
    op.create_index("ix_evidence_investigation_shadow_cases_candidate_id", "evidence_investigation_shadow_cases", ["candidate_id"])
    op.create_table(
        "evidence_investigation_shadow_executions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("shadow_case_id", sa.BigInteger(), sa.ForeignKey("evidence_investigation_shadow_cases.id"), nullable=False, unique=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("evidence_investigation_runs.id"), nullable=False, unique=True),
        sa.Column("execution_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "evidence_investigation_review_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("shadow_case_id", sa.BigInteger(), sa.ForeignKey("evidence_investigation_shadow_cases.id"), nullable=False),
        sa.Column("boundary", sa.String(16), nullable=False),
        sa.Column("principal", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("shadow_case_id", "boundary", name="uq_shadow_review_boundary"),
        sa.CheckConstraint("boundary in ('start', 'end')", name="ck_shadow_review_boundary"),
    )
    op.create_index("ix_evidence_investigation_review_observations_shadow_case_id", "evidence_investigation_review_observations", ["shadow_case_id"])
    op.create_table(
        "evidence_investigation_shadow_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("shadow_case_id", sa.BigInteger(), sa.ForeignKey("evidence_investigation_shadow_cases.id"), nullable=False, unique=True),
        sa.Column("candidate_disposition", sa.String(32)),
        sa.Column("scope_mode", sa.String(32)),
        sa.Column("selected_dependency_ids_json", postgresql.JSONB(), nullable=False),
        sa.Column("correction", sa.Boolean(), nullable=False),
        sa.Column("undo", sa.Boolean(), nullable=False),
        sa.Column("unresolved", sa.Boolean(), nullable=False),
        sa.Column("outcome_identities_json", postgresql.JSONB(), nullable=False),
        sa.Column("strata_json", postgresql.JSONB(), nullable=False),
        sa.Column("review_seconds", sa.Float()),
        sa.Column("outcome_sha256", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table in (
        "evidence_investigation_shadow_cases",
        "evidence_investigation_shadow_executions",
        "evidence_investigation_review_observations",
        "evidence_investigation_shadow_outcomes",
    ):
        op.execute(
            f"create trigger {table}_append_only before update or delete on {table} "
            "for each row execute function refuse_evidence_investigation_receipt_mutation()"
        )


def downgrade() -> None:
    raise RuntimeError("cannot downgrade immutable prospective shadow history")
