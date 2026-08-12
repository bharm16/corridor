"""complete Coordination Plan reason projections

Revision ID: c249d7e1f4a3
Revises: b249c7e1d4f3

ADR-0038 requires an Action Due Date or an attributable structured reason for
every current Coordination Plan.  The initial subject expansion stored that
projection for Commitment Lineages but omitted Dependencies, and its deferred
Milestone Impact validator did not prove that ``not_yet_known`` still had an
open plan.  This successor closes both gaps without rewriting a stamped
revision or manufacturing reasons for legacy receipts.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c249d7e1f4a3"
down_revision: Union[str, Sequence[str], None] = "b249c7e1d4f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dependencies",
        sa.Column("action_due_date_reason", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        create or replace function validate_work_decision_milestone_impact()
        returns trigger
        language plpgsql
        as $$
        declare
            target_decision_id bigint;
            target_field text;
            target_lineage_id bigint;
            target_project_id bigint;
            impact_value text;
            impact_json jsonb;
            impact_state text;
            linked_ids bigint[];
            receipt_ids bigint[];
            invalid_project_link boolean;
            current_owner_value text;
            current_action_value text;
            current_action_reason text;
            current_action_json jsonb;
        begin
            if tg_table_name = 'work_decisions' then
                target_decision_id := case when tg_op = 'DELETE'
                    then (to_jsonb(old) ->> 'id')::bigint
                    else (to_jsonb(new) ->> 'id')::bigint
                end;
            else
                target_decision_id := case when tg_op = 'DELETE'
                    then (to_jsonb(old) ->> 'work_decision_id')::bigint
                    else (to_jsonb(new) ->> 'work_decision_id')::bigint
                end;
            end if;
            select field, commitment_lineage_id, after_value
              into target_field, target_lineage_id, impact_value
            from work_decisions where id = target_decision_id;
            if target_field is null then
                return null;
            end if;
            select project_id into target_project_id
            from commitment_lineages where id = target_lineage_id;
            select coalesce(array_agg(milestone_id order by milestone_id), '{}')
              into linked_ids
            from work_decision_milestone_impacts
            where work_decision_id = target_decision_id;
            if target_field <> 'milestone_impact' then
                if cardinality(linked_ids) <> 0 then
                    raise exception 'only Milestone Impact may link Milestones'
                        using errcode = '23514';
                end if;
                return null;
            end if;
            if impact_value is null then
                raise exception 'Milestone Impact needs an explicit state'
                    using errcode = '23514';
            end if;
            begin
                impact_json := impact_value::jsonb;
            exception when others then
                raise exception 'Milestone Impact receipt must be structured'
                    using errcode = '23514';
            end;
            if jsonb_typeof(impact_json) <> 'object'
               or jsonb_typeof(impact_json -> 'milestone_ids') <> 'array' then
                raise exception 'Milestone Impact receipt must name a state and Milestones'
                    using errcode = '23514';
            end if;
            impact_state := impact_json ->> 'state';
            if impact_state is null
               or impact_state not in ('affects', 'does_not_affect', 'not_yet_known') then
                raise exception 'Milestone Impact has an unknown state'
                    using errcode = '23514';
            end if;
            begin
                select coalesce(array_agg(value::bigint order by value::bigint), '{}')
                  into receipt_ids
                from jsonb_array_elements_text(impact_json -> 'milestone_ids');
            exception when others then
                raise exception 'Milestone Impact receipt has invalid Milestone identity'
                    using errcode = '23514';
            end;
            if cardinality(receipt_ids) <> (
                select count(distinct value::bigint)
                from jsonb_array_elements_text(impact_json -> 'milestone_ids')
            ) then
                raise exception 'Milestone Impact receipt repeats a Milestone'
                    using errcode = '23514';
            end if;
            if impact_state = 'affects' and cardinality(receipt_ids) = 0 then
                raise exception 'an affecting Milestone Impact needs Milestones'
                    using errcode = '23514';
            end if;
            if impact_state <> 'affects' and cardinality(receipt_ids) <> 0 then
                raise exception 'only an affecting Milestone Impact names Milestones'
                    using errcode = '23514';
            end if;
            if receipt_ids is distinct from linked_ids then
                raise exception 'Milestone Impact receipt and exact links disagree'
                    using errcode = '23514';
            end if;
            select exists (
                select 1
                from milestones milestone
                where milestone.id = any(linked_ids)
                  and milestone.project_id is distinct from target_project_id
            ) into invalid_project_link;
            if invalid_project_link then
                raise exception 'Milestone Impact cannot cross projects'
                    using errcode = '23514';
            end if;
            if impact_state = 'not_yet_known' then
                select decision.after_value into current_owner_value
                from work_decisions decision
                where decision.commitment_lineage_id = target_lineage_id
                  and decision.field = 'internal_owner'
                  and not exists (
                      select 1 from work_decisions successor
                      where successor.predecessor_decision_id = decision.id
                  );
                select decision.after_value, decision.action_due_date_reason
                  into current_action_value, current_action_reason
                from work_decisions decision
                where decision.commitment_lineage_id = target_lineage_id
                  and decision.field = 'next_action'
                  and not exists (
                      select 1 from work_decisions successor
                      where successor.predecessor_decision_id = decision.id
                  );
                if current_owner_value is null or current_action_value is null then
                    raise exception 'unknown Milestone Impact needs a current Internal Owner and Next Action'
                        using errcode = '23514';
                end if;
                begin
                    current_action_json := current_action_value::jsonb;
                exception when others then
                    raise exception 'current Next Action receipt must be structured'
                        using errcode = '23514';
                end;
                if (current_action_json ->> 'due_date') is null
                   and current_action_reason not in (
                       'awaiting_external_information',
                       'awaiting_schedule_information',
                       'date_not_yet_known'
                   ) then
                    raise exception 'unknown Milestone Impact needs an Action Due Date or structured unknown-date reason'
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
        "cannot downgrade statement contract: Coordination Plan reason "
        "projections cannot be round-tripped safely"
    )
