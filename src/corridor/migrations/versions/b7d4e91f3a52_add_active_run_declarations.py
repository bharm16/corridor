"""add attributable, append-preserving active run declarations

Revision ID: b7d4e91f3a52
Revises: a1c8f3e27b64
Create Date: 2026-08-07 02:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d4e91f3a52"
down_revision: Union[str, Sequence[str], None] = "a1c8f3e27b64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Declaring an Active Run becomes a signed, appended act.

    ``active_extraction_runs`` stays the one-row projection every reader
    joins; this table records who declared each run and in what order, as
    one linear chain per document. The live table is empty at this
    migration, so there is no history to backfill and no row to attribute
    to anyone who did not sign it.
    """
    op.create_table(
        "active_run_declarations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_id", sa.BigInteger(), sa.ForeignKey("documents.id"),
            nullable=False,
        ),
        sa.Column("extraction_run_id", sa.BigInteger(), nullable=False),
        sa.Column("declared_by", sa.String(length=128), nullable=False),
        sa.Column(
            "declared_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("predecessor_declaration_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
        ),
        sa.UniqueConstraint(
            "document_id", "id", name="uq_active_run_declarations_document_id_id"
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "predecessor_declaration_id"],
            ["active_run_declarations.document_id", "active_run_declarations.id"],
            name="fk_active_run_declarations_predecessor",
        ),
        sa.UniqueConstraint(
            "predecessor_declaration_id",
            name="uq_active_run_declarations_predecessor",
        ),
    )
    op.create_index(
        "uq_active_run_declarations_one_root",
        "active_run_declarations",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("predecessor_declaration_id is null"),
    )
    op.execute(
        """
        create function enforce_active_run_declaration()
        returns trigger
        language plpgsql
        as $$
        declare
            demo_document boolean;
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            -- The one sanctioned escape, identical to Candidate lineage:
            -- the isolated demo project resets itself, and only itself.
            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from documents
                      join projects on projects.id = documents.project_id
                     where documents.id = old.document_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_document;
                if demo_document then
                    return old;
                end if;
            end if;
            raise exception 'Active Run declarations are immutable'
                using errcode = '23514';
        end;
        $$;

        create trigger active_run_declarations_are_immutable
        before update or delete
        on active_run_declarations
        for each row execute function enforce_active_run_declaration();

        create trigger active_run_declarations_reject_truncate
        before truncate on active_run_declarations
        for each statement execute function enforce_active_run_declaration();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        drop trigger active_run_declarations_reject_truncate
            on active_run_declarations;
        drop trigger active_run_declarations_are_immutable
            on active_run_declarations;
        drop function enforce_active_run_declaration();
        """
    )
    op.drop_index(
        "uq_active_run_declarations_one_root", table_name="active_run_declarations"
    )
    op.drop_table("active_run_declarations")
