"""Backfill legacy statement scope and scalar-date facts conservatively.

Revision ID: e226a8d4f3c2
Revises: d225a7c4e3f2

The earlier statement expansion preserved legacy scalar values but could not
prove their original wording, speaker, timing precision, or prior timing.
This migration records only what the old rows establish: existing events gain
their initial selected scope decision, and a scalar-only Committed Date gains
an unresolved, legacy-unknown Commitment.  No prose is parsed and no affected
party is copied into the stated-party role.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e226a8d4f3c2"
down_revision: Union[str, Sequence[str], None] = "d225a7c4e3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MIGRATION_ACTOR = "corridor:statement-migration-v1"
_SCALAR_DESCRIPTION_PREFIX = "Legacy scalar-only Committed Date migrated for Dependency "


def upgrade() -> None:
    # #224 deliberately left pre-existing rows untouched.  Their initial
    # decision is a new migration act, not an invented claim about who made
    # the historic placement decision.
    op.execute(
        f"""
        alter table dependency_event_scopes
            disable trigger dependency_event_scope_links_are_immutable;
        alter table dependency_event_scopes
            disable trigger dependency_event_scope_decision_link_is_valid;
        insert into dependency_event_scope_decisions
            (event_id, scope_mode, decided_by)
        select event.id, event.scope_mode, '{_MIGRATION_ACTOR}'
        from dependency_events event
        where not exists (
            select 1 from dependency_event_scope_decisions decision
            where decision.event_id = event.id
        );

        update dependency_event_scopes scope
        set scope_decision_id = decision.id,
            recorded_by = '{_MIGRATION_ACTOR}'
        from dependency_event_scope_decisions decision
        where scope.event_id = decision.event_id
          and scope.scope_decision_id is null
          and decision.supersedes_scope_decision_id is null;
        set constraints all immediate;
        alter table dependency_event_scopes
            enable trigger dependency_event_scope_links_are_immutable;
        alter table dependency_event_scopes
            enable trigger dependency_event_scope_decision_link_is_valid;
        set constraints all deferred;
        """
    )
    op.alter_column(
        "dependency_event_scopes", "scope_decision_id", nullable=False
    )
    op.alter_column("dependency_event_scopes", "recorded_by", nullable=False)

    # A scalar date that had no event must not vanish from statement history.
    # It cannot prove a speaker or exact source precision, so the generated
    # statement is unresolved and legacy-unknown even though the compatibility
    # scalar remains untouched on dependencies.
    op.execute(
        f"""
        create temporary table legacy_scalar_statement_backfill (
            dependency_id bigint primary key,
            project_id bigint not null,
            affected_external_org_id bigint,
            committed_date date not null,
            event_id bigint
        ) on commit drop;

        insert into legacy_scalar_statement_backfill
            (dependency_id, project_id, affected_external_org_id, committed_date)
        select dependency.id, dependency.project_id, dependency.external_org_id,
               dependency.committed_date
        from dependencies dependency
        where dependency.committed_date is not null
          and not exists (
              select 1
              from dependency_event_scopes scope
              where scope.dependency_id = dependency.id
          );

        insert into dependency_events
            (project_id, affected_external_org_id, stated_external_org_id,
             attribution_state, scope_mode, timing_direction, event_type,
             source_kind, stated_party, event_date, description, created_by)
        select project_id, affected_external_org_id, null, 'unresolved',
               'selected', null, 'commitment', 'cited', null, null,
               '{_SCALAR_DESCRIPTION_PREFIX}' || dependency_id::text,
               '{_MIGRATION_ACTOR}'
        from legacy_scalar_statement_backfill;

        update legacy_scalar_statement_backfill backfill
        set event_id = event.id
        from dependency_events event
        where event.project_id = backfill.project_id
          and event.created_by = '{_MIGRATION_ACTOR}'
          and event.description =
              '{_SCALAR_DESCRIPTION_PREFIX}' || backfill.dependency_id::text;

        insert into dependency_event_timings
            (event_id, kind, text, precision, start_date, end_date)
        select event_id, 'new', committed_date::text, 'legacy_unknown', null, null
        from legacy_scalar_statement_backfill;

        insert into dependency_event_scopes
            (event_id, scope_decision_id, dependency_id, recorded_by)
        select backfill.event_id, decision.id, backfill.dependency_id,
               '{_MIGRATION_ACTOR}'
        from legacy_scalar_statement_backfill backfill
        join dependency_event_scope_decisions decision
          on decision.event_id = backfill.event_id
         and decision.supersedes_scope_decision_id is null;
        """
    )


def downgrade() -> None:
    # The scalar compatibility field is still present, so discard only the
    # generated representation after reopening the temporary migration-only
    # children.  A real scope correction has its own #224 downgrade refusal.
    op.alter_column("dependency_event_scopes", "recorded_by", nullable=True)
    op.alter_column("dependency_event_scopes", "scope_decision_id", nullable=True)
    op.execute(
        f"""
        alter table dependency_event_scopes disable trigger dependency_event_scope_links_are_immutable;
        alter table dependency_event_scopes disable trigger dependency_event_scope_decision_link_is_valid;
        alter table dependency_event_scope_decisions disable trigger dependency_event_scope_decisions_are_immutable;
        alter table dependency_events disable trigger external_party_statement_events_are_immutable;
        alter table dependency_event_timings disable trigger external_party_statement_timings_are_immutable;

        delete from dependency_event_timings timing
        using dependency_events event
        where timing.event_id = event.id
          and event.created_by = '{_MIGRATION_ACTOR}'
          and event.description like '{_SCALAR_DESCRIPTION_PREFIX}%';
        delete from dependency_event_scopes scope
        using dependency_events event
        where scope.event_id = event.id
          and event.created_by = '{_MIGRATION_ACTOR}'
          and event.description like '{_SCALAR_DESCRIPTION_PREFIX}%';
        delete from dependency_event_scope_decisions decision
        using dependency_events event
        where decision.event_id = event.id
          and event.created_by = '{_MIGRATION_ACTOR}'
          and event.description like '{_SCALAR_DESCRIPTION_PREFIX}%';
        delete from dependency_events event
        where event.created_by = '{_MIGRATION_ACTOR}'
          and event.description like '{_SCALAR_DESCRIPTION_PREFIX}%';

        update dependency_event_scopes scope
        set scope_decision_id = null,
            recorded_by = null
        from dependency_event_scope_decisions decision
        where scope.scope_decision_id = decision.id
          and decision.decided_by = '{_MIGRATION_ACTOR}'
          and decision.supersedes_scope_decision_id is null;
        delete from dependency_event_scope_decisions
        where decided_by = '{_MIGRATION_ACTOR}'
          and supersedes_scope_decision_id is null;

        set constraints all immediate;
        alter table dependency_event_scopes enable trigger dependency_event_scope_links_are_immutable;
        alter table dependency_event_scopes enable trigger dependency_event_scope_decision_link_is_valid;
        alter table dependency_event_scope_decisions enable trigger dependency_event_scope_decisions_are_immutable;
        alter table dependency_events enable trigger external_party_statement_events_are_immutable;
        alter table dependency_event_timings enable trigger external_party_statement_timings_are_immutable;
        """
    )
