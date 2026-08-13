"""Link an attributable External Party closure to exactly one Commitment.

Revision ID: e253a7c4d9e2
Revises: d253a7c4d9e2

Party identity is not enough to close a commitment: one External Party may
have several independent statements.  This link lets the work list stop a
past-due Derivation only when supported closure evidence names that lineage.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e253a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "d253a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dependency_events",
        sa.Column("closes_commitment_lineage_id", sa.BigInteger()),
    )
    op.create_foreign_key(
        "fk_dependency_events_closes_commitment_lineage",
        "dependency_events",
        "commitment_lineages",
        ["closes_commitment_lineage_id"],
        ["id"],
    )
    op.create_index(
        "ix_dependency_events_closes_commitment_lineage",
        "dependency_events",
        ["closes_commitment_lineage_id"],
    )
    op.execute(
        """
        create or replace function verify_dependency_event_scope_shape()
        returns trigger language plpgsql as $$
        declare
            target_event_id bigint; target_mode text; target_source text;
            target_event_date date; target_event_type text; link_count integer;
            new_precision text; previous_count integer;
        begin
            if tg_table_name = 'dependency_events' then
                target_event_id := case when tg_op = 'DELETE' then old.id else new.id end;
            elsif tg_table_name = 'dependency_event_timings' then
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
            else
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
            end if;
            select scope_mode, source_kind, event_date, event_type
              into target_mode, target_source, target_event_date, target_event_type
            from dependency_events where id = target_event_id;
            if target_mode is null then return null; end if;
            select count(*) into link_count from dependency_event_scopes where event_id = target_event_id;
            if target_mode = 'unknown' and link_count <> 0 then
                raise exception 'unknown statement scope has Dependency links' using errcode = '23514';
            end if;
            if target_mode in ('selected', 'all_active', 'carried_forward') and link_count = 0 then
                raise exception 'known statement scope has no Dependency links' using errcode = '23514';
            end if;
            if target_source = 'verbal' and target_event_type <> 'closure' then
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
            if target_source = 'verbal' and target_event_type = 'closure'
               and target_event_date is null then
                raise exception 'a Verbal closure requires its conversation date'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;

        create or replace function validate_commitment_closure_link()
        returns trigger language plpgsql as $$
        declare lineage_project_id bigint; lineage_party_id bigint;
        begin
            if new.closes_commitment_lineage_id is null then
                return new;
            end if;
            if new.event_type <> 'closure' then
                raise exception 'only an External Party closure may close a Commitment Lineage'
                    using errcode = '23514';
            end if;
            select lineage.project_id, statement.affected_external_org_id
              into lineage_project_id, lineage_party_id
            from commitment_lineages lineage
            join dependency_events statement
              on statement.commitment_lineage_id = lineage.id
            where lineage.id = new.closes_commitment_lineage_id
              and statement.event_type in ('commitment', 'committed_date_change')
              and statement.attribution_state = 'resolved'
              and statement.stated_external_org_id is not null
            order by statement.id desc limit 1;
            if lineage_project_id is null
               or lineage_project_id is distinct from new.project_id
               or lineage_party_id is distinct from new.affected_external_org_id
               or new.stated_external_org_id is distinct from lineage_party_id
               or new.attribution_state <> 'resolved' then
                raise exception 'closure must name an attributable Commitment from the same External Party and project'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;
        create trigger dependency_event_closure_link_is_valid
        before insert or update on dependency_events
        for each row execute function validate_commitment_closure_link();
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade work list: closure identity would be lost and "
        "party-level Commitment history could be misrepresented"
    )
