"""Retain declared unreadable-cell reading profiles, receipts, and gate.

Revision ID: e7a2f4c9d1b6
Revises: d359a1b2c3e4
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e7a2f4c9d1b6"
down_revision: Union[str, Sequence[str], None] = "d3f1a9c05b21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_APPEND_ONLY_TABLES = (
    "unreadable_cell_reading_profiles",
    "unreadable_cell_reading_runs",
    "unreadable_cell_reading_steps",
    "unreadable_cell_resolutions",
    "unreadable_cell_admission_activations",
)


def upgrade() -> None:
    op.create_table(
        "unreadable_cell_reading_profiles",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_version", sa.String(64), nullable=False),
        sa.Column("min_readable_text_chars", sa.Integer(), nullable=False),
        sa.Column("page_scope_json", postgresql.JSONB(), nullable=False),
        sa.Column("image_op_identities_json", postgresql.JSONB(), nullable=False),
        sa.Column("read_identities_json", postgresql.JSONB(), nullable=False),
        sa.Column("max_cells_per_page", sa.Integer(), nullable=False),
        sa.Column("max_image_ops_per_cell", sa.Integer(), nullable=False),
        sa.Column("max_reads_per_cell", sa.Integer(), nullable=False),
        sa.Column("max_corpus_reads_per_cell", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(profile_version)) > 0",
            name="ck_unreadable_cell_profile_version",
        ),
        sa.CheckConstraint(
            "min_readable_text_chars between 1 and 100000",
            name="ck_unreadable_cell_profile_min_chars",
        ),
        sa.CheckConstraint(
            "max_cells_per_page between 1 and 10000",
            name="ck_unreadable_cell_profile_max_cells",
        ),
        sa.CheckConstraint(
            "max_image_ops_per_cell between 1 and 100",
            name="ck_unreadable_cell_profile_max_image_ops",
        ),
        sa.CheckConstraint(
            "max_reads_per_cell between 1 and 100",
            name="ck_unreadable_cell_profile_max_reads",
        ),
        sa.CheckConstraint(
            "max_corpus_reads_per_cell between 1 and 100",
            name="ck_unreadable_cell_profile_max_corpus_reads",
        ),
        sa.CheckConstraint(
            "timeout_seconds between 1 and 600",
            name="ck_unreadable_cell_profile_timeout",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(page_scope_json) = 'array'",
            name="ck_unreadable_cell_profile_page_scope",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(image_op_identities_json) = 'array'",
            name="ck_unreadable_cell_profile_image_ops",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(read_identities_json) = 'array'",
            name="ck_unreadable_cell_profile_reads",
        ),
        sa.CheckConstraint(
            "length(trim(created_by)) > 0",
            name="ck_unreadable_cell_profile_actor",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_unreadable_cell_reading_profiles_project_id",
        "unreadable_cell_reading_profiles",
        ["project_id"],
    )
    op.create_table(
        "unreadable_cell_reading_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("cell_key", sa.String(128), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("profile_version", sa.String(64), nullable=True),
        sa.Column("page_image_sha256", sa.String(64), nullable=False),
        sa.Column("read_fingerprint", sa.String(64), nullable=True),
        sa.Column("terminal_state", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(160), nullable=True),
        sa.Column("outcome_json", postgresql.JSONB(), nullable=True),
        sa.Column("validator_outcome", sa.String(32), nullable=False),
        sa.Column("budget_json", postgresql.JSONB(), nullable=False),
        sa.Column("usage_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "non_authoritative",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "terminal_state in ('rescued', 'corroborated', 'reading_only', "
            "'failure', 'stale_input', 'budget_exhausted', 'refused', "
            "'validation_refused', 'runtime_failure')",
            name="ck_unreadable_cell_run_state",
        ),
        sa.CheckConstraint(
            "page_image_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_unreadable_cell_run_image_sha",
        ),
        sa.CheckConstraint(
            "read_fingerprint is null or read_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_unreadable_cell_run_fingerprint",
        ),
        sa.CheckConstraint(
            "non_authoritative", name="ck_unreadable_cell_run_non_auth"
        ),
        sa.CheckConstraint(
            "length(trim(cell_key)) > 0", name="ck_unreadable_cell_run_cell_key"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["unreadable_cell_reading_profiles.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(
        "ix_unreadable_cell_reading_runs_project_id",
        "unreadable_cell_reading_runs",
        ["project_id"],
    )
    op.create_index(
        "ix_unreadable_cell_reading_runs_document_id",
        "unreadable_cell_reading_runs",
        ["document_id"],
    )
    op.create_table(
        "unreadable_cell_reading_steps",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(24), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("arguments_json", postgresql.JSONB(), nullable=False),
        sa.Column("result_summary_json", postgresql.JSONB(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "step_type in ('image_op', 'read', 'corpus_read')",
            name="ck_unreadable_cell_step_type",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["unreadable_cell_reading_runs.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_unreadable_cell_step_ordinal"),
    )
    op.create_index(
        "ix_unreadable_cell_reading_steps_run_id",
        "unreadable_cell_reading_steps",
        ["run_id"],
    )
    op.create_table(
        "unreadable_cell_resolutions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("cell_key", sa.String(128), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("corroboration_document_id", sa.BigInteger(), nullable=True),
        sa.Column("corroboration_page_no", sa.Integer(), nullable=True),
        sa.Column("corroboration_quote", sa.Text(), nullable=True),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=True),
        sa.Column("policy_sha256", sa.String(64), nullable=True),
        sa.Column("recorded_by", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state in ('unconfirmed', 'corroborated', 'absent', 'admitted')",
            name="ck_unreadable_cell_resolution_state",
        ),
        sa.CheckConstraint(
            "origin in ('harness', 'corroboration_upgrade', 'admission', "
            "'human_decision')",
            name="ck_unreadable_cell_resolution_origin",
        ),
        sa.CheckConstraint(
            "policy_sha256 is null or policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_unreadable_cell_resolution_sha",
        ),
        sa.CheckConstraint(
            "length(trim(cell_key)) > 0",
            name="ck_unreadable_cell_resolution_cell_key",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(
            ["corroboration_document_id"], ["documents.id"]
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["unreadable_cell_reading_runs.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_unreadable_cell_resolutions_project_id",
        "unreadable_cell_resolutions",
        ["project_id"],
    )
    op.create_index(
        "ix_unreadable_cell_resolutions_document_id",
        "unreadable_cell_resolutions",
        ["document_id"],
    )
    op.create_index(
        "ix_unreadable_cell_resolutions_cell",
        "unreadable_cell_resolutions",
        ["project_id", "document_id", "page_no", "cell_key"],
    )
    op.create_table(
        "unreadable_cell_admission_activations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("replay_case_count", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(160), nullable=False),
        sa.Column("recorded_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action in ('activate', 'suspend')",
            name="ck_unreadable_cell_admission_action",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_unreadable_cell_admission_reason",
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_unreadable_cell_admission_actor",
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_unreadable_cell_admission_sha",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_unreadable_cell_admission_activations_project_id",
        "unreadable_cell_admission_activations",
        ["project_id"],
    )
    for table in _APPEND_ONLY_TABLES:
        op.execute(f"""
            create function reject_{table}_mutation() returns trigger language plpgsql as $$
            begin raise exception '{table} are append-only'; end; $$;
            create trigger {table}_are_immutable before update or delete on {table}
            for each row execute function reject_{table}_mutation();
            create trigger {table}_reject_truncate before truncate on {table}
            for each statement execute function reject_{table}_mutation();
        """)


def downgrade() -> None:
    for table in _APPEND_ONLY_TABLES:
        op.execute(f"""
            do $$ begin
              if exists (select 1 from {table}) then raise exception 'cannot erase retained {table}'; end if;
            end $$;
            drop trigger if exists {table}_are_immutable on {table};
            drop trigger if exists {table}_reject_truncate on {table};
            drop function if exists reject_{table}_mutation();
        """)
    op.drop_table("unreadable_cell_admission_activations")
    op.drop_table("unreadable_cell_resolutions")
    op.drop_table("unreadable_cell_reading_steps")
    op.drop_table("unreadable_cell_reading_runs")
    op.drop_table("unreadable_cell_reading_profiles")
