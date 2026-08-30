"""Discover and process documents from one connected location (#350).

Two durable tables back the one concrete connected-location adapter. A
``discovered_references`` row is the single coalesced identity for a reference
observed at a location — proposed until an attributable authorization declares
the kind the bytes cannot state, registered once a validated fetch turns it into
a Document. A ``source_fetch_attempts`` row retains every fetch outcome against
an authorized reference, so a failure sits beside the last good retrieval and a
crash between fetching and registering cannot advertise partial content as
complete. Neither table is a new writer of Constraint Records; extraction and
Record Inclusion remain the standing project-processing pass's job.

Revision ID: d3f1a9c05b21
Revises: c345a9f1d2e3
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3f1a9c05b21"
down_revision: Union[str, Sequence[str], None] = "d359a1b2c3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discovered_references",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("location_id", sa.String(length=128), nullable=False),
        sa.Column("reference_key", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("archive_url", sa.Text(), nullable=True),
        sa.Column("member", sa.Text(), nullable=True),
        sa.Column("observed_title", sa.Text(), nullable=True),
        sa.Column("observed_type_hint", sa.String(length=64), nullable=True),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "observed_count", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "state",
            sa.Enum(
                "proposed",
                "authorized",
                "registered",
                name="discovered_reference_state",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="proposed",
            nullable=False,
        ),
        sa.Column("authorized_doc_type", sa.String(length=32), nullable=True),
        sa.Column("authorized_registry_id", sa.String(length=128), nullable=True),
        sa.Column("authorized_by", sa.String(length=128), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registered_document_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "observed_count >= 1", name="ck_discovered_reference_observed_count"
        ),
        sa.CheckConstraint(
            "authorized_doc_type is null or authorized_doc_type in "
            "('matrix','minutes','agreement','email','plan','schedule','spec',"
            "'status_report','other')",
            name="ck_discovered_reference_authorized_doc_type",
        ),
        sa.CheckConstraint(
            "(state = 'proposed' and authorized_at is null "
            "and registered_document_id is null) or "
            "(state = 'authorized' and authorized_at is not null "
            "and authorized_doc_type is not null) or "
            "(state = 'registered' and authorized_at is not null "
            "and registered_document_id is not null)",
            name="ck_discovered_reference_state",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "reference_key", name="uq_discovered_reference_identity"
        ),
    )
    op.create_index(
        "ix_discovered_references_project_id",
        "discovered_references",
        ["project_id"],
    )
    op.create_index(
        "ix_discovered_references_project_state",
        "discovered_references",
        ["project_id", "state"],
    )

    op.create_table(
        "source_fetch_attempts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("reference_key", sa.String(length=64), nullable=True),
        sa.Column("location_id", sa.String(length=128), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "registered",
                "unchanged",
                "drift",
                "failed",
                "budget_exhausted",
                name="source_fetch_outcome",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("resolved_url", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("byte_count", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("prior_sha256", sa.String(length=64), nullable=True),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "resumable", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sha256 is null or sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_fetch_attempt_sha256",
        ),
        sa.CheckConstraint(
            "prior_sha256 is null or prior_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_fetch_attempt_prior_sha256",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_fetch_attempts_project_id",
        "source_fetch_attempts",
        ["project_id"],
    )
    op.create_index(
        "ix_source_fetch_attempts_project_outcome",
        "source_fetch_attempts",
        ["project_id", "outcome"],
    )
    op.create_index(
        "ix_source_fetch_attempts_reference",
        "source_fetch_attempts",
        ["project_id", "reference_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_fetch_attempts_reference", table_name="source_fetch_attempts"
    )
    op.drop_index(
        "ix_source_fetch_attempts_project_outcome",
        table_name="source_fetch_attempts",
    )
    op.drop_index(
        "ix_source_fetch_attempts_project_id", table_name="source_fetch_attempts"
    )
    op.drop_table("source_fetch_attempts")
    op.drop_index(
        "ix_discovered_references_project_state",
        table_name="discovered_references",
    )
    op.drop_index(
        "ix_discovered_references_project_id", table_name="discovered_references"
    )
    op.drop_table("discovered_references")
