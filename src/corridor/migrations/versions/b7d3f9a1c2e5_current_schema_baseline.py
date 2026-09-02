"""Build the current Corridor schema from the released-head baseline.

Revision ID: b7d3f9a1c2e5
Revises: None
"""

from pathlib import Path

from alembic import op


revision = "b7d3f9a1c2e5"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the complete released schema on a blank database."""

    connection = op.get_bind()
    connection.exec_driver_sql(
        """
        do $$
        begin
            if not exists (
                select 1 from pg_roles
                where rolname = 'corridor_statement_retirement'
            ) then
                create role corridor_statement_retirement nologin noinherit;
            end if;
        end
        $$
        """
    )
    sql = Path(__file__).with_name("b7d3f9a1c2e5_current_schema.sql").read_text(
        encoding="utf-8"
    )
    connection.exec_driver_sql(sql.replace("%", "%%"))
    connection.exec_driver_sql(
        "alter function public.purge_external_party_statement_rows(bigint, text) "
        "owner to corridor_statement_retirement"
    )
    connection.exec_driver_sql("set search_path = public")
    connection.exec_driver_sql("set check_function_bodies = true")


def downgrade() -> None:
    """The consolidated baseline has no supported predecessor."""

    raise RuntimeError("baseline downgrade is unsupported")
