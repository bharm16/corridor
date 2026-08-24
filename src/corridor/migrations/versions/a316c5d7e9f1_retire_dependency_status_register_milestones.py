"""Retire Dependency status and register exact Milestone inputs.

Revision ID: a316c5d7e9f1
Revises: f315b4c6d8e0

The old status values remain in a retirement table and lose all authority.
Existing Milestones receive honest legacy registrations with preserved rows and
no invented file digest; new imports record the exact source SHA-256.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a316c5d7e9f1"
down_revision: Union[str, Sequence[str], None] = "f315b4c6d8e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "retired_dependency_statuses",
        sa.Column(
            "dependency_id",
            sa.BigInteger(),
            sa.ForeignKey("dependencies.id"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        "insert into retired_dependency_statuses (dependency_id, status) "
        "select id, status::text from dependencies"
    )

    op.create_table(
        "milestone_registrations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "milestone_id",
            sa.BigInteger(),
            sa.ForeignKey("milestones.id"),
            nullable=False,
        ),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64)),
        sa.Column("source_row_json", postgresql.JSONB(), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "predecessor_registration_id",
            sa.BigInteger(),
            sa.ForeignKey("milestone_registrations.id"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "predecessor_registration_id",
            name="uq_milestone_registrations_predecessor",
        ),
        sa.CheckConstraint(
            "source_sha256 is null or source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_milestone_registrations_source_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_row_json) = 'object'",
            name="ck_milestone_registrations_source_row",
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_milestone_registrations_recorded_by",
        ),
    )
    op.create_index(
        "ix_milestone_registrations_milestone_id",
        "milestone_registrations",
        ["milestone_id"],
    )
    op.execute(
        """
        create unique index uq_milestone_registrations_one_root
        on milestone_registrations (milestone_id)
        where predecessor_registration_id is null;

        create function enforce_milestone_registration()
        returns trigger language plpgsql as $$
        begin
            if tg_op = 'INSERT' then return new; end if;
            raise exception 'Milestone Registrations are immutable'
                using errcode = '23514';
        end
        $$;

        create trigger milestone_registrations_are_immutable
        before update or delete on milestone_registrations
        for each row execute function enforce_milestone_registration();

        create trigger milestone_registrations_reject_truncate
        before truncate on milestone_registrations
        for each statement execute function enforce_milestone_registration();

        insert into milestone_registrations
            (milestone_id, source_name, source_sha256, source_row_json, recorded_by)
        select id,
               coalesce(source, 'legacy source unavailable'),
               null,
               jsonb_build_object(
                   'code', code,
                   'name', name,
                   'need_date', case when need_date is null then null
                                     else need_date::text end
               ),
               'corridor:legacy-milestone-migration'
          from milestones
         order by id;
        """
    )

    op.add_column("milestones", sa.Column("current_registration_id", sa.BigInteger()))
    op.execute(
        """
        update milestones milestone
           set current_registration_id = registration.id
          from milestone_registrations registration
         where registration.milestone_id = milestone.id
        """
    )
    op.create_foreign_key(
        "fk_milestones_current_registration",
        "milestones",
        "milestone_registrations",
        ["current_registration_id"],
        ["id"],
    )

    op.add_column(
        "dependencies", sa.Column("milestone_registration_id", sa.BigInteger())
    )
    op.execute(
        """
        update dependencies dependency
           set milestone_registration_id = milestone.current_registration_id
          from milestones milestone
         where dependency.milestone_id = milestone.id
        """
    )
    op.create_foreign_key(
        "fk_dependencies_milestone_registration",
        "dependencies",
        "milestone_registrations",
        ["milestone_registration_id"],
        ["id"],
    )

    op.execute(
        """
        create or replace function validate_dependency_event_scope_decision()
        returns trigger language plpgsql as $$
        declare
            target_decision_id bigint;
            target_event_id bigint;
            target_mode text;
            target_source text;
            link_count integer;
            active_count integer;
            selected_count integer;
            predecessor_event_id bigint;
        begin
            if tg_table_name = 'dependency_event_scope_decisions' then
                target_decision_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_decision_id := case when tg_op = 'DELETE'
                    then old.scope_decision_id else new.scope_decision_id end;
            end if;
            select decision.event_id, decision.scope_mode, event.source_kind
              into target_event_id, target_mode, target_source
              from dependency_event_scope_decisions decision
              join dependency_events event on event.id = decision.event_id
             where decision.id = target_decision_id;
            if target_event_id is null then return null; end if;
            if tg_table_name = 'dependency_event_scope_decisions' then
                if tg_op <> 'DELETE' and new.supersedes_scope_decision_id is not null then
                    select event_id into predecessor_event_id
                      from dependency_event_scope_decisions
                     where id = new.supersedes_scope_decision_id;
                    if predecessor_event_id is distinct from new.event_id then
                        raise exception 'scope correction must supersede a decision on the same statement'
                            using errcode = '23514';
                    end if;
                end if;
            end if;
            select count(*) into link_count from dependency_event_scopes
             where scope_decision_id = target_decision_id;
            if target_mode = 'unknown' and link_count <> 0 then
                raise exception 'unknown statement scope has Dependency links'
                    using errcode = '23514';
            end if;
            if target_mode in ('selected', 'all_active', 'carried_forward') and link_count = 0 then
                raise exception 'known statement scope has no Dependency links'
                    using errcode = '23514';
            end if;
            if target_source = 'verbal'
               and (target_mode <> 'selected' or link_count <> 1) then
                raise exception 'Verbal statements require one selected Dependency scope'
                    using errcode = '23514';
            end if;
            if target_mode = 'all_active' then
                select count(*) into active_count
                  from dependencies dependency
                  join dependency_events event on event.id = target_event_id
                 where dependency.project_id = event.project_id
                   and dependency.external_org_id = event.affected_external_org_id
                   and dependency.dismissed_at is null;
                select count(*) into selected_count
                  from dependency_event_scopes scope
                  join dependencies dependency on dependency.id = scope.dependency_id
                  join dependency_events event on event.id = target_event_id
                 where scope.scope_decision_id = target_decision_id
                   and dependency.project_id = event.project_id
                   and dependency.external_org_id = event.affected_external_org_id
                   and dependency.dismissed_at is null;
                if active_count <> selected_count or link_count <> active_count then
                    raise exception 'all-active scope must record the exact eligible Dependency snapshot'
                        using errcode = '23514';
                end if;
            end if;
            return null;
        end;
        $$;
        """
    )
    _remove_status_from_scope_link_guard()

    op.drop_column("dependencies", "status")


def downgrade() -> None:
    raise RuntimeError(
        "cannot restore an unauthoritative Dependency status or discard Milestone Registrations"
    )


def _remove_status_from_scope_link_guard() -> None:
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text(
            "select pg_get_functiondef("
            "'validate_dependency_event_scope_decision_link()'::regprocedure)"
        )
    )
    replacements = (
        ("            dependency_status text;\n", ""),
        (
            """            select project_id, external_org_id, dismissed_at, status
              into dependency_project, dependency_party, dependency_dismissed,
                   dependency_status
""",
            """            select project_id, external_org_id, dismissed_at
              into dependency_project, dependency_party, dependency_dismissed
""",
        ),
        (
            """            if dependency_status = 'closed' then
                raise exception 'statement scope cannot include a closed Dependency'
                    using errcode = '23514';
            end if;
""",
            "",
        ),
    )
    for old, new in replacements:
        if old not in definition:
            raise RuntimeError(
                "released statement scope guard does not match the expected predecessor"
            )
        definition = definition.replace(old, new, 1)
    connection.exec_driver_sql(definition)
