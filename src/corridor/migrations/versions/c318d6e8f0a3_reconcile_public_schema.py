"""Reconcile historical and fresh public-schema shapes.

Revision ID: c318d6e8f0a3
Revises: b317c5d7e9f2

Several older migrations were repaired after a shared database had already
applied them.  Replaying the current migration sources therefore produced a
logically different schema from upgrading that historical database.  This
successor makes both paths converge without rewriting authoritative rows.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "c318d6e8f0a3"
down_revision: Union[str, Sequence[str], None] = "b317c5d7e9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RETIREMENT_AWARE_VERBAL_EVENT_GUARD = """
create or replace function reject_verbal_dependency_event_mutation()
returns trigger
language plpgsql
as $$
        begin
            if current_user = 'corridor_statement_retirement' then
                if tg_op = 'TRUNCATE' then
                    return null;
                end if;
                return case when tg_op = 'DELETE' then old else new end;
            end if;
            if tg_op = 'TRUNCATE' then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            if tg_op = 'DELETE' and old.source_kind = 'verbal' then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            if tg_op = 'UPDATE' and (
                old.source_kind = 'verbal' or new.source_kind = 'verbal'
            ) then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;
"""


def upgrade() -> None:
    # The deployed predecessor uses an IDENTITY column while a fresh replay
    # uses BIGSERIAL.  Preserve the identity sequence's logical next value
    # before DROP IDENTITY removes its internally owned sequence.
    op.execute(
        """
        do $$
        declare
            identity_column boolean;
            source_sequence regclass;
            source_last_value bigint;
            source_is_called boolean;
        begin
            select is_identity = 'YES'
            into identity_column
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'dependency_evidence_sufficiencies'
              and column_name = 'id';

            if identity_column then
                source_sequence := pg_get_serial_sequence(
                    'public.dependency_evidence_sufficiencies', 'id'
                )::regclass;
                execute format(
                    'select last_value, is_called from %s', source_sequence
                ) into source_last_value, source_is_called;

                alter table dependency_evidence_sufficiencies
                    alter column id drop identity;
                create sequence if not exists
                    dependency_evidence_sufficiencies_id_seq as bigint;
                perform setval(
                    'dependency_evidence_sufficiencies_id_seq',
                    source_last_value,
                    source_is_called
                );
                alter sequence dependency_evidence_sufficiencies_id_seq
                    owned by dependency_evidence_sufficiencies.id;
                alter table dependency_evidence_sufficiencies
                    alter column id set default nextval(
                        'dependency_evidence_sufficiencies_id_seq'::regclass
                    );
            end if;
        end;
        $$;
        """
    )

    op.execute(
        """
        alter table external_report_releases
            alter column evaluation_context_json drop not null;
        alter table external_report_releases
            drop constraint if exists ck_external_report_releases_evaluation_object;
        alter table external_report_releases
            add constraint ck_external_report_releases_evaluation_object
            check (
                evaluation_context_json is null
                or jsonb_typeof(evaluation_context_json) = 'object'
            );

        alter table dependency_events
            drop constraint if exists event_source_kind;

        drop trigger if exists verbal_dependency_event_scopes_are_immutable
            on dependency_event_scopes;
        drop trigger if exists verbal_dependency_event_timings_are_immutable
            on dependency_event_timings;
        drop function if exists reject_verbal_statement_child_mutation();
        """
    )
    op.execute(_RETIREMENT_AWARE_VERBAL_EVENT_GUARD)


def downgrade() -> None:
    # The canonical freshly replayed b317 shape already has the nullable
    # Evaluation receipt, retirement-aware guard, and no legacy child guard.
    # Only its redundant SQLAlchemy-generated source-kind check needs to be
    # restored.
    op.execute(
        """
        alter table dependency_events
            add constraint event_source_kind
            check (
                source_kind::text = any (
                    array[
                        'cited'::character varying::text,
                        'verbal'::character varying::text
                    ]
                )
            );
        """
    )
