"""record verbal statements at stated precision and scope

Revision ID: f3a5c7d9e1b2
Revises: d359a1b2c3e4

The first verbal contract forced every recorded call into one exact-day
commitment scoped to exactly one Constraint.  That collapse made ``01/2025``
look like January 1, could not preserve an approximate promise, refused a
party-level or several-Constraint call, and could not record a stated change
of promised timing.  ADR-0036 already accepts the richer shape for cited
statements; this migration extends the two remaining verbal-only database
guards so a verbal obeys the same precision and scope rules as any other
attributable statement, while still requiring its conversation date.

Only the verbal-specific branches relax.  The generic guards that reject
impossible timing (``ck_dependency_event_timing_bounds`` and
``verify_dependency_event_timing_cardinality``), malformed scope, cross-project
links, unresolved attribution, and any historical rewrite stay exactly as they
were.  This replaces two trigger function bodies and touches no rows, so every
existing verbal and documentary statement keeps its stored precision, actor,
and source.  ``downgrade`` restores both function bodies verbatim as head
defined them, so a downgrade/re-upgrade rehearsal reconciles byte for byte.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f3a5c7d9e1b2"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A verbal keeps exactly one verbal-specific invariant: the recorder must know
# when the conversation happened.  Precision, event type, and previous timing
# now follow the same generic guards as a cited statement.
_RELAXED_VERBAL_SHAPE = """
CREATE OR REPLACE FUNCTION public.validate_verbal_statement_shape()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        declare
            target_event_id bigint;
            target_source text;
            target_event_date date;
        begin
            if tg_table_name = 'dependency_events' then
                target_event_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_event_id := case when tg_op = 'DELETE'
                    then old.event_id else new.event_id end;
            end if;
            select source_kind, event_date
              into target_source, target_event_date
            from dependency_events where id = target_event_id;
            if target_source is distinct from 'verbal' then
                return null;
            end if;
            if target_event_date is null then
                raise exception 'Verbal statements require the conversation date'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $function$
"""


# The strict verbal shape, restored on downgrade exactly as head defines it.
_STRICT_VERBAL_SHAPE = """
CREATE OR REPLACE FUNCTION public.validate_verbal_statement_shape()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        declare
            target_event_id bigint;
            target_source text;
            target_type text;
            target_event_date date;
            new_precision text;
            previous_count integer;
        begin
            if tg_table_name = 'dependency_events' then
                target_event_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_event_id := case when tg_op = 'DELETE'
                    then old.event_id else new.event_id end;
            end if;
            select source_kind, event_type, event_date
              into target_source, target_type, target_event_date
            from dependency_events where id = target_event_id;
            if target_source is distinct from 'verbal' then
                return null;
            end if;
            select precision into new_precision
            from dependency_event_timings
            where event_id = target_event_id and kind = 'new';
            select count(*) into previous_count
            from dependency_event_timings
            where event_id = target_event_id and kind = 'previous';
            if target_event_date is null
               or target_type <> 'commitment'
               or new_precision is distinct from 'day'
               or previous_count <> 0 then
                raise exception 'Verbal statements require one exact-day commitment timing'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $function$
"""


# The scope-decision guard keeps every generic rule; only the verbal-only branch
# requiring exactly one selected Dependency is removed.
_RELAXED_SCOPE_DECISION = """
CREATE OR REPLACE FUNCTION public.validate_dependency_event_scope_decision()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
        $function$
"""


# The strict scope guard, restored on downgrade exactly as head defines it.
_STRICT_SCOPE_DECISION = """
CREATE OR REPLACE FUNCTION public.validate_dependency_event_scope_decision()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
        $function$
"""


def upgrade() -> None:
    op.execute(_RELAXED_VERBAL_SHAPE)
    op.execute(_RELAXED_SCOPE_DECISION)


def downgrade() -> None:
    op.execute(_STRICT_SCOPE_DECISION)
    op.execute(_STRICT_VERBAL_SHAPE)
