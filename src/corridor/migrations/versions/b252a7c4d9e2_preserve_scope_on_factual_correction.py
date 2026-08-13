"""preserve scope when a statement fact is corrected

Revision ID: b252a7c4d9e2
Revises: a252a7c4d9e2

An all-active decision is a human-recorded snapshot, not a standing rule.  A
later factual correction must retain that scope without pretending the
coordinator selected a new set or silently taking a fresh all-active snapshot.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b252a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "a252a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_dependency_events_scope_mode", "dependency_events", type_="check"
    )
    op.create_check_constraint(
        "ck_dependency_events_scope_mode",
        "dependency_events",
        "scope_mode in ('unknown', 'selected', 'all_active', 'carried_forward')",
    )
    op.drop_constraint(
        "ck_dependency_event_scope_decisions_mode",
        "dependency_event_scope_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_dependency_event_scope_decisions_mode",
        "dependency_event_scope_decisions",
        "scope_mode in ('unknown', 'selected', 'all_active', 'carried_forward')",
    )
    op.execute(
        """
        create or replace function validate_dependency_event_scope_decision()
        returns trigger
        language plpgsql
        as $$
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
            if target_event_id is null then
                return null;
            end if;
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
            select count(*) into link_count
            from dependency_event_scopes
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
                  and dependency.dismissed_at is null
                  and dependency.status <> 'closed';
                select count(*) into selected_count
                from dependency_event_scopes scope
                join dependencies dependency on dependency.id = scope.dependency_id
                join dependency_events event on event.id = target_event_id
                where scope.scope_decision_id = target_decision_id
                  and dependency.project_id = event.project_id
                  and dependency.external_org_id = event.affected_external_org_id
                  and dependency.dismissed_at is null
                  and dependency.status <> 'closed';
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


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade statement contract: carried-forward scope preserves "
        "the exact earlier human decision"
    )
