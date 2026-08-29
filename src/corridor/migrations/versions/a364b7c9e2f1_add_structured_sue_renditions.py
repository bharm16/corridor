"""Register converted XLS renditions and non-adjudicable Evidence proposals.

The prior spreadsheet path could read only OOXML conflict matrices. Treating
SUE probe/test-hole rows as Constraints was rejected because these tables are
supporting observations, not conflict decisions. This successor retains the
original binary Document, records one derived XLSX Document without asserting
equivalence or Supersession, and permits Evidence Candidates that admission
continues to ignore.

Revision ID: a364b7c9e2f1
Revises: f367a8c1d2e4
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a364b7c9e2f1"
down_revision: Union[str, Sequence[str], None] = "f367a8c1d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("candidate_kind", "candidates", type_="check")
    op.create_check_constraint(
        "candidate_kind",
        "candidates",
        "kind in ('dependency', 'event', 'evidence')",
    )
    op.create_table(
        "document_rendition_derivations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source_document_id", sa.BigInteger(), nullable=False),
        sa.Column("derived_document_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("derived_format", sa.String(length=16), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("derived_sha256", sa.String(length=64), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_document_id <> derived_document_id",
            name="ck_rendition_derivation_distinct_documents",
        ),
        sa.CheckConstraint(
            "kind = 'format_conversion' and source_format = 'xls' "
            "and derived_format = 'xlsx'",
            name="ck_rendition_derivation_kind",
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' and "
            "derived_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rendition_derivation_hashes",
        ),
        sa.CheckConstraint(
            "length(trim(tool)) > 0 and length(trim(tool_version)) > 0",
            name="ck_rendition_derivation_tool",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "source_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_rendition_derivation_source_same_project",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "derived_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_rendition_derivation_derived_same_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("derived_document_id"),
    )
    op.create_index(
        "ix_document_rendition_derivations_project_id",
        "document_rendition_derivations",
        ["project_id"],
    )
    op.execute(
        """
        create function reject_document_rendition_derivation_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'document rendition derivations are append-only';
        end;
        $$;
        create trigger document_rendition_derivations_are_immutable
        before update or delete on document_rendition_derivations
        for each row execute function reject_document_rendition_derivation_mutation();
        create trigger document_rendition_derivations_reject_truncate
        before truncate on document_rendition_derivations
        for each statement execute function reject_document_rendition_derivation_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from document_rendition_derivations)
               or exists (select 1 from candidates where kind = 'evidence') then
                raise exception 'cannot erase retained structured SUE provenance';
            end if;
        end
        $$;
        drop trigger if exists document_rendition_derivations_are_immutable
            on document_rendition_derivations;
        drop trigger if exists document_rendition_derivations_reject_truncate
            on document_rendition_derivations;
        drop function if exists reject_document_rendition_derivation_mutation();
        """
    )
    op.drop_index(
        "ix_document_rendition_derivations_project_id",
        table_name="document_rendition_derivations",
    )
    op.drop_table("document_rendition_derivations")
    op.drop_constraint("candidate_kind", "candidates", type_="check")
    op.create_check_constraint(
        "candidate_kind",
        "candidates",
        "kind in ('dependency', 'event')",
    )
