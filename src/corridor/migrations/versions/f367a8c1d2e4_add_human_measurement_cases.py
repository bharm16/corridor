"""Retain human rulings as append-only Extraction Measurement cases.

Previously, corrections and exclusions survived only in their Project Record
history, so a later extractor evaluation could not replay the exact source
location that taught the system it was wrong. This successor adds a separate
Operations record: exact source identity plus an immutable lineage of human
ruling states, without changing the Project Record.

Revision ID: f367a8c1d2e4
Revises: e319f8a0b2c5
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f367a8c1d2e4"
down_revision: Union[str, Sequence[str], None] = "e319f8a0b2c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "extraction_measurement_case_states",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("case_key", sa.String(length=160), nullable=False),
        sa.Column("predecessor_state_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("ruling_type", sa.String(length=64), nullable=False),
        sa.Column("ruling_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_identity_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "expected_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind in ('candidate_correction', 'source_discrepancy_settlement', "
            "'do_not_add', 'statement_fact_correction', "
            "'statement_scope_correction')",
            name="ck_extraction_measurement_case_states_kind",
        ),
        sa.CheckConstraint(
            "state in ('active', 'reversed')",
            name="ck_extraction_measurement_case_states_state",
        ),
        sa.CheckConstraint(
            "length(trim(case_key)) > 0 and length(trim(recorded_by)) > 0 "
            "and ruling_id > 0",
            name="ck_extraction_measurement_case_states_identity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_identity_json) = 'object' and "
            "source_identity_json ?& array["
            "'candidate_id', 'extraction_run_id', 'documents'] and "
            "jsonb_typeof(source_identity_json -> 'documents') = 'array' and "
            "jsonb_array_length(source_identity_json -> 'documents') > 0",
            name="ck_extraction_measurement_case_states_source",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_json) = 'object' and "
            "jsonb_typeof(expected_json -> 'scoring_rule') = 'string' and "
            "length(trim(expected_json ->> 'scoring_rule')) > 0",
            name="ck_extraction_measurement_case_states_expected",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["predecessor_state_id"], ["extraction_measurement_case_states.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("predecessor_state_id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "ruling_type",
            "ruling_id",
            name="uq_extraction_measurement_case_states_ruling",
        ),
    )
    op.create_index(
        "ix_extraction_measurement_case_states_project_id",
        "extraction_measurement_case_states",
        ["project_id"],
    )
    op.create_index(
        "ix_extraction_measurement_case_states_case_key",
        "extraction_measurement_case_states",
        ["case_key"],
    )
    op.create_index(
        "uq_extraction_measurement_case_states_root",
        "extraction_measurement_case_states",
        ["case_key"],
        unique=True,
        postgresql_where=sa.text("predecessor_state_id is null"),
    )
    op.execute(
        """
        create function validate_extraction_measurement_case_state()
        returns trigger language plpgsql as $$
        declare predecessor extraction_measurement_case_states%rowtype;
        begin
            if new.predecessor_state_id is null then
                if new.state <> 'active' then
                    raise exception 'an Extraction Measurement case must begin active';
                end if;
                return new;
            end if;
            select * into predecessor
            from extraction_measurement_case_states
            where id = new.predecessor_state_id;
            if not found
               or predecessor.project_id <> new.project_id
               or predecessor.case_key <> new.case_key
               or predecessor.kind <> new.kind then
                raise exception 'Extraction Measurement case predecessor mismatch';
            end if;
            return new;
        end;
        $$;
        create trigger extraction_measurement_case_state_lineage
        before insert on extraction_measurement_case_states
        for each row execute function validate_extraction_measurement_case_state();

        create function reject_extraction_measurement_case_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'Extraction Measurement case states are append-only';
        end;
        $$;
        create trigger extraction_measurement_case_states_are_immutable
        before update or delete on extraction_measurement_case_states
        for each row execute function reject_extraction_measurement_case_mutation();
        create trigger extraction_measurement_case_states_reject_truncate
        before truncate on extraction_measurement_case_states
        for each statement execute function reject_extraction_measurement_case_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from extraction_measurement_case_states) then
                raise exception 'cannot erase retained human measurement cases';
            end if;
        end
        $$;
        drop trigger if exists extraction_measurement_case_states_are_immutable
            on extraction_measurement_case_states;
        drop trigger if exists extraction_measurement_case_states_reject_truncate
            on extraction_measurement_case_states;
        drop trigger if exists extraction_measurement_case_state_lineage
            on extraction_measurement_case_states;
        drop function if exists reject_extraction_measurement_case_mutation();
        drop function if exists validate_extraction_measurement_case_state();
        """
    )
    op.drop_index(
        "uq_extraction_measurement_case_states_root",
        table_name="extraction_measurement_case_states",
    )
    op.drop_index(
        "ix_extraction_measurement_case_states_case_key",
        table_name="extraction_measurement_case_states",
    )
    op.drop_index(
        "ix_extraction_measurement_case_states_project_id",
        table_name="extraction_measurement_case_states",
    )
    op.drop_table("extraction_measurement_case_states")
