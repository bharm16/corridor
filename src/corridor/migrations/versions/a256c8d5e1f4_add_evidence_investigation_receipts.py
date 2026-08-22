"""Add immutable Evidence Investigation terminal receipts.

Revision ID: a256c8d5e1f4
Revises: f255b7c4d9e3
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a256c8d5e1f4"
down_revision: Union[str, Sequence[str], None] = "f255b7c4d9e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_investigation_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False, unique=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column("extraction_run_id", sa.BigInteger(), sa.ForeignKey("extraction_runs.id")),
        sa.Column("terminal_status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(128)),
        sa.Column("detail", sa.Text()),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("tool_contract_version", sa.String(128), nullable=False),
        sa.Column("validator_version", sa.String(128), nullable=False),
        sa.Column("transport_gate_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_payload_sha256", sa.String(64), nullable=False),
        sa.Column("read_fingerprint", sa.String(64)),
        sa.Column("budget_json", postgresql.JSONB(), nullable=False),
        sa.Column("usage_json", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "terminal_status in ('options_available', 'human_judgment_needed', 'abstained', 'failed')",
            name="ck_evidence_investigation_runs_terminal_status",
        ),
        sa.CheckConstraint(
            "length(candidate_payload_sha256) = 64 and length(transport_gate_sha256) = 64",
            name="ck_evidence_investigation_runs_hashes",
        ),
    )
    op.create_index("ix_evidence_investigation_runs_project_id", "evidence_investigation_runs", ["project_id"])
    op.create_index("ix_evidence_investigation_runs_candidate_id", "evidence_investigation_runs", ["candidate_id"])
    op.create_table(
        "evidence_investigation_step_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("evidence_investigation_runs.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("opaque_references_json", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_arguments_json", postgresql.JSONB(), nullable=False),
        sa.Column("result_summary_json", postgresql.JSONB(), nullable=False),
        sa.Column("usage_json", postgresql.JSONB(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_investigation_step_ordinal"),
    )
    op.create_index("ix_evidence_investigation_step_receipts_run_id", "evidence_investigation_step_receipts", ["run_id"])
    op.create_table(
        "evidence_investigation_packet_receipts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("evidence_investigation_runs.id"), nullable=False, unique=True),
        sa.Column("packet_json", postgresql.JSONB(), nullable=False),
        sa.Column("validator_outcome", sa.String(32), nullable=False),
        sa.Column("packet_sha256", sa.String(64), nullable=False),
        sa.Column("non_authoritative", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.execute(
        """
        create function refuse_evidence_investigation_receipt_mutation()
        returns trigger language plpgsql as $$
        begin
          raise exception 'Evidence Investigation receipts are append-only';
        end $$
        """
    )
    for table in (
        "evidence_investigation_runs",
        "evidence_investigation_step_receipts",
        "evidence_investigation_packet_receipts",
    ):
        op.execute(
            f"create trigger {table}_append_only before update or delete on {table} "
            "for each row execute function refuse_evidence_investigation_receipt_mutation()"
        )


def downgrade() -> None:
    raise RuntimeError("cannot downgrade immutable Evidence Investigation receipts")
