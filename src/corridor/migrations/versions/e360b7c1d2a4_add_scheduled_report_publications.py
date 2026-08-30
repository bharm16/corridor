"""Retain scheduled weekly report readings and prepared-PDF bindings.

Revision ID: e360b7c1d2a4
Revises: e4c8b1a6d3f7
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e360b7c1d2a4"
down_revision: Union[str, Sequence[str], None] = "f360a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_report_publications",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("occurrence_id", sa.BigInteger(), nullable=False),
        sa.Column("schedule_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("configuration_version", sa.String(64), nullable=False),
        sa.Column("provenance_mode", sa.String(32), nullable=False),
        sa.Column("predecessor_release_id", sa.BigInteger(), nullable=True),
        sa.Column("prepared_artifact_id", sa.BigInteger(), nullable=True),
        sa.Column("evaluated_on", sa.Date(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("comparison_window_days", sa.Integer(), nullable=True),
        sa.Column("ruleset_version", sa.String(64), nullable=False),
        sa.Column("thresholds_json", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provenance_mode in ('all-supported-sources', 'document-only')",
            name="ck_scheduled_report_publication_provenance_mode",
        ),
        sa.CheckConstraint(
            "(predecessor_release_id is null) = (window_start is null)",
            name="ck_scheduled_report_publication_predecessor_window",
        ),
        sa.CheckConstraint(
            "(window_start is null) = (comparison_window_days is null)",
            name="ck_scheduled_report_publication_window_days",
        ),
        sa.CheckConstraint(
            "comparison_window_days is null or comparison_window_days >= 0",
            name="ck_scheduled_report_publication_window_nonneg",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot_json) = 'object'",
            name="ck_scheduled_report_publication_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(thresholds_json) = 'object'",
            name="ck_scheduled_report_publication_thresholds_object",
        ),
        sa.ForeignKeyConstraint(["occurrence_id"], ["due_work_occurrences.id"]),
        sa.ForeignKeyConstraint(["schedule_id"], ["due_work_schedules.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        # predecessor_release_id and prepared_artifact_id are plain reference
        # ids into append-only stores, deliberately not foreign keys: this row
        # must not change the truncate or deletion semantics of the release and
        # artifact tables, whose rows are immutable and never removed.
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "occurrence_id", name="uq_scheduled_report_publication_occurrence"
        ),
    )
    op.create_index(
        "ix_scheduled_report_publications_project_id",
        "scheduled_report_publications",
        ["project_id"],
    )
    # The retained reading is append-only: a refreshed working view or a later
    # release must not rewrite the predecessor or comparison window this row
    # recorded (ADR-0053).
    op.execute("""
        create function reject_scheduled_report_publications_mutation()
        returns trigger language plpgsql as $$
        begin raise exception 'scheduled_report_publications are append-only'; end; $$;
        create trigger scheduled_report_publications_are_immutable
        before update or delete on scheduled_report_publications
        for each row execute function reject_scheduled_report_publications_mutation();
        create trigger scheduled_report_publications_reject_truncate
        before truncate on scheduled_report_publications
        for each statement execute function reject_scheduled_report_publications_mutation();
    """)


def downgrade() -> None:
    op.execute("""
        do $$ begin
          if exists (select 1 from scheduled_report_publications) then
            raise exception 'cannot erase retained scheduled_report_publications';
          end if;
        end $$;
        drop trigger if exists scheduled_report_publications_are_immutable
          on scheduled_report_publications;
        drop trigger if exists scheduled_report_publications_reject_truncate
          on scheduled_report_publications;
        drop function if exists reject_scheduled_report_publications_mutation();
    """)
    op.drop_index(
        "ix_scheduled_report_publications_project_id",
        table_name="scheduled_report_publications",
    )
    op.drop_table("scheduled_report_publications")
