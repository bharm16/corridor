"""Add the plain current Project Record view.

Revision ID: 8fc4c747b2d9
Revises: 20c7d970be63
"""

from alembic import op


revision = "8fc4c747b2d9"
down_revision = "20c7d970be63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Project effective Fact decisions through an ordinary PostgreSQL view."""

    op.execute(
        """
        create view current_project_record as
        select
            decisions.project_id,
            proposals.candidate_id,
            candidates.merged_into as dependency_id,
            decisions.subject_key,
            decisions.fact_type,
            facts.text_value,
            facts.date_value,
            facts.date_range_start,
            facts.date_range_end,
            facts.external_org_value_id,
            facts.document_value_id,
            decisions.id as decision_id,
            facts.id as fact_id,
            decisions.revision_id,
            decisions.decided_at
        from fact_decisions decisions
        join facts on facts.id = decisions.fact_id
        left join extracted_proposals proposals
          on proposals.project_id = facts.project_id
         and proposals.document_id = facts.document_id
         and proposals.extraction_run_id = facts.extraction_run_id
         and proposals.subject_key = facts.subject_key
        left join candidates on candidates.id = proposals.candidate_id
        where decisions.superseded_by is null;
        """
    )


def downgrade() -> None:
    """The supported migration window never removes a current-record seam."""

    raise RuntimeError("current Project Record view downgrade is unsupported")
