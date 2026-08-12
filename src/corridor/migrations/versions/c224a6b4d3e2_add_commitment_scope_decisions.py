"""Record append-only Commitment Scope decisions beside statement events.

Revision ID: c224a6b4d3e2
Revises: b223f5a4c3d2

An event says what the External Party stated.  A scope decision says which
Dependencies that statement affects at one recorded moment.  The two must not
share a mutable row: correcting placement is a new decision that supersedes
the former one while retaining every former link.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c224a6b4d3e2"
down_revision: Union[str, Sequence[str], None] = "b223f5a4c3d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dependency_event_scope_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("dependency_events.id"),
            nullable=False,
        ),
        sa.Column("scope_mode", sa.String(length=16), nullable=False),
        sa.Column(
            "supersedes_scope_decision_id",
            sa.BigInteger(),
            sa.ForeignKey("dependency_event_scope_decisions.id"),
        ),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "supersedes_scope_decision_id",
            name="uq_dependency_event_scope_decision_supersedes",
        ),
        sa.CheckConstraint(
            "scope_mode in ('unknown', 'selected', 'all_active')",
            name="ck_dependency_event_scope_decisions_mode",
        ),
        sa.CheckConstraint(
            "length(trim(decided_by)) > 0",
            name="ck_dependency_event_scope_decisions_actor",
        ),
    )
    op.create_index(
        "uq_dependency_event_scope_decision_root",
        "dependency_event_scope_decisions",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_scope_decision_id is null"),
    )
    op.add_column(
        "dependency_event_scopes",
        sa.Column("scope_decision_id", sa.BigInteger()),
    )
    op.add_column("dependency_event_scopes", sa.Column("recorded_by", sa.Text()))
    op.create_foreign_key(
        "fk_dependency_event_scopes_scope_decision",
        "dependency_event_scopes",
        "dependency_event_scope_decisions",
        ["scope_decision_id"],
        ["id"],
    )
    op.execute(
        "alter table dependency_event_scopes drop constraint "
        "dependency_event_scopes_event_id_dependency_id_key"
    )
    op.create_unique_constraint(
        "uq_dependency_event_scopes_decision_dependency",
        "dependency_event_scopes",
        ["scope_decision_id", "dependency_id"],
    )

    # Direct event links had one immutable lifetime.  New scope corrections
    # must instead validate their own decision and links.  Timings retain the
    # existing statement-child trigger from #217.
    op.execute(
        """
        drop trigger dependency_event_scope_links_match_shape on dependency_event_scopes;
        drop trigger dependency_event_scope_shape_is_valid on dependency_events;
        drop trigger dependency_event_timings_match_statement on dependency_event_timings;
        drop function verify_dependency_event_scope_shape();
        drop trigger dependency_event_scope_link_is_valid on dependency_event_scopes;
        drop function validate_dependency_event_scope_link();
        drop trigger external_party_statement_scopes_are_immutable on dependency_event_scopes;
        drop trigger external_party_statement_scopes_reject_truncate on dependency_event_scopes;

        create function is_attributable_statement_scope_actor(actor text)
        returns boolean
        language sql
        immutable
        as $$
            select actor in (
                'corridor:event-admission',
                'corridor:statement-migration-v1'
            )
            or (
                actor ~ '^[a-z][a-z0-9._-]{1,31}:[^[:space:]]+$'
                and lower(substring(actor from '^[^:]+:(.*)$')) not in (
                    'agent', 'demo', 'extractor', 'reviewer', 'system'
                )
            );
        $$;

        create function create_initial_dependency_event_scope_decision()
        returns trigger
        language plpgsql
        as $$
        begin
            if not is_attributable_statement_scope_actor(new.created_by) then
                raise exception 'Commitment Scope decision needs a named human or deployed policy actor'
                    using errcode = '23514';
            end if;
            insert into dependency_event_scope_decisions
                (event_id, scope_mode, decided_by)
            values (new.id, new.scope_mode, new.created_by);
            return new;
        end;
        $$;

        create trigger dependency_events_receive_initial_scope_decision
        after insert on dependency_events
        for each row execute function create_initial_dependency_event_scope_decision();

        create function validate_dependency_event_scope_decision_actor()
        returns trigger
        language plpgsql
        as $$
        begin
            if not is_attributable_statement_scope_actor(new.decided_by) then
                raise exception 'Commitment Scope decision needs a named human or deployed policy actor'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger dependency_event_scope_decision_actor_is_valid
        before insert or update on dependency_event_scope_decisions
        for each row execute function validate_dependency_event_scope_decision_actor();

        create function validate_dependency_event_scope_decision_link()
        returns trigger
        language plpgsql
        as $$
        declare
            decision_event_id bigint;
            decision_mode text;
            decision_actor text;
            event_project bigint;
            event_party bigint;
            dependency_project bigint;
            dependency_party bigint;
            dependency_dismissed timestamp with time zone;
            dependency_status text;
        begin
            if new.scope_decision_id is null then
                select decision.id, decision.decided_by
                  into new.scope_decision_id, decision_actor
                from dependency_event_scope_decisions decision
                where decision.event_id = new.event_id
                  and not exists (
                      select 1 from dependency_event_scope_decisions later
                      where later.supersedes_scope_decision_id = decision.id
                  )
                order by decision.id;
            end if;
            select decision.event_id, decision.scope_mode, decision.decided_by,
                   event.project_id, event.affected_external_org_id
              into decision_event_id, decision_mode, decision_actor,
                   event_project, event_party
            from dependency_event_scope_decisions decision
            join dependency_events event on event.id = decision.event_id
            where decision.id = new.scope_decision_id;
            if decision_event_id is null or decision_event_id <> new.event_id then
                raise exception 'statement scope link must belong to its event decision'
                    using errcode = '23514';
            end if;
            if new.recorded_by is null then
                new.recorded_by := decision_actor;
            end if;
            if not is_attributable_statement_scope_actor(new.recorded_by) then
                raise exception 'statement scope link needs a named human or deployed policy actor'
                    using errcode = '23514';
            end if;
            if decision_mode = 'unknown' then
                raise exception 'unknown statement scope cannot link a Dependency'
                    using errcode = '23514';
            end if;
            select project_id, external_org_id, dismissed_at, status
              into dependency_project, dependency_party, dependency_dismissed,
                   dependency_status
            from dependencies where id = new.dependency_id;
            if event_project is distinct from dependency_project then
                raise exception 'statement scope cannot cross projects'
                    using errcode = '23514';
            end if;
            if event_party is distinct from dependency_party then
                raise exception 'statement scope names another External Party'
                    using errcode = '23514';
            end if;
            if dependency_dismissed is not null then
                raise exception 'statement scope cannot include a dismissed Dependency'
                    using errcode = '23514';
            end if;
            if dependency_status = 'closed' then
                raise exception 'statement scope cannot include a closed Dependency'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger dependency_event_scope_decision_link_is_valid
        before insert or update on dependency_event_scopes
        for each row execute function validate_dependency_event_scope_decision_link();

        create function validate_dependency_event_scope_decision()
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
            if target_mode in ('selected', 'all_active') and link_count = 0 then
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

        create constraint trigger dependency_event_scope_decision_shape_is_valid
        after insert or update or delete on dependency_event_scope_decisions
        deferrable initially deferred
        for each row execute function validate_dependency_event_scope_decision();

        create constraint trigger dependency_event_scope_decision_links_match_shape
        after insert or update or delete on dependency_event_scopes
        deferrable initially deferred
        for each row execute function validate_dependency_event_scope_decision();

        create function validate_verbal_statement_shape()
        returns trigger
        language plpgsql
        as $$
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
        $$;

        create constraint trigger verbal_statements_match_shape
        after insert or update or delete on dependency_events
        deferrable initially deferred
        for each row execute function validate_verbal_statement_shape();

        create constraint trigger verbal_statement_timings_match_shape
        after insert or update or delete on dependency_event_timings
        deferrable initially deferred
        for each row execute function validate_verbal_statement_shape();

        create function reject_dependency_event_scope_decision_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            if current_user <> 'corridor_statement_retirement' then
                raise exception 'Commitment Scope decisions are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

        create function reject_dependency_event_scope_link_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            if current_user <> 'corridor_statement_retirement' then
                raise exception 'Commitment Scope links are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

        create trigger dependency_event_scope_decisions_are_immutable
        before update or delete on dependency_event_scope_decisions
        for each row execute function reject_dependency_event_scope_decision_mutation();

        create trigger dependency_event_scope_links_are_immutable
        before update or delete on dependency_event_scopes
        for each row execute function reject_dependency_event_scope_link_mutation();

        create trigger dependency_event_scope_decisions_reject_truncate
        before truncate on dependency_event_scope_decisions
        for each statement execute function reject_external_party_statement_truncate();

        create trigger dependency_event_scope_links_reject_truncate
        before truncate on dependency_event_scopes
        for each statement execute function reject_external_party_statement_truncate();

        grant select, delete on table dependency_event_scope_decisions
            to corridor_statement_retirement;

        create or replace function public.purge_external_party_statement_rows(
            target_project_id bigint,
            target_purpose text
        )
        returns void
        language plpgsql
        security definer
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

            delete from public.evidence_links where event_id in (
                select id from public.dependency_events where project_id = target_project_id
            );
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
        end;
        $$;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_correction = bind.execute(
        sa.text(
            "select exists (select 1 from dependency_event_scope_decisions "
            "where supersedes_scope_decision_id is not null)"
        )
    ).scalar()
    if has_correction:
        raise RuntimeError(
            "cannot downgrade Commitment Scope decisions with a recorded correction"
        )
    op.execute(
        """
        drop trigger dependency_event_scope_links_reject_truncate on dependency_event_scopes;
        drop trigger dependency_event_scope_decisions_reject_truncate on dependency_event_scope_decisions;
        drop trigger dependency_event_scope_links_are_immutable on dependency_event_scopes;
        drop trigger dependency_event_scope_decisions_are_immutable on dependency_event_scope_decisions;
        drop function reject_dependency_event_scope_link_mutation();
        drop function reject_dependency_event_scope_decision_mutation();
        drop trigger dependency_event_scope_decision_actor_is_valid
            on dependency_event_scope_decisions;
        drop function validate_dependency_event_scope_decision_actor();
        drop trigger dependency_event_scope_decision_links_match_shape on dependency_event_scopes;
        drop trigger dependency_event_scope_decision_shape_is_valid on dependency_event_scope_decisions;
        drop trigger verbal_statement_timings_match_shape on dependency_event_timings;
        drop trigger verbal_statements_match_shape on dependency_events;
        drop function validate_verbal_statement_shape();
        drop function validate_dependency_event_scope_decision();
        drop trigger dependency_event_scope_decision_link_is_valid on dependency_event_scopes;
        drop function validate_dependency_event_scope_decision_link();
        drop trigger dependency_events_receive_initial_scope_decision on dependency_events;
        drop function create_initial_dependency_event_scope_decision();
        drop function is_attributable_statement_scope_actor(text);
        """
    )
    op.drop_constraint(
        "uq_dependency_event_scopes_decision_dependency",
        "dependency_event_scopes",
        type_="unique",
    )
    op.create_unique_constraint(
        "dependency_event_scopes_event_id_dependency_id_key",
        "dependency_event_scopes",
        ["event_id", "dependency_id"],
    )
    op.drop_constraint(
        "fk_dependency_event_scopes_scope_decision",
        "dependency_event_scopes",
        type_="foreignkey",
    )
    op.drop_column("dependency_event_scopes", "recorded_by")
    op.drop_column("dependency_event_scopes", "scope_decision_id")
    op.drop_index(
        "uq_dependency_event_scope_decision_root",
        table_name="dependency_event_scope_decisions",
    )
    op.drop_table("dependency_event_scope_decisions")

    # Restore #217's direct-scope guards for the parent revision.
    op.execute(
        """
        create function validate_dependency_event_scope_link()
        returns trigger language plpgsql as $$
        declare
            event_project bigint; event_party bigint; event_mode text;
            dependency_project bigint; dependency_party bigint;
            dependency_dismissed timestamp with time zone;
        begin
            select project_id, affected_external_org_id, scope_mode
              into event_project, event_party, event_mode
            from dependency_events where id = new.event_id;
            select project_id, external_org_id, dismissed_at
              into dependency_project, dependency_party, dependency_dismissed
            from dependencies where id = new.dependency_id;
            if event_mode = 'unknown' then
                raise exception 'unknown statement scope cannot link a Dependency' using errcode = '23514';
            end if;
            if event_project is distinct from dependency_project then
                raise exception 'statement scope cannot cross projects' using errcode = '23514';
            end if;
            if event_party is distinct from dependency_party then
                raise exception 'statement scope names another External Party' using errcode = '23514';
            end if;
            if dependency_dismissed is not null then
                raise exception 'statement scope cannot include a dismissed Dependency' using errcode = '23514';
            end if;
            return new;
        end;
        $$;
        create trigger dependency_event_scope_link_is_valid
        before insert or update on dependency_event_scopes
        for each row execute function validate_dependency_event_scope_link();

        create function verify_dependency_event_scope_shape()
        returns trigger language plpgsql as $$
        declare
            target_event_id bigint; target_mode text; target_source text;
            target_event_date date; link_count integer; new_precision text;
            previous_count integer;
        begin
            if tg_table_name = 'dependency_events' then
                target_event_id := case when tg_op = 'DELETE' then old.id else new.id end;
            elsif tg_table_name = 'dependency_event_timings' then
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
            else
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
            end if;
            select scope_mode, source_kind, event_date
              into target_mode, target_source, target_event_date
            from dependency_events where id = target_event_id;
            if target_mode is null then return null; end if;
            select count(*) into link_count from dependency_event_scopes where event_id = target_event_id;
            if target_mode = 'unknown' and link_count <> 0 then
                raise exception 'unknown statement scope has Dependency links' using errcode = '23514';
            end if;
            if target_mode in ('selected', 'all_active') and link_count = 0 then
                raise exception 'known statement scope has no Dependency links' using errcode = '23514';
            end if;
            if target_source = 'verbal' then
                select precision into new_precision from dependency_event_timings
                where event_id = target_event_id and kind = 'new';
                select count(*) into previous_count from dependency_event_timings
                where event_id = target_event_id and kind = 'previous';
                if target_event_date is null or target_mode <> 'selected'
                   or link_count <> 1 or new_precision is distinct from 'day'
                   or previous_count <> 0 then
                    raise exception 'verbal statements require one exact-day commitment and one Dependency'
                        using errcode = '23514';
                end if;
            end if;
            return null;
        end;
        $$;
        create constraint trigger dependency_event_scope_shape_is_valid
        after insert or update or delete on dependency_events
        deferrable initially deferred for each row
        execute function verify_dependency_event_scope_shape();
        create constraint trigger dependency_event_scope_links_match_shape
        after insert or update or delete on dependency_event_scopes
        deferrable initially deferred for each row
        execute function verify_dependency_event_scope_shape();
        create constraint trigger dependency_event_timings_match_statement
        after insert or update or delete on dependency_event_timings
        deferrable initially deferred for each row
        execute function verify_dependency_event_scope_shape();
        create trigger external_party_statement_scopes_are_immutable
        before update or delete on dependency_event_scopes
        for each row execute function reject_external_party_statement_child_mutation();
        create trigger external_party_statement_scopes_reject_truncate
        before truncate on dependency_event_scopes
        for each statement execute function reject_external_party_statement_truncate();

        create or replace function public.purge_external_party_statement_rows(
            target_project_id bigint, target_purpose text
        ) returns void language plpgsql security definer
        set search_path = pg_catalog, public as $$
        begin
            if target_purpose = 'retirement' then
                perform 1 from public.legacy_ledger_archives where project_id = target_project_id;
                if not found then
                    raise exception 'statement retirement requires a sealed Legacy Ledger archive' using errcode = '23514';
                end if;
            elsif target_purpose = 'demo_reset' then
                perform 1 from public.projects where id = target_project_id
                  and slug = 'corridor-demo' and is_synthetic is true;
                if not found then
                    raise exception 'statement reset is allowed only for the synthetic corridor-demo project' using errcode = '23514';
                end if;
            else
                raise exception 'unrecognized statement retirement purpose' using errcode = '23514';
            end if;
            delete from public.evidence_links where event_id in (select id from public.dependency_events where project_id = target_project_id);
            delete from public.dependency_event_timings where event_id in (select id from public.dependency_events where project_id = target_project_id);
            delete from public.dependency_event_scopes where event_id in (select id from public.dependency_events where project_id = target_project_id);
            delete from public.dependency_events where project_id = target_project_id;
        end;
        $$;
        """
    )
