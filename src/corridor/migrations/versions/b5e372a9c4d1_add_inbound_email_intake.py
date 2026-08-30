"""Store shared-address inbound mail, deterministic threads, and route triage.

Revision ID: b5e372a9c4d1
Revises: b4d1e2f3a5c6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b5e372a9c4d1"
down_revision: Union[str, Sequence[str], None] = "c355a7d9e2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "intake_project_identifiers",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("value_normalized", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(trim(kind)) > 0", name="ck_intake_identifier_kind"),
        sa.CheckConstraint("length(trim(value_normalized)) > 0", name="ck_intake_identifier_value"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "value_normalized", "project_id"),
    )
    op.create_index("ix_intake_project_identifiers_project_id", "intake_project_identifiers", ["project_id"])
    op.create_table(
        "inbound_threads",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("dependency_id", sa.BigInteger(), nullable=True),
        sa.Column("bound_by_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dependency_id"], ["dependencies.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("headers_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("route_status", sa.String(length=16), nullable=False),
        sa.Column("route_evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.Column("attachments_json", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("raw_sha256 ~ '^[0-9a-f]{64}$'", name="ck_inbound_message_sha256"),
        sa.CheckConstraint("route_status in ('routed', 'triage')", name="ck_inbound_message_route"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["thread_id"], ["inbound_threads.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_sha256"),
        sa.UniqueConstraint("message_id", name="uq_inbound_message_message_id"),
    )
    op.create_index("ix_inbound_messages_project_id", "inbound_messages", ["project_id"])
    op.create_index("ix_inbound_messages_thread_id", "inbound_messages", ["thread_id"])
    op.create_foreign_key("fk_inbound_threads_bound_by_message", "inbound_threads", "inbound_messages", ["bound_by_message_id"], ["id"])
    op.create_table(
        "inbound_thread_readings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("closing_message_id", sa.BigInteger(), nullable=False),
        sa.Column("resolution", sa.String(length=16), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("open_question", sa.Text(), nullable=True),
        sa.Column("turn_context_json", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("resolution in ('concluded', 'unresolved')", name="ck_inbound_thread_reading_resolution"),
        sa.CheckConstraint("(resolution = 'concluded') = (candidate_id is not null)", name="ck_inbound_thread_reading_claim"),
        sa.CheckConstraint("(resolution = 'unresolved') = (open_question is not null)", name="ck_inbound_thread_reading_question"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["closing_message_id"], ["inbound_messages.id"]),
        sa.ForeignKeyConstraint(["thread_id"], ["inbound_threads.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", "closing_message_id"),
    )
    op.create_index("ix_inbound_thread_readings_thread_id", "inbound_thread_readings", ["thread_id"])
    op.create_table(
        "inbound_route_triage",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_project_ids", postgresql.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("resolved_project_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("state in ('pending', 'resolved')", name="ck_inbound_route_triage_state"),
        sa.ForeignKeyConstraint(["resolved_project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["thread_id"], ["inbound_threads.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id"),
    )


def downgrade() -> None:
    op.drop_table("inbound_route_triage")
    op.drop_index("ix_inbound_thread_readings_thread_id", table_name="inbound_thread_readings")
    op.drop_table("inbound_thread_readings")
    op.drop_constraint("fk_inbound_threads_bound_by_message", "inbound_threads", type_="foreignkey")
    op.drop_index("ix_inbound_messages_thread_id", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_project_id", table_name="inbound_messages")
    op.drop_table("inbound_messages")
    op.drop_table("inbound_threads")
    op.drop_index("ix_intake_project_identifiers_project_id", table_name="intake_project_identifiers")
    op.drop_table("intake_project_identifiers")
