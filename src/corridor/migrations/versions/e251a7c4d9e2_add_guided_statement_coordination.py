"""add atomic guided statement coordination receipts

Revision ID: e251a7c4d9e2
Revises: d249f8a2e5b4

The Guided Statement command needs one project roster selection boundary and
one grouping receipt.  It does not rewrite any stamped statement, scope, or
Work Decision tables: those rows retain their independent provenance.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e251a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "d249f8a2e5b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_roster_entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("principal_subject", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(trim(principal_subject)) > 0", name="ck_project_roster_principal"),
        sa.CheckConstraint("length(trim(display_name)) > 0", name="ck_project_roster_display_name"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "principal_subject", name="uq_project_roster_principal"),
    )
    op.create_table(
        "statement_coordination_receipts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("commitment_lineage_id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_event_id", sa.BigInteger(), nullable=False),
        sa.Column("scope_decision_id", sa.BigInteger(), nullable=False),
        sa.Column("internal_owner_roster_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("internal_owner_decision_id", sa.BigInteger(), nullable=False),
        sa.Column("next_action_decision_id", sa.BigInteger(), nullable=False),
        sa.Column("milestone_impact_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("audit_log_id", sa.BigInteger(), nullable=False),
        sa.Column("expected_predecessors_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("accepted_facts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidate_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("jsonb_typeof(expected_predecessors_json) = 'object'", name="ck_statement_coordination_receipt_predecessors_object"),
        sa.CheckConstraint("jsonb_typeof(accepted_facts_json) = 'object'", name="ck_statement_coordination_receipt_facts_object"),
        sa.CheckConstraint("length(trim(recorded_by)) > 0", name="ck_statement_coordination_receipt_actor"),
        sa.ForeignKeyConstraint(["audit_log_id"], ["audit_log.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["commitment_lineage_id"], ["commitment_lineages.id"]),
        sa.ForeignKeyConstraint(["dependency_event_id"], ["dependency_events.id"]),
        sa.ForeignKeyConstraint(["internal_owner_decision_id"], ["work_decisions.id"]),
        sa.ForeignKeyConstraint(["internal_owner_roster_entry_id"], ["project_roster_entries.id"]),
        sa.ForeignKeyConstraint(["milestone_impact_decision_id"], ["work_decisions.id"]),
        sa.ForeignKeyConstraint(["next_action_decision_id"], ["work_decisions.id"]),
        sa.ForeignKeyConstraint(["scope_decision_id"], ["dependency_event_scope_decisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("audit_log_id"),
        sa.UniqueConstraint("candidate_id"),
        sa.UniqueConstraint("dependency_event_id"),
        sa.UniqueConstraint("internal_owner_decision_id"),
        sa.UniqueConstraint("milestone_impact_decision_id"),
        sa.UniqueConstraint("next_action_decision_id"),
        sa.UniqueConstraint("scope_decision_id"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade guided statement coordination: grouping receipts "
        "and roster-bound plans cannot be round-tripped safely"
    )
