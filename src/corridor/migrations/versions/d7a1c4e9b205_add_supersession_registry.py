"""add declared document supersession registry metadata

Revision ID: d7a1c4e9b205
Revises: c4e9a61d2b73
Create Date: 2026-08-05 15:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7a1c4e9b205"
down_revision: Union[str, Sequence[str], None] = "c4e9a61d2b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Store only complete, source-checkable supersession declarations."""
    op.add_column(
        "documents", sa.Column("registry_id", sa.String(length=128), nullable=True)
    )
    op.add_column("documents", sa.Column("superseded_on", sa.Date(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("supersession_source_document_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("supersession_source_page", sa.Integer(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_documents_project_registry_id",
        "documents",
        ["project_id", "registry_id"],
    )
    op.create_unique_constraint(
        "uq_documents_project_id_id",
        "documents",
        ["project_id", "id"],
    )
    op.drop_constraint("documents_superseded_by_fkey", "documents", type_="foreignkey")
    op.create_foreign_key(
        "fk_documents_superseded_by_same_project",
        "documents",
        "documents",
        ["project_id", "superseded_by"],
        ["project_id", "id"],
    )
    op.create_foreign_key(
        "fk_documents_supersession_source_same_project",
        "documents",
        "documents",
        ["project_id", "supersession_source_document_id"],
        ["project_id", "id"],
    )
    op.create_foreign_key(
        "fk_documents_supersession_source_page",
        "documents",
        "doc_pages",
        ["supersession_source_document_id", "supersession_source_page"],
        ["document_id", "page_no"],
    )
    op.create_check_constraint(
        "ck_documents_no_self_supersession",
        "documents",
        "superseded_by is null or superseded_by <> id",
    )
    op.create_check_constraint(
        "ck_documents_complete_supersession",
        "documents",
        "(superseded_by is null and superseded_on is null "
        "and supersession_source_document_id is null "
        "and supersession_source_page is null) or "
        "(superseded_by is not null and registry_id is not null "
        "and superseded_on is not null "
        "and supersession_source_document_id is not null "
        "and supersession_source_page is not null "
        "and supersession_source_page > 0)",
    )
    op.execute(
        """
        create function enforce_document_supersession_registry()
        returns trigger
        language plpgsql
        as $$
        declare
            successor_registry text;
            source_registry text;
        begin
            if tg_op = 'UPDATE'
               and old.registry_id is not null
               and new.registry_id is distinct from old.registry_id then
                raise exception
                    'documents.registry_id is immutable once set (project %, document %)',
                    new.project_id,
                    new.id
                    using errcode = '23514';
            end if;

            if new.superseded_by is null then
                return new;
            end if;

            select documents.registry_id
              into successor_registry
              from documents
             where documents.project_id = new.project_id
               and documents.id = new.superseded_by;
            if successor_registry is null then
                raise exception
                    'supersession successor must already have a registry_id (project %, predecessor %, successor %)',
                    new.project_id,
                    new.id,
                    new.superseded_by
                    using errcode = '23514';
            end if;

            select documents.registry_id
              into source_registry
              from documents
             where documents.project_id = new.project_id
               and documents.id = new.supersession_source_document_id;
            if source_registry is null then
                raise exception
                    'supersession source must already have a registry_id (project %, predecessor %, source %)',
                    new.project_id,
                    new.id,
                    new.supersession_source_document_id
                    using errcode = '23514';
            end if;

            return new;
        end;
        $$;
        """
    )
    op.execute(
        """
        create constraint trigger ck_documents_registered_supersession_participants
        after insert or update of
            registry_id,
            superseded_by,
            superseded_on,
            supersession_source_document_id,
            supersession_source_page
        on documents
        deferrable initially immediate
        for each row
        execute function enforce_document_supersession_registry();
        """
    )


def downgrade() -> None:
    # A downgrade removes the registry feature. Clear each edge as one
    # complete tuple so a later re-upgrade never finds ``superseded_by``
    # without the provenance columns being dropped here.
    op.execute(
        sa.text(
            "update documents set superseded_by = null, superseded_on = null, "
            "supersession_source_document_id = null, "
            "supersession_source_page = null where superseded_by is not null"
        )
    )
    op.drop_constraint("ck_documents_complete_supersession", "documents", type_="check")
    op.drop_constraint("ck_documents_no_self_supersession", "documents", type_="check")
    op.execute(
        """
        drop trigger if exists ck_documents_registered_supersession_participants
        on documents
        """
    )
    op.execute("drop function if exists enforce_document_supersession_registry()")
    op.drop_constraint(
        "fk_documents_supersession_source_page", "documents", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_documents_supersession_source_same_project",
        "documents",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_documents_superseded_by_same_project",
        "documents",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "documents_superseded_by_fkey",
        "documents",
        "documents",
        ["superseded_by"],
        ["id"],
    )
    op.drop_constraint("uq_documents_project_id_id", "documents", type_="unique")
    op.drop_constraint("uq_documents_project_registry_id", "documents", type_="unique")
    op.drop_column("documents", "supersession_source_page")
    op.drop_column("documents", "supersession_source_document_id")
    op.drop_column("documents", "superseded_on")
    op.drop_column("documents", "registry_id")
