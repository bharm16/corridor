"""Expand attributable statement storage beside the legacy event shape.

Revision ID: b223f5a4c3d2
Revises: a217e4f3a2b1

The existing event representation stays operational.  This revision adds an
explicit attribution state, enforces timing cardinality, and captures every
pre-expansion event row in an immutable receipt for transitional downgrade.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b223f5a4c3d2"
down_revision: Union[str, Sequence[str], None] = "a217e4f3a2b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dependency_event_migration_receipts",
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("dependency_events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("original_event", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        "insert into dependency_event_migration_receipts "
        "(event_id, original_event) "
        "select id, to_jsonb(dependency_events) from dependency_events"
    )
    op.add_column(
        "dependency_events", sa.Column("attribution_state", sa.String(length=16))
    )
    # a217 was amended after some databases had already applied it.  Those
    # databases have its shape but not the later External Party immutability
    # guard, so this revision must not assume that trigger exists.  The guard
    # is converged below before this revision commits.
    op.execute(
        """
        do $$
        begin
            if exists (
                select 1 from pg_trigger
                where tgrelid = 'dependency_events'::regclass
                  and tgname = 'external_party_statement_events_are_immutable'
            ) then
                alter table dependency_events disable trigger
                    external_party_statement_events_are_immutable;
            end if;
        end;
        $$;
        """
    )
    op.execute(
        "alter table dependency_events disable trigger "
        "verbal_dependency_events_are_immutable"
    )
    op.execute(
        "update dependency_events set attribution_state = "
        "case when stated_external_org_id is null then 'unresolved' else 'resolved' end"
    )
    op.execute("set constraints all immediate")
    op.execute(
        "alter table dependency_events enable trigger "
        "verbal_dependency_events_are_immutable"
    )
    op.execute("set constraints all deferred")
    op.alter_column(
        "dependency_events",
        "attribution_state",
        nullable=False,
        server_default="unresolved",
    )
    op.create_check_constraint(
        "ck_dependency_events_attribution",
        "dependency_events",
        "(attribution_state = 'resolved' and stated_external_org_id is not null) "
        "or (attribution_state = 'unresolved' and stated_external_org_id is null)",
    )
    # Re-establish the latest a217 immutability contract here as a repair
    # migration.  It makes databases that applied an earlier a217 source
    # converge with fresh installations before later revisions rely on these
    # functions and trigger names.
    op.execute(
        """
        create or replace function reject_external_party_statement_child_mutation()
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

        create or replace function reject_external_party_statement_mutation()
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

        create or replace function reject_external_party_statement_evidence_mutation()
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

        create or replace function reject_external_party_statement_truncate()
        returns trigger
        language plpgsql
        as $$
        begin
            raise exception 'External Party statements are append-only'
                using errcode = '23514';
        end;
        $$;

        drop trigger if exists external_party_statement_events_are_immutable
            on dependency_events;
        drop trigger if exists external_party_statement_events_reject_truncate
            on dependency_events;
        drop trigger if exists external_party_statement_scopes_are_immutable
            on dependency_event_scopes;
        drop trigger if exists external_party_statement_scopes_reject_truncate
            on dependency_event_scopes;
        drop trigger if exists external_party_statement_timings_are_immutable
            on dependency_event_timings;
        drop trigger if exists external_party_statement_timings_reject_truncate
            on dependency_event_timings;
        drop trigger if exists external_party_statement_evidence_is_immutable
            on evidence_links;
        drop trigger if exists external_party_statement_evidence_reject_truncate
            on evidence_links;
        drop trigger if exists external_party_statement_evidence_updates_are_immutable
            on evidence_links;
        drop trigger if exists external_party_statement_evidence_deletes_are_immutable
            on evidence_links;

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
    op.execute(
        """
        create function verify_dependency_event_timing_cardinality()
        returns trigger
        language plpgsql
        as $$
        declare
            target_event_id bigint;
            target_event_type text;
            new_count integer;
            previous_count integer;
        begin
            if tg_table_name = 'dependency_events' then
                target_event_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
            end if;
            select event_type into target_event_type
            from dependency_events where id = target_event_id;
            if target_event_type is null then
                return null;
            end if;
            select
                count(*) filter (where kind = 'new'),
                count(*) filter (where kind = 'previous')
            into new_count, previous_count
            from dependency_event_timings where event_id = target_event_id;
            if target_event_type = 'commitment'
               and (new_count <> 1 or previous_count <> 0) then
                raise exception 'Commitment requires exactly one new timing'
                    using errcode = '23514';
            end if;
            if target_event_type = 'committed_date_change'
               and (new_count <> 1 or previous_count <> 1) then
                raise exception 'Committed Date Change requires previous and new timings'
                    using errcode = '23514';
            end if;
            if target_event_type = 'closure'
               and (new_count <> 0 or previous_count <> 0) then
                raise exception 'closure cannot carry a commitment timing'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;

        create constraint trigger dependency_event_timing_cardinality_is_valid
        after insert or update or delete on dependency_events
        deferrable initially deferred
        for each row execute function verify_dependency_event_timing_cardinality();

        create constraint trigger dependency_event_timing_rows_match_event
        after insert or update or delete on dependency_event_timings
        deferrable initially deferred
        for each row execute function verify_dependency_event_timing_cardinality();

        create function reject_dependency_event_migration_receipt_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            if current_user <> 'corridor_statement_retirement'
               and not (tg_op = 'DELETE' and pg_trigger_depth() > 1) then
                raise exception 'statement migration receipts are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

        create trigger dependency_event_migration_receipts_are_immutable
        before update or delete on dependency_event_migration_receipts
        for each row execute function reject_dependency_event_migration_receipt_mutation();

        create trigger dependency_event_migration_receipts_reject_truncate
        before truncate on dependency_event_migration_receipts
        for each statement execute function reject_external_party_statement_truncate();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        drop trigger dependency_event_timing_rows_match_event
            on dependency_event_timings;
        drop trigger dependency_event_timing_cardinality_is_valid
            on dependency_events;
        drop function verify_dependency_event_timing_cardinality();
        drop trigger dependency_event_migration_receipts_reject_truncate
            on dependency_event_migration_receipts;
        drop trigger dependency_event_migration_receipts_are_immutable
            on dependency_event_migration_receipts;
        drop function reject_dependency_event_migration_receipt_mutation();
        """
    )
    op.drop_constraint(
        "ck_dependency_events_attribution", "dependency_events", type_="check"
    )
    op.drop_column("dependency_events", "attribution_state")
    op.drop_table("dependency_event_migration_receipts")
