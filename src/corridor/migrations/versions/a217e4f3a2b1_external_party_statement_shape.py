"""Represent attributable External Party statements with explicit timing and scope.

Revision ID: a217e4f3a2b1
Revises: e216f5a4b3c2

Existing event identities and EvidenceLink ids stay intact.  Their direct
Dependency ownership becomes one selected scope link; old normalized dates are
kept only as legacy-unknown timing, except for an existing Verbal's explicit
phone-entry day, because a cited scalar cannot prove what precision the source
actually stated.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a217e4f3a2b1"
down_revision: Union[str, Sequence[str], None] = "e216f5a4b3c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # #216's row trigger correctly blocks ordinary mutation, but this
    # transaction must attach the new shape to its immutable historical rows
    # before reenabling it.
    op.execute(
        "alter table dependency_events disable trigger verbal_dependency_events_are_immutable"
    )
    op.add_column("dependency_events", sa.Column("project_id", sa.BigInteger()))
    op.add_column(
        "dependency_events", sa.Column("affected_external_org_id", sa.BigInteger())
    )
    op.add_column(
        "dependency_events", sa.Column("stated_external_org_id", sa.BigInteger())
    )
    op.add_column(
        "dependency_events",
        sa.Column("scope_mode", sa.String(length=16), server_default="selected"),
    )
    op.add_column(
        "dependency_events", sa.Column("timing_direction", sa.String(length=16))
    )
    op.execute(
        """
        update dependency_events event
        set project_id = dependency.project_id,
            affected_external_org_id = dependency.external_org_id,
            scope_mode = 'selected'
        from dependencies dependency
        where event.dependency_id = dependency.id
        """
    )
    op.alter_column("dependency_events", "project_id", nullable=False)
    op.alter_column("dependency_events", "scope_mode", nullable=False)
    op.create_foreign_key(
        "fk_dependency_events_project", "dependency_events", "projects", ["project_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_dependency_events_affected_external_org",
        "dependency_events",
        "external_orgs",
        ["affected_external_org_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_dependency_events_stated_external_org",
        "dependency_events",
        "external_orgs",
        ["stated_external_org_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_dependency_events_scope_mode",
        "dependency_events",
        "scope_mode in ('unknown', 'selected', 'all_active')",
    )
    op.create_check_constraint(
        "ck_dependency_events_timing_direction",
        "dependency_events",
        "timing_direction is null or timing_direction in ('earlier', 'later', 'unknown')",
    )
    op.drop_constraint("event_type", "dependency_events", type_="check")
    # A legacy scalar Slip does not prove both old and new source timings.
    # It becomes the ordinary commitment its surviving source can support;
    # historical report snapshots retain the old wording independently.
    op.execute(
        "update dependency_events set event_type = 'commitment' where event_type = 'slip'"
    )
    op.execute(
        "alter table dependency_events enable trigger verbal_dependency_events_are_immutable"
    )
    op.alter_column(
        "dependency_events",
        "event_type",
        existing_type=sa.String(length=13),
        type_=sa.String(length=32),
    )
    op.create_check_constraint(
        "event_type",
        "dependency_events",
        "event_type in ('commitment', 'committed_date_change', 'response', "
        "'escalation', 'status_change', 'closure')",
    )

    op.create_table(
        "dependency_event_scopes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", sa.BigInteger(), sa.ForeignKey("dependency_events.id"), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), sa.ForeignKey("dependencies.id"), nullable=False),
        sa.UniqueConstraint("event_id", "dependency_id"),
    )
    op.execute(
        """
        insert into dependency_event_scopes (event_id, dependency_id)
        select id, dependency_id from dependency_events
        """
    )
    op.create_table(
        "dependency_event_timings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", sa.BigInteger(), sa.ForeignKey("dependency_events.id"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("precision", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.UniqueConstraint("event_id", "kind", name="uq_dependency_event_timing_kind"),
        sa.CheckConstraint("kind in ('previous', 'new')", name="ck_dependency_event_timing_kind"),
        sa.CheckConstraint(
            "precision in ('day', 'month', 'approximate', 'legacy_unknown')",
            name="ck_dependency_event_timing_precision",
        ),
        sa.CheckConstraint(
            "(precision = 'day' and start_date is not null and end_date = start_date) "
            "or (precision = 'month' and start_date is not null and end_date is not null "
            "and start_date = date_trunc('month', start_date::timestamp)::date "
            "and end_date = (date_trunc('month', start_date::timestamp) "
            "+ interval '1 month - 1 day')::date) "
            "or (precision in ('approximate', 'legacy_unknown') and start_date is null and end_date is null)",
            name="ck_dependency_event_timing_bounds",
        ),
    )
    op.execute(
        """
        insert into dependency_event_timings (event_id, kind, text, precision, start_date, end_date)
        select
            id,
            'new',
            committed_date::text,
            case when source_kind = 'verbal' then 'day' else 'legacy_unknown' end,
            case when source_kind = 'verbal' then committed_date else null end,
            case when source_kind = 'verbal' then committed_date else null end
        from dependency_events
        where committed_date is not null
        """
    )

    # Existing event Evidence links retain both their ids and source values.
    # New event provenance no longer needs a fake Dependency, so the column is
    # nullable; scope is read only from dependency_event_scopes.
    op.alter_column("evidence_links", "dependency_id", nullable=True)
    op.create_table(
        "dependency_evidence_sufficiencies",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("dependency_id", sa.BigInteger(), sa.ForeignKey("dependencies.id"), nullable=False),
        sa.Column("evidence_link_id", sa.BigInteger(), sa.ForeignKey("evidence_links.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dependency_id", "evidence_link_id"),
    )
    op.execute(
        """
        insert into dependency_evidence_sufficiencies (dependency_id, evidence_link_id)
        select dependency_id, id
        from evidence_links
        where event_id is not null and satisfies_requirement is true
        """
    )
    # Publication support is per Dependency, but its cited Evidence can now
    # be an event-level row with no fake direct owner.  Preserve existing
    # designations while replacing the legacy composite ownership FK with an
    # Evidence identity FK; readers separately verify the event scope.
    op.drop_constraint(
        "fk_operative_support_dependency_evidence",
        "operative_support",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_operative_support_evidence_link",
        "operative_support",
        "evidence_links",
        ["evidence_link_id"],
        ["id"],
    )
    op.execute(
        """
        update evidence_links
        set dependency_id = null, satisfies_requirement = false
        where event_id is not null
        """
    )
    op.create_check_constraint(
        "ck_evidence_links_event_ownership",
        "evidence_links",
        "(event_id is null and dependency_id is not null) or "
        "(event_id is not null and dependency_id is null and satisfies_requirement is false)",
    )

    op.drop_constraint(
        "ck_verbal_events_require_call_facts", "dependency_events", type_="check"
    )
    op.drop_constraint(
        "dependency_events_dependency_id_fkey", "dependency_events", type_="foreignkey"
    )
    op.drop_column("dependency_events", "dependency_id")
    op.drop_column("dependency_events", "committed_date")
    op.alter_column("dependency_events", "scope_mode", server_default="unknown")

    op.execute(
        """
        do $$
        begin
            if not exists (
                select 1 from pg_roles where rolname = 'corridor_statement_retirement'
            ) then
                create role corridor_statement_retirement nologin noinherit;
            end if;
        end;
        $$;

        grant usage on schema public to corridor_statement_retirement;
        grant select, delete on table projects, legacy_ledger_archives,
            dependency_events, dependency_event_scopes,
            dependency_event_timings, evidence_links
            to corridor_statement_retirement;

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

        create function public.purge_external_party_statement_rows(
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
                perform 1
                from public.legacy_ledger_archives
                where project_id = target_project_id;
                if not found then
                    raise exception 'statement retirement requires a sealed Legacy Ledger archive'
                        using errcode = '23514';
                end if;
            elsif target_purpose = 'demo_reset' then
                perform 1
                from public.projects
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

            delete from public.evidence_links
            where event_id in (
                select id from public.dependency_events
                where project_id = target_project_id
            );
            delete from public.dependency_event_timings
            where event_id in (
                select id from public.dependency_events
                where project_id = target_project_id
            );
            delete from public.dependency_event_scopes
            where event_id in (
                select id from public.dependency_events
                where project_id = target_project_id
            );
            delete from public.dependency_events
            where project_id = target_project_id;
        end;
        $$;

        alter function public.purge_external_party_statement_rows(bigint, text)
            owner to corridor_statement_retirement;
        revoke all on function public.purge_external_party_statement_rows(bigint, text)
            from public;
        grant execute on function public.purge_external_party_statement_rows(bigint, text)
            to corridor_statement_retirement;

        create function validate_dependency_event_scope_link()
        returns trigger
        language plpgsql
        as $$
        declare
            event_project bigint;
            event_party bigint;
            event_mode text;
            dependency_project bigint;
            dependency_party bigint;
            dependency_dismissed timestamp with time zone;
        begin
            select project_id, affected_external_org_id, scope_mode
            into event_project, event_party, event_mode
            from dependency_events where id = new.event_id;
            select project_id, external_org_id, dismissed_at
            into dependency_project, dependency_party, dependency_dismissed
            from dependencies where id = new.dependency_id;
            if event_mode = 'unknown' then
                raise exception 'unknown statement scope cannot link a Dependency'
                    using errcode = '23514';
            end if;
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
            return new;
        end;
        $$;

        create trigger dependency_event_scope_link_is_valid
        before insert or update on dependency_event_scopes
        for each row execute function validate_dependency_event_scope_link();

        create function verify_dependency_event_scope_shape()
        returns trigger
        language plpgsql
        as $$
        declare
            target_event_id bigint;
            target_mode text;
            target_source text;
            target_event_date date;
            link_count integer;
            new_precision text;
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
            -- The row is gone during a permitted retirement delete.
            if target_mode is null then
                return null;
            end if;
            select count(*) into link_count from dependency_event_scopes
                where event_id = target_event_id;
            if target_mode = 'unknown' and link_count <> 0 then
                raise exception 'unknown statement scope has Dependency links'
                    using errcode = '23514';
            end if;
            if target_mode in ('selected', 'all_active') and link_count = 0 then
                raise exception 'known statement scope has no Dependency links'
                    using errcode = '23514';
            end if;
            if target_source = 'verbal' then
                select precision into new_precision
                from dependency_event_timings
                where event_id = target_event_id and kind = 'new';
                select count(*) into previous_count
                from dependency_event_timings
                where event_id = target_event_id and kind = 'previous';
                if target_event_date is null
                   or target_mode <> 'selected'
                   or link_count <> 1
                   or new_precision is distinct from 'day'
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
        deferrable initially deferred
        for each row execute function verify_dependency_event_scope_shape();

        create constraint trigger dependency_event_scope_links_match_shape
        after insert or update or delete on dependency_event_scopes
        deferrable initially deferred
        for each row execute function verify_dependency_event_scope_shape();

        create constraint trigger dependency_event_timings_match_statement
        after insert or update or delete on dependency_event_timings
        deferrable initially deferred
        for each row execute function verify_dependency_event_scope_shape();

        create function reject_external_party_statement_child_mutation()
        returns trigger
        language plpgsql
        as $$
        declare
            target_event_id bigint;
            target_source text;
        begin
            if current_user <> 'corridor_statement_retirement' then
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
                select source_kind into target_source
                from dependency_events where id = target_event_id;
                if target_source = 'verbal' then
                    raise exception 'verbal dependency events are append-only'
                        using errcode = '23514';
                end if;
                raise exception 'External Party statements are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

        create function reject_external_party_statement_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            if current_user <> 'corridor_statement_retirement' then
                if (tg_op = 'DELETE' and old.source_kind = 'verbal')
                   or (tg_op = 'UPDATE' and (
                       old.source_kind = 'verbal' or new.source_kind = 'verbal'
                   )) then
                    raise exception 'verbal dependency events are append-only'
                        using errcode = '23514';
                end if;
                raise exception 'External Party statements are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

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

        create function reject_external_party_statement_truncate()
        returns trigger
        language plpgsql
        as $$
        begin
            raise exception 'External Party statements are append-only'
                using errcode = '23514';
        end;
        $$;

        create trigger external_party_statement_events_are_immutable
        before update or delete on dependency_events
        for each row execute function reject_external_party_statement_mutation();

        create trigger external_party_statement_evidence_is_immutable
        before update or delete on evidence_links
        for each row execute function reject_external_party_statement_evidence_mutation();

        create trigger external_party_statement_events_reject_truncate
        before truncate on dependency_events
        for each statement execute function reject_external_party_statement_truncate();

        create trigger external_party_statement_scopes_reject_truncate
        before truncate on dependency_event_scopes
        for each statement execute function reject_external_party_statement_truncate();

        create trigger external_party_statement_timings_reject_truncate
        before truncate on dependency_event_timings
        for each statement execute function reject_external_party_statement_truncate();

        create trigger external_party_statement_evidence_reject_truncate
        before truncate on evidence_links
        for each statement execute function reject_external_party_statement_truncate();

        create trigger external_party_statement_scopes_are_immutable
        before update or delete on dependency_event_scopes
        for each row execute function reject_external_party_statement_child_mutation();

        create trigger external_party_statement_timings_are_immutable
        before update or delete on dependency_event_timings
        for each row execute function reject_external_party_statement_child_mutation();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    invalid_scope = bind.execute(
        sa.text(
            """
            select exists (
                select 1
                from dependency_events event
                left join dependency_event_scopes scope on scope.event_id = event.id
                group by event.id, event.scope_mode
                having event.scope_mode = 'unknown'
                    or count(scope.id) <> 1
                    or event.event_type = 'committed_date_change'
            )
            """
        )
    ).scalar()
    non_day_timing = bind.execute(
        sa.text(
            """
            select exists (
                select 1 from dependency_event_timings
                where kind = 'new' and precision <> 'day'
            )
            """
        )
    ).scalar()
    if invalid_scope or non_day_timing:
        raise RuntimeError(
            "cannot downgrade statement scope or timing without inventing a single exact Dependency date"
        )

    op.execute(
        """
        drop trigger dependency_event_scope_links_match_shape on dependency_event_scopes;
        drop trigger dependency_event_timings_match_statement on dependency_event_timings;
        drop trigger dependency_event_scope_shape_is_valid on dependency_events;
        drop trigger external_party_statement_evidence_reject_truncate on evidence_links;
        drop trigger external_party_statement_timings_reject_truncate on dependency_event_timings;
        drop trigger external_party_statement_scopes_reject_truncate on dependency_event_scopes;
        drop trigger external_party_statement_events_reject_truncate on dependency_events;
        drop trigger external_party_statement_evidence_is_immutable on evidence_links;
        drop trigger external_party_statement_events_are_immutable on dependency_events;
        drop trigger external_party_statement_timings_are_immutable on dependency_event_timings;
        drop trigger external_party_statement_scopes_are_immutable on dependency_event_scopes;
        drop function reject_external_party_statement_truncate();
        drop function reject_external_party_statement_evidence_mutation();
        drop function reject_external_party_statement_mutation();
        drop function reject_external_party_statement_child_mutation();
        drop function verify_dependency_event_scope_shape();
        drop trigger dependency_event_scope_link_is_valid on dependency_event_scopes;
        drop function validate_dependency_event_scope_link();
        drop function public.purge_external_party_statement_rows(bigint, text);
        revoke all privileges on all tables in schema public
            from corridor_statement_retirement;
        revoke all privileges on all sequences in schema public
            from corridor_statement_retirement;
        revoke usage on schema public from corridor_statement_retirement;

        create or replace function reject_verbal_dependency_event_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
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
    )
    op.add_column("dependency_events", sa.Column("dependency_id", sa.BigInteger()))
    op.add_column("dependency_events", sa.Column("committed_date", sa.Date()))
    op.execute(
        "alter table dependency_events disable trigger verbal_dependency_events_are_immutable"
    )
    op.execute(
        """
        update dependency_events event
        set dependency_id = scope.dependency_id,
            committed_date = (
                select timing.start_date
                from dependency_event_timings timing
                where timing.event_id = event.id and timing.kind = 'new'
            )
        from dependency_event_scopes scope
        where scope.event_id = event.id
        """
    )
    op.execute(
        "alter table dependency_events enable trigger verbal_dependency_events_are_immutable"
    )
    op.alter_column("dependency_events", "dependency_id", nullable=False)
    op.create_foreign_key(
        "dependency_events_dependency_id_fkey",
        "dependency_events",
        "dependencies",
        ["dependency_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_verbal_events_require_call_facts",
        "dependency_events",
        """
        source_kind <> 'verbal' or (
            event_type in ('commitment', 'slip')
            and event_date is not null
            and committed_date is not null
            and length(trim(stated_party)) > 0
            and length(trim(description)) > 0
            and length(trim(created_by)) > 0
        )
        """,
    )
    op.execute(
        """
        update evidence_links link
        set dependency_id = scope.dependency_id,
            satisfies_requirement = exists (
                select 1 from dependency_evidence_sufficiencies sufficiency
                where sufficiency.evidence_link_id = link.id
                  and sufficiency.dependency_id = scope.dependency_id
            )
        from dependency_event_scopes scope
        where link.event_id = scope.event_id
        """
    )
    op.drop_constraint(
        "ck_evidence_links_event_ownership", "evidence_links", type_="check"
    )
    op.drop_constraint(
        "fk_operative_support_evidence_link",
        "operative_support",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_operative_support_dependency_evidence",
        "operative_support",
        "evidence_links",
        ["dependency_id", "evidence_link_id"],
        ["dependency_id", "id"],
    )
    op.drop_table("dependency_evidence_sufficiencies")
    op.drop_table("dependency_event_timings")
    op.drop_table("dependency_event_scopes")
    op.drop_constraint("event_type", "dependency_events", type_="check")
    op.alter_column(
        "dependency_events",
        "event_type",
        existing_type=sa.String(length=32),
        type_=sa.String(length=13),
    )
    op.create_check_constraint(
        "event_type",
        "dependency_events",
        "event_type in ('commitment', 'response', 'slip', 'escalation', 'status_change', 'closure')",
    )
    op.drop_constraint("ck_dependency_events_timing_direction", "dependency_events", type_="check")
    op.drop_constraint("ck_dependency_events_scope_mode", "dependency_events", type_="check")
    op.drop_constraint("fk_dependency_events_stated_external_org", "dependency_events", type_="foreignkey")
    op.drop_constraint("fk_dependency_events_affected_external_org", "dependency_events", type_="foreignkey")
    op.drop_constraint("fk_dependency_events_project", "dependency_events", type_="foreignkey")
    op.drop_column("dependency_events", "timing_direction")
    op.drop_column("dependency_events", "scope_mode")
    op.drop_column("dependency_events", "stated_external_org_id")
    op.drop_column("dependency_events", "affected_external_org_id")
    op.drop_column("dependency_events", "project_id")
    op.alter_column("evidence_links", "dependency_id", nullable=False)
