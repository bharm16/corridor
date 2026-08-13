"""Preserve both attributable External Party identities on a closure link.

Revision ID: f253a7c4d9e2
Revises: e253a7c4d9e2

An affected External Party and the party who spoke can differ.  The original
closure-link guard compared both to the affected party, which would reject a
valid closure of an already attributable Commitment.  This successor repairs
the applied guard without changing its historical revision.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f253a7c4d9e2"
down_revision: Union[str, Sequence[str], None] = "e253a7c4d9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        create or replace function validate_commitment_closure_link()
        returns trigger language plpgsql as $$
        declare
            lineage_project_id bigint;
            lineage_affected_party_id bigint;
            lineage_stated_party_id bigint;
        begin
            if new.closes_commitment_lineage_id is null then
                return new;
            end if;
            if new.event_type <> 'closure' then
                raise exception 'only an External Party closure may close a Commitment Lineage'
                    using errcode = '23514';
            end if;
            select lineage.project_id,
                   statement.affected_external_org_id,
                   statement.stated_external_org_id
              into lineage_project_id,
                   lineage_affected_party_id,
                   lineage_stated_party_id
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
               or lineage_affected_party_id is distinct from new.affected_external_org_id
               or lineage_stated_party_id is distinct from new.stated_external_org_id
               or new.attribution_state <> 'resolved' then
                raise exception 'closure must name an attributable Commitment from the same External Party and project'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "cannot downgrade statement contract: closure identity would be lost "
        "and party-level Commitment history could be misrepresented"
    )
