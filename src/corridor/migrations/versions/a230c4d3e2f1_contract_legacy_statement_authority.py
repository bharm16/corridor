"""Contract statement storage to the structured External Party authority.

Revision ID: a230c4d3e2f1
Revises: f227b9e4d3c2

The additive migrations deliberately kept three transitional shadows while
readers and writers moved: the event id on EvidenceLink, its inline readiness
bit, and Dependency.committed_date.  Event ownership and readiness now live in
their dedicated rows.  The date column remains for compatibility only and is
reconciled from the structured statement before this migration removes either
of the other shadows.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a230c4d3e2f1"
down_revision: Union[str, Sequence[str], None] = "f227b9e4d3c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Do not begin DDL until the two old evidence views can be proved to have
    # their structured counterparts.  ``event_id`` is still available in this
    # preflight precisely so it can identify the one mapping being contracted.
    op.execute(
        """
        do $$
        begin
            if exists (
                select 1
                from evidence_links link
                left join dependency_event_evidence mapping
                  on mapping.evidence_link_id = link.id
                group by link.id, link.event_id
                having (
                    link.event_id is not null
                    and (
                        count(mapping.evidence_link_id) <> 1
                        or min(mapping.event_id) is distinct from link.event_id
                    )
                )
                or (
                    link.event_id is null
                    and count(mapping.evidence_link_id) <> 0
                )
            ) then
                raise exception
                    'statement contract preflight found an incomplete event Evidence mapping'
                    using errcode = '23514';
            end if;

            if exists (
                select 1
                from dependency_evidence_sufficiencies sufficiency
                left join dependency_event_evidence mapping
                  on mapping.evidence_link_id = sufficiency.evidence_link_id
                left join dependency_event_scopes scope
                  on scope.id = sufficiency.scope_link_id
                where mapping.evidence_link_id is not null
                  and (
                      scope.id is null
                      or scope.event_id <> mapping.event_id
                      or scope.dependency_id <> sufficiency.dependency_id
                  )
            ) then
                raise exception
                    'statement contract preflight found an incomplete event Evidence role'
                    using errcode = '23514';
            end if;
        end;
        $$;
        """
    )

    # The compatibility date is never an independent claim.  A month,
    # approximation, unknown legacy precision, unknown scope, or a stale
    # direct scalar therefore clears rather than publishing a day nobody can
    # derive from the current structured statement.
    op.execute(
        """
        update dependencies dependency
        set committed_date = (
            select case when timing.precision = 'day' then timing.start_date end
            from dependency_event_scopes scope
            join dependency_event_scope_decisions decision
              on decision.id = scope.scope_decision_id
            join dependency_events event on event.id = scope.event_id
            join dependency_event_timings timing
              on timing.event_id = event.id and timing.kind = 'new'
            where scope.dependency_id = dependency.id
              and event.event_type in ('commitment', 'committed_date_change')
              and decision.scope_mode in ('selected', 'all_active')
              and not exists (
                  select 1
                  from dependency_event_scope_decisions later
                  where later.supersedes_scope_decision_id = decision.id
              )
            order by event.event_date desc nulls last, event.id desc
            limit 1
        )
        where dependency.committed_date is distinct from (
            select case when timing.precision = 'day' then timing.start_date end
            from dependency_event_scopes scope
            join dependency_event_scope_decisions decision
              on decision.id = scope.scope_decision_id
            join dependency_events event on event.id = scope.event_id
            join dependency_event_timings timing
              on timing.event_id = event.id and timing.kind = 'new'
            where scope.dependency_id = dependency.id
              and event.event_type in ('commitment', 'committed_date_change')
              and decision.scope_mode in ('selected', 'all_active')
              and not exists (
                  select 1
                  from dependency_event_scope_decisions later
                  where later.supersedes_scope_decision_id = decision.id
              )
            order by event.event_date desc nulls last, event.id desc
            limit 1
        );

        do $$
        begin
            if exists (
                select 1
                from dependencies dependency
                where dependency.committed_date is distinct from (
                    select case when timing.precision = 'day' then timing.start_date end
                    from dependency_event_scopes scope
                    join dependency_event_scope_decisions decision
                      on decision.id = scope.scope_decision_id
                    join dependency_events event on event.id = scope.event_id
                    join dependency_event_timings timing
                      on timing.event_id = event.id and timing.kind = 'new'
                    where scope.dependency_id = dependency.id
                      and event.event_type in ('commitment', 'committed_date_change')
                      and decision.scope_mode in ('selected', 'all_active')
                      and not exists (
                          select 1
                          from dependency_event_scope_decisions later
                          where later.supersedes_scope_decision_id = decision.id
                      )
                    order by event.event_date desc nulls last, event.id desc
                    limit 1
                )
            ) then
                raise exception
                    'statement contract preflight found a Committed Date shadow that disagrees with structured authority'
                    using errcode = '23514';
            end if;
        end;
        $$;
        """
    )

    # Direct Evidence used the inline bit until statement Evidence gained
    # explicit roles.  Give direct Evidence the same dedicated row before
    # dropping that bit.  Event Evidence already has a role row keyed to its
    # exact scope link.
    op.alter_column(
        "dependency_evidence_sufficiencies", "scope_link_id", nullable=True
    )
    op.execute(
        """
        insert into dependency_evidence_sufficiencies
            (dependency_id, evidence_link_id, scope_link_id)
        select link.dependency_id, link.id, null
        from evidence_links link
        where link.event_id is null
          and link.satisfies_requirement is true
        on conflict do nothing;
        """
    )
    op.create_index(
        "uq_dependency_evidence_sufficiency_direct_evidence",
        "dependency_evidence_sufficiencies",
        ["evidence_link_id"],
        unique=True,
        postgresql_where=sa.text("scope_link_id is null"),
    )

    op.execute(
        """
        drop trigger evidence_links_receive_event_evidence on evidence_links;
        drop function create_dependency_event_evidence();
        drop trigger dependency_event_evidence_is_valid on dependency_event_evidence;
        drop function validate_dependency_event_evidence();
        drop trigger dependency_evidence_sufficiency_scope_is_valid
            on dependency_evidence_sufficiencies;
        drop function validate_dependency_event_evidence_scope_role();
        drop trigger operative_event_evidence_scope_is_valid on operative_support;
        drop function validate_operative_event_evidence_scope_role();
        drop trigger external_party_statement_evidence_is_immutable on evidence_links;
        drop function reject_external_party_statement_evidence_mutation();
        """
    )
    op.drop_constraint(
        "ck_evidence_links_event_ownership", "evidence_links", type_="check"
    )
    op.drop_constraint(
        "evidence_links_event_id_fkey", "evidence_links", type_="foreignkey"
    )
    op.drop_column("evidence_links", "event_id")
    op.drop_column("evidence_links", "satisfies_requirement")

    op.execute(
        """
        create function validate_dependency_event_evidence()
        returns trigger
        language plpgsql
        as $$
        declare
            direct_dependency_id bigint;
        begin
            select dependency_id into direct_dependency_id
            from evidence_links where id = new.evidence_link_id;
            if direct_dependency_id is not null then
                raise exception 'event Evidence cannot carry direct Dependency ownership'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger dependency_event_evidence_is_valid
        before insert or update on dependency_event_evidence
        for each row execute function validate_dependency_event_evidence();

        create function validate_evidence_link_ownership()
        returns trigger
        language plpgsql
        as $$
        declare
            target_evidence_link_id bigint;
            direct_dependency_id bigint;
            mapping_count integer;
        begin
            target_evidence_link_id := case when tg_table_name = 'evidence_links'
                then coalesce(
                    (to_jsonb(old)->>'id')::bigint,
                    (to_jsonb(new)->>'id')::bigint
                )
                else coalesce(
                    (to_jsonb(old)->>'evidence_link_id')::bigint,
                    (to_jsonb(new)->>'evidence_link_id')::bigint
                )
            end;
            select dependency_id into direct_dependency_id
            from evidence_links where id = target_evidence_link_id;
            if not found then
                return null;
            end if;
            select count(*) into mapping_count
            from dependency_event_evidence
            where evidence_link_id = target_evidence_link_id;
            if direct_dependency_id is null and mapping_count <> 1 then
                raise exception 'statement Evidence needs exactly one event owner'
                    using errcode = '23514';
            end if;
            if direct_dependency_id is not null and mapping_count <> 0 then
                raise exception 'direct Evidence cannot carry an event owner'
                    using errcode = '23514';
            end if;
            if exists (
                select 1
                from dependency_evidence_sufficiencies sufficiency
                where sufficiency.evidence_link_id = target_evidence_link_id
                  and (
                      direct_dependency_id is null
                      or sufficiency.scope_link_id is not null
                      or sufficiency.dependency_id is distinct from direct_dependency_id
                  )
            ) then
                raise exception 'direct Evidence roles must retain the direct Dependency owner'
                    using errcode = '23514';
            end if;
            if exists (
                select 1
                from operative_support support
                where support.evidence_link_id = target_evidence_link_id
                  and (
                      direct_dependency_id is null
                      or support.scope_link_id is not null
                      or support.dependency_id is distinct from direct_dependency_id
                  )
            ) then
                raise exception 'direct Evidence support must retain the direct Dependency owner'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;

        create constraint trigger evidence_links_have_one_owner
        after insert or update or delete on evidence_links
        deferrable initially deferred
        for each row execute function validate_evidence_link_ownership();

        create constraint trigger dependency_event_evidence_has_one_owner
        after insert or update or delete on dependency_event_evidence
        deferrable initially deferred
        for each row execute function validate_evidence_link_ownership();

        create function validate_dependency_evidence_sufficiency_scope_role()
        returns trigger
        language plpgsql
        as $$
        declare
            direct_dependency_id bigint;
            scope_event_id bigint;
            scope_dependency_id bigint;
            evidence_event_id bigint;
        begin
            select dependency_id into direct_dependency_id
            from evidence_links where id = new.evidence_link_id;
            select event_id into evidence_event_id
            from dependency_event_evidence
            where evidence_link_id = new.evidence_link_id;
            if evidence_event_id is null then
                if new.scope_link_id is not null
                   or direct_dependency_id is distinct from new.dependency_id then
                    raise exception 'direct Evidence role must name its direct Dependency'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            if new.scope_link_id is null then
                raise exception 'event Evidence role must name its exact Dependency scope link'
                    using errcode = '23514';
            end if;
            select scope.event_id, scope.dependency_id
              into scope_event_id, scope_dependency_id
            from dependency_event_scopes scope where scope.id = new.scope_link_id;
            if scope_event_id is distinct from evidence_event_id
               or scope_dependency_id is distinct from new.dependency_id then
                raise exception 'event Evidence role must name its exact Dependency scope link'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger dependency_evidence_sufficiency_scope_is_valid
        before insert or update on dependency_evidence_sufficiencies
        for each row execute function validate_dependency_evidence_sufficiency_scope_role();

        create function validate_operative_event_evidence_scope_role()
        returns trigger
        language plpgsql
        as $$
        declare
            direct_dependency_id bigint;
            scope_event_id bigint;
            scope_dependency_id bigint;
            evidence_event_id bigint;
        begin
            select dependency_id into direct_dependency_id
            from evidence_links where id = new.evidence_link_id;
            select event_id into evidence_event_id
            from dependency_event_evidence
            where evidence_link_id = new.evidence_link_id;
            if evidence_event_id is null then
                if new.scope_link_id is not null
                   or direct_dependency_id is distinct from new.dependency_id then
                    raise exception 'direct Evidence role must name its direct Dependency'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            if new.scope_link_id is null then
                raise exception 'event Evidence publication support needs its exact scope link'
                    using errcode = '23514';
            end if;
            select scope.event_id, scope.dependency_id
              into scope_event_id, scope_dependency_id
            from dependency_event_scopes scope where scope.id = new.scope_link_id;
            if scope_event_id is distinct from evidence_event_id
               or scope_dependency_id is distinct from new.dependency_id then
                raise exception 'event Evidence role must name its exact Dependency scope link'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger operative_event_evidence_scope_is_valid
        before insert or update on operative_support
        for each row execute function validate_operative_event_evidence_scope_role();

        create function reject_external_party_statement_evidence_mutation()
        returns trigger
        language plpgsql
        as $$
        declare
            target_evidence_link_id bigint;
        begin
            target_evidence_link_id := case when tg_op = 'DELETE'
                then old.id else coalesce(old.id, new.id) end;
            if current_user <> 'corridor_statement_retirement'
               and exists (
                   select 1 from dependency_event_evidence mapping
                   where mapping.evidence_link_id = target_evidence_link_id
               ) then
                raise exception 'External Party statement Evidence is append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

        create trigger external_party_statement_evidence_is_immutable
        before update or delete on evidence_links
        for each row execute function reject_external_party_statement_evidence_mutation();

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
        end;
        $$;
        """
    )


def downgrade() -> None:
    # A later structured act cannot be translated to the #216 scalar event
    # shape.  This guard intentionally runs before any DDL, so a refused
    # downgrade leaves the entire current schema and every row untouched.
    bind = op.get_bind()
    unrepresentable = bind.execute(
        sa.text(
            """
            select exists (
                select 1
                from dependency_events event
                where event.attribution_state <> 'unresolved'
                   or event.stated_external_org_id is not null
                   or event.scope_mode <> 'selected'
                   or event.timing_direction is not null
                   or (
                       select count(*)
                       from dependency_event_scopes scope
                       join dependency_event_scope_decisions decision
                         on decision.id = scope.scope_decision_id
                       where scope.event_id = event.id
                         and decision.scope_mode = 'selected'
                         and not exists (
                             select 1
                             from dependency_event_scope_decisions later
                             where later.supersedes_scope_decision_id = decision.id
                         )
                   ) <> 1
                   or exists (
                       select 1 from dependency_event_scope_decisions decision
                       where decision.event_id = event.id
                         and decision.supersedes_scope_decision_id is not null
                   )
                   or exists (
                       select 1 from dependency_event_timings timing
                       where timing.event_id = event.id
                         and (
                             timing.precision <> 'day'
                             or timing.kind = 'previous'
                         )
                   )
                   or (
                       event.source_kind = 'verbal'
                       and event.event_type <> 'commitment'
                   )
            )
            """
        )
    ).scalar()
    if unrepresentable:
        raise RuntimeError(
            "cannot downgrade statement contract: structured scope, timing, "
            "attribution, or correction cannot round-trip to the legacy shape"
        )

    op.execute(
        """
        drop trigger operative_event_evidence_scope_is_valid on operative_support;
        drop function validate_operative_event_evidence_scope_role();
        drop trigger dependency_evidence_sufficiency_scope_is_valid
            on dependency_evidence_sufficiencies;
        drop function validate_dependency_evidence_sufficiency_scope_role();
        drop trigger external_party_statement_evidence_is_immutable on evidence_links;
        drop function reject_external_party_statement_evidence_mutation();
        drop trigger dependency_event_evidence_has_one_owner
            on dependency_event_evidence;
        drop trigger evidence_links_have_one_owner on evidence_links;
        drop function validate_evidence_link_ownership();
        drop trigger dependency_event_evidence_is_valid on dependency_event_evidence;
        drop function validate_dependency_event_evidence();
        """
    )
    op.drop_index(
        "uq_dependency_evidence_sufficiency_direct_evidence",
        table_name="dependency_evidence_sufficiencies",
    )
    op.add_column("evidence_links", sa.Column("event_id", sa.BigInteger()))
    op.add_column(
        "evidence_links",
        sa.Column(
            "satisfies_requirement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_foreign_key(
        "evidence_links_event_id_fkey",
        "evidence_links",
        "dependency_events",
        ["event_id"],
        ["id"],
    )
    op.execute(
        """
        update evidence_links link
        set event_id = mapping.event_id
        from dependency_event_evidence mapping
        where mapping.evidence_link_id = link.id;

        update evidence_links link
        set satisfies_requirement = true
        from dependency_evidence_sufficiencies sufficiency
        where sufficiency.evidence_link_id = link.id
          and sufficiency.scope_link_id is null;

        delete from dependency_evidence_sufficiencies
        where scope_link_id is null;
        """
    )
    op.alter_column(
        "dependency_evidence_sufficiencies", "scope_link_id", nullable=False
    )
    op.create_check_constraint(
        "ck_evidence_links_event_ownership",
        "evidence_links",
        "(event_id is null and dependency_id is not null) or "
        "(event_id is not null and dependency_id is null and satisfies_requirement is false)",
    )
    op.execute(
        """
        create function validate_dependency_event_evidence()
        returns trigger
        language plpgsql
        as $$
        declare
            linked_event_id bigint;
        begin
            select event_id into linked_event_id
            from evidence_links where id = new.evidence_link_id;
            if linked_event_id is null or linked_event_id <> new.event_id then
                raise exception 'event Evidence must retain the same event-owned Evidence identity'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger dependency_event_evidence_is_valid
        before insert or update on dependency_event_evidence
        for each row execute function validate_dependency_event_evidence();

        create function create_dependency_event_evidence()
        returns trigger
        language plpgsql
        as $$
        declare
            event_actor text;
        begin
            if new.event_id is not null then
                select created_by into event_actor
                from dependency_events where id = new.event_id;
                insert into dependency_event_evidence
                    (evidence_link_id, event_id, recorded_by)
                values (new.id, new.event_id, event_actor)
                on conflict (evidence_link_id) do nothing;
            end if;
            return new;
        end;
        $$;

        create trigger evidence_links_receive_event_evidence
        after insert on evidence_links
        for each row execute function create_dependency_event_evidence();

        create function validate_dependency_event_evidence_scope_role()
        returns trigger
        language plpgsql
        as $$
        declare
            scope_event_id bigint;
            scope_dependency_id bigint;
            evidence_event_id bigint;
        begin
            if new.scope_link_id is null then
                return new;
            end if;
            select scope.event_id, scope.dependency_id
              into scope_event_id, scope_dependency_id
            from dependency_event_scopes scope where scope.id = new.scope_link_id;
            select event_id into evidence_event_id
            from dependency_event_evidence
            where evidence_link_id = new.evidence_link_id;
            if evidence_event_id is null
               or scope_event_id <> evidence_event_id
               or scope_dependency_id <> new.dependency_id then
                raise exception 'event Evidence role must name its exact Dependency scope link'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger dependency_evidence_sufficiency_scope_is_valid
        before insert or update on dependency_evidence_sufficiencies
        for each row execute function validate_dependency_event_evidence_scope_role();

        create function validate_operative_event_evidence_scope_role()
        returns trigger
        language plpgsql
        as $$
        declare
            scope_event_id bigint;
            scope_dependency_id bigint;
            evidence_event_id bigint;
        begin
            select event_id into evidence_event_id
            from dependency_event_evidence
            where evidence_link_id = new.evidence_link_id;
            if evidence_event_id is null then
                if new.scope_link_id is not null then
                    raise exception 'direct Evidence cannot claim a statement scope link'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            if new.scope_link_id is null then
                raise exception 'event Evidence publication support needs its exact scope link'
                    using errcode = '23514';
            end if;
            select scope.event_id, scope.dependency_id
              into scope_event_id, scope_dependency_id
            from dependency_event_scopes scope where scope.id = new.scope_link_id;
            if scope_event_id <> evidence_event_id
               or scope_dependency_id <> new.dependency_id then
                raise exception 'event Evidence role must name its exact Dependency scope link'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger operative_event_evidence_scope_is_valid
        before insert or update on operative_support
        for each row execute function validate_operative_event_evidence_scope_role();

        create function reject_external_party_statement_evidence_mutation()
        returns trigger
        language plpgsql
        as $$
        declare
            target_event_id bigint;
        begin
            if tg_op = 'DELETE' then
                target_event_id := old.event_id;
            else
                target_event_id := coalesce(old.event_id, new.event_id);
            end if;
            if target_event_id is not null
               and current_user <> 'corridor_statement_retirement' then
                raise exception 'External Party statement Evidence is append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

        create trigger external_party_statement_evidence_is_immutable
        before update or delete on evidence_links
        for each row execute function reject_external_party_statement_evidence_mutation();

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

            delete from public.dependency_event_evidence where event_id in (
                select id from public.dependency_events where project_id = target_project_id
            );
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
