"""Add append-only per-project check-threshold configurations.

The exception engine's thresholds were fixed module constants, varied only
by a test. This table lets Corridor operations declare a project's own
horizons for the existing checks without editing history: each save is a
new retained identity, the newest row is effective, and a trigger keeps the
rows append-only so an earlier configuration and the reports published under
it stay readable.

Revision ID: b4d1e2f3a5c6
Revises: a364b7c9e2f1
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4d1e2f3a5c6"
down_revision: Union[str, Sequence[str], None] = "a364b7c9e2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_check_configurations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("ruleset_version", sa.String(length=32), nullable=False),
        sa.Column("stale_days", sa.Integer(), nullable=False),
        sa.Column("due_soon_days", sa.Integer(), nullable=False),
        sa.Column("action_due_soon_days", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stale_days between 1 and 3650",
            name="ck_project_check_configurations_stale_days",
        ),
        sa.CheckConstraint(
            "due_soon_days between 1 and 3650",
            name="ck_project_check_configurations_due_soon_days",
        ),
        sa.CheckConstraint(
            "action_due_soon_days between 1 and 3650",
            name="ck_project_check_configurations_action_due_soon_days",
        ),
        sa.CheckConstraint(
            "length(trim(ruleset_version)) > 0",
            name="ck_project_check_configurations_ruleset_version",
        ),
        sa.CheckConstraint(
            "length(trim(created_by)) > 0",
            name="ck_project_check_configurations_created_by",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_project_check_configurations_project_id",
        "project_check_configurations",
        ["project_id"],
    )
    op.execute(
        """
        create function reject_project_check_configuration_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'project check configurations are append-only';
        end;
        $$;
        create trigger project_check_configurations_are_immutable
        before update or delete on project_check_configurations
        for each row execute function reject_project_check_configuration_mutation();
        create trigger project_check_configurations_reject_truncate
        before truncate on project_check_configurations
        for each statement execute function reject_project_check_configuration_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from project_check_configurations) then
                raise exception 'cannot erase declared check configuration history';
            end if;
        end
        $$;
        drop trigger if exists project_check_configurations_are_immutable
            on project_check_configurations;
        drop trigger if exists project_check_configurations_reject_truncate
            on project_check_configurations;
        drop function if exists reject_project_check_configuration_mutation();
        """
    )
    op.drop_index(
        "ix_project_check_configurations_project_id",
        table_name="project_check_configurations",
    )
    op.drop_table("project_check_configurations")
