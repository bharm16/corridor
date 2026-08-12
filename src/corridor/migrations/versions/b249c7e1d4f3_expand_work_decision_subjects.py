"""Expand Work Decisions to one discriminated Coordination Subject.

Revision ID: b249c7e1d4f3
Revises: b230e4f5a6b7

The existing Dependency receipt chain remains byte-for-byte intact.  This
additive revision introduces a durable Commitment Lineage subject beside it,
then makes the database reject neither/both subjects, unaccepted statement
subjects, cross-subject predecessors, and Milestone Impact without exact
registered Milestones.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b249c7e1d4f3"
down_revision: Union[str, Sequence[str], None] = "b230e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "commitment_lineages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("internal_owner", sa.Text(), nullable=True),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("action_due_date", sa.Date(), nullable=True),
        sa.Column("action_due_date_reason", sa.String(length=64), nullable=True),
        sa.Column("milestone_impact", sa.String(length=32), nullable=True),
        sa.Column(
            "milestone_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "plan_needs_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "dependency_events",
        sa.Column("commitment_lineage_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "dependency_events",
        sa.Column("supersedes_event_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_dependency_events_commitment_lineage",
        "dependency_events",
        "commitment_lineages",
        ["commitment_lineage_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_dependency_events_supersedes_event",
        "dependency_events",
        "dependency_events",
        ["supersedes_event_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_dependency_events_supersedes_event",
        "dependency_events",
        ["supersedes_event_id"],
    )

    # Historical statement rows are append-only.  A migration alone may
    # attach the additive lineage key, and each accepted historical fact gets
    # its own root because no pre-#249 relation can honestly infer a factual
    # correction chain.
    op.execute(
        "alter table dependency_events disable trigger "
        "external_party_statement_events_are_immutable"
    )
    op.execute(
        """
        do $$
        declare
            statement_row record;
            lineage_id bigint;
        begin
            for statement_row in
                select id, project_id
                from dependency_events
                where event_type in ('commitment', 'committed_date_change')
                  and attribution_state = 'resolved'
                  and stated_external_org_id is not null
                order by id
            loop
                insert into commitment_lineages (project_id)
                values (statement_row.project_id)
                returning id into lineage_id;
                update dependency_events
                set commitment_lineage_id = lineage_id
                where id = statement_row.id;
            end loop;
        end;
        $$;
        """
    )
    op.execute(
        "alter table dependency_events enable trigger "
        "external_party_statement_events_are_immutable"
    )

    op.alter_column("work_decisions", "dependency_id", nullable=True)
    op.add_column(
        "work_decisions",
        sa.Column("commitment_lineage_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "work_decisions",
        sa.Column("action_due_date_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "work_decisions",
        sa.Column("no_follow_up_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "work_decisions",
        sa.Column("cancellation_reason", sa.String(length=64), nullable=True),
    )
    op.add_column("work_decisions", sa.Column("note", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_work_decisions_commitment_lineage",
        "work_decisions",
        "commitment_lineages",
        ["commitment_lineage_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_work_decisions_commitment_lineage_id_id",
        "work_decisions",
        ["commitment_lineage_id", "id"],
    )
    op.create_foreign_key(
        "fk_work_decisions_commitment_lineage_predecessor",
        "work_decisions",
        "work_decisions",
        ["commitment_lineage_id", "predecessor_decision_id"],
        ["commitment_lineage_id", "id"],
    )
    op.create_check_constraint(
        "ck_work_decisions_exactly_one_subject",
        "work_decisions",
        "(dependency_id is not null and commitment_lineage_id is null) "
        "or (dependency_id is null and commitment_lineage_id is not null)",
    )
    op.create_check_constraint(
        "ck_work_decisions_field",
        "work_decisions",
        "field in ('internal_owner', 'next_action', 'milestone_impact')",
    )
    op.create_index(
        "uq_work_decisions_commitment_lineage_one_root",
        "work_decisions",
        ["commitment_lineage_id", "field"],
        unique=True,
        postgresql_where=sa.text("predecessor_decision_id is null"),
    )

    op.create_table(
        "work_decision_milestone_impacts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "work_decision_id",
            sa.BigInteger(),
            sa.ForeignKey("work_decisions.id"),
            nullable=False,
        ),
        sa.Column(
            "milestone_id",
            sa.BigInteger(),
            sa.ForeignKey("milestones.id"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "work_decision_id",
            "milestone_id",
            name="uq_work_decision_milestone_impact",
        ),
    )

    op.execute(
        """
        create function validate_commitment_lineage_event()
        returns trigger
        language plpgsql
        as $$
        declare
            target_lineage_id bigint;
            target_project_id bigint;
            event_project_id bigint;
            event_type_value text;
            attribution_value text;
            stated_party_id bigint;
            predecessor_lineage_id bigint;
            predecessor_affected_party_id bigint;
            predecessor_stated_party_id bigint;
            current_count integer;
        begin
            target_lineage_id := case when tg_op = 'DELETE'
                then old.commitment_lineage_id else new.commitment_lineage_id end;
            if target_lineage_id is null then
                return null;
            end if;
            select project_id into target_project_id
            from commitment_lineages where id = target_lineage_id;
            if target_project_id is null then
                return null;
            end if;
            if tg_op <> 'DELETE' then
                select project_id, event_type, attribution_state, stated_external_org_id
                  into event_project_id, event_type_value, attribution_value, stated_party_id
                from dependency_events where id = new.id;
                if event_project_id is null
                   or event_project_id is distinct from target_project_id
                   or event_type_value not in ('commitment', 'committed_date_change')
                   or attribution_value <> 'resolved'
                   or stated_party_id is null then
                    raise exception 'Commitment Lineage needs one accepted attributable statement'
                        using errcode = '23514';
                end if;
            end if;
            if tg_op <> 'DELETE' and new.supersedes_event_id is not null then
                select
                    commitment_lineage_id,
                    affected_external_org_id,
                    stated_external_org_id
                  into
                    predecessor_lineage_id,
                    predecessor_affected_party_id,
                    predecessor_stated_party_id
                from dependency_events where id = new.supersedes_event_id;
                if predecessor_lineage_id is distinct from target_lineage_id then
                    raise exception 'statement correction must remain in one Commitment Lineage'
                        using errcode = '23514';
                end if;
                if predecessor_affected_party_id is distinct from new.affected_external_org_id
                   or predecessor_stated_party_id is distinct from new.stated_external_org_id then
                    raise exception 'statement correction must keep the same External Parties'
                        using errcode = '23514';
                end if;
            end if;
            select count(*) into current_count
            from dependency_events event
            where event.commitment_lineage_id = target_lineage_id
              and event.event_type in ('commitment', 'committed_date_change')
              and event.attribution_state = 'resolved'
              and event.stated_external_org_id is not null
              and not exists (
                  select 1 from dependency_events successor
                  where successor.supersedes_event_id = event.id
              );
            if current_count <> 1 then
                raise exception 'Commitment Lineage needs exactly one current accepted statement'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;

        create function validate_commitment_lineage()
        returns trigger
        language plpgsql
        as $$
        declare
            target_lineage_id bigint;
            current_count integer;
        begin
            if tg_table_name = 'commitment_lineages' then
                target_lineage_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_lineage_id := case when tg_op = 'DELETE'
                    then old.commitment_lineage_id else new.commitment_lineage_id end;
            end if;
            if target_lineage_id is null then
                return null;
            end if;
            if not exists (
                select 1 from commitment_lineages where id = target_lineage_id
            ) then
                return null;
            end if;
            select count(*) into current_count
            from dependency_events event
            where event.commitment_lineage_id = target_lineage_id
              and event.event_type in ('commitment', 'committed_date_change')
              and event.attribution_state = 'resolved'
              and event.stated_external_org_id is not null
              and not exists (
                  select 1 from dependency_events successor
                  where successor.supersedes_event_id = event.id
              );
            if current_count <> 1 then
                raise exception 'Commitment Lineage needs exactly one current accepted statement'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;

        create constraint trigger dependency_events_have_valid_commitment_lineage
        after insert or update or delete on dependency_events
        deferrable initially deferred
        for each row execute function validate_commitment_lineage_event();

        create constraint trigger commitment_lineages_have_current_statement
        after insert or update or delete on commitment_lineages
        deferrable initially deferred
        for each row execute function validate_commitment_lineage();

        create function validate_work_decision_subject()
        returns trigger
        language plpgsql
        as $$
        declare
            dependency_project_id bigint;
            dependency_dismissed_at timestamp with time zone;
            lineage_project_id bigint;
            current_statement_count integer;
        begin
            if new.dependency_id is not null then
                select project_id, dismissed_at
                  into dependency_project_id, dependency_dismissed_at
                from dependencies where id = new.dependency_id;
                if dependency_project_id is null or dependency_dismissed_at is not null then
                    raise exception 'Work Decision needs an open Dependency Coordination Subject'
                        using errcode = '23514';
                end if;
                if new.field = 'milestone_impact' then
                    raise exception 'Milestone Impact belongs to a Committed Date Change'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            select project_id into lineage_project_id
            from commitment_lineages where id = new.commitment_lineage_id;
            if lineage_project_id is null then
                raise exception 'Work Decision needs a Commitment Lineage subject'
                    using errcode = '23514';
            end if;
            select count(*) into current_statement_count
            from dependency_events event
            where event.commitment_lineage_id = new.commitment_lineage_id
              and event.event_type in ('commitment', 'committed_date_change')
              and event.attribution_state = 'resolved'
              and event.stated_external_org_id is not null
              and not exists (
                  select 1 from dependency_events successor
                  where successor.supersedes_event_id = event.id
              );
            if current_statement_count <> 1 then
                raise exception 'Work Decision needs one accepted current statement subject'
                    using errcode = '23514';
            end if;
            if new.field = 'milestone_impact' and not exists (
                select 1
                from dependency_events event
                where event.commitment_lineage_id = new.commitment_lineage_id
                  and event.event_type = 'committed_date_change'
                  and not exists (
                      select 1 from dependency_events successor
                      where successor.supersedes_event_id = event.id
                  )
            ) then
                raise exception 'Milestone Impact belongs to a Committed Date Change'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger work_decisions_have_valid_subject
        before insert or update on work_decisions
        for each row execute function validate_work_decision_subject();

        create function validate_work_decision_milestone_impact()
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
        begin
            -- This constraint function is installed on two differently
            -- shaped tables.  ``OLD.work_decision_id`` cannot be referenced
            -- directly: PL/pgSQL binds OLD to each trigger table's row type
            -- while compiling the function.  JSON access keeps the one
            -- generic validator safe for both relation shapes.
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
            return null;
        end;
        $$;

        create constraint trigger work_decisions_have_valid_milestone_impact
        after insert or update or delete on work_decisions
        deferrable initially deferred
        for each row execute function validate_work_decision_milestone_impact();

        create constraint trigger work_decision_milestone_impacts_are_valid
        after insert or update or delete on work_decision_milestone_impacts
        deferrable initially deferred
        for each row execute function validate_work_decision_milestone_impact();

        create or replace function public.purge_external_party_statement_rows(
            target_project_id bigint,
            target_purpose text
        )
        returns void
        language plpgsql
        security invoker
        set search_path = pg_catalog, public
        as $$
        begin
            if target_purpose = 'retirement' then
                perform 1 from public.legacy_ledger_archives
                where project_id = target_project_id;
                if not found then
                    raise exception 'statement retirement requires a sealed Legacy Ledger archive'
                        using errcode = '23514';
                end if;
            elsif target_purpose = 'demo_reset' then
                perform 1 from public.projects
                where id = target_project_id
                  and slug = 'corridor-demo'
                  and is_synthetic is true;
                if not found then
                    raise exception 'statement reset is allowed only for the synthetic corridor-demo project'
                        using errcode = '23514';
                end if;
            else
                raise exception 'unrecognized statement retirement purpose'
                    using errcode = '23514';
            end if;

            -- The External Party fact may retire only while no internal
            -- Coordination Plan depends on its durable lineage.  Deleting
            -- the plan would violate immutable Work Decision history; leaving
            -- it would leave a subject with no accepted statement.  The
            -- caller must use the dependent-lineage workflow instead.
            if exists (
                select 1
                from public.work_decisions decision
                join public.commitment_lineages lineage
                  on lineage.id = decision.commitment_lineage_id
                where lineage.project_id = target_project_id
            ) then
                raise exception 'statement retirement refuses a dependent Coordination Plan'
                    using errcode = '23514';
            end if;

            with event_evidence as (
                delete from public.dependency_event_evidence
                where event_id in (
                    select id from public.dependency_events
                    where project_id = target_project_id
                )
                returning evidence_link_id
            )
            delete from public.evidence_links link
            using event_evidence mapping
            where link.id = mapping.evidence_link_id;
            delete from public.dependency_event_timings where event_id in (
                select id from public.dependency_events where project_id = target_project_id
            );
            delete from public.dependency_event_scopes where event_id in (
                select id from public.dependency_events where project_id = target_project_id
            );
            delete from public.dependency_event_scope_decisions where event_id in (
                select id from public.dependency_events where project_id = target_project_id
            );
            delete from public.dependency_events where project_id = target_project_id;
            delete from public.commitment_lineages where project_id = target_project_id;
        end;
        $$;

        grant select on table work_decisions, commitment_lineages
            to corridor_statement_retirement;
        grant delete on table commitment_lineages
            to corridor_statement_retirement;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade statement contract: a Work Decision may now have "
        "an External Party Commitment Coordination Subject that cannot be "
        "round-tripped to a Dependency-only receipt"
    )
