"""Move historical event citations and roles to event-owned provenance.

Revision ID: f227b9e4d3c2
Revises: e226a8d4f3c2

Every existing EvidenceLink keeps its id and source columns.  The migration
adds the event-owned identity row beside it and gives each historical
sufficiency or publication designation the exact scope link on which it was
originally made.  Nothing is copied to a later scope decision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f227b9e4d3c2"
down_revision: Union[str, Sequence[str], None] = "e226a8d4f3c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        insert into dependency_event_evidence
            (evidence_link_id, event_id, recorded_by)
        select evidence.id, evidence.event_id, event.created_by
        from evidence_links evidence
        join dependency_events event on event.id = evidence.event_id
        where evidence.event_id is not null
        on conflict (evidence_link_id) do nothing;

        do $$
        begin
            if exists (
                select 1
                from dependency_evidence_sufficiencies sufficiency
                join evidence_links evidence
                  on evidence.id = sufficiency.evidence_link_id
                left join dependency_event_scopes scope
                  on scope.event_id = evidence.event_id
                 and scope.dependency_id = sufficiency.dependency_id
                where evidence.event_id is not null
                group by sufficiency.id
                having count(scope.id) <> 1
            ) then
                raise exception 'event Evidence sufficiency has ambiguous historical Dependency scope'
                    using errcode = '23514';
            end if;
            if exists (
                select 1
                from operative_support support
                join evidence_links evidence
                  on evidence.id = support.evidence_link_id
                left join dependency_event_scopes scope
                  on scope.event_id = evidence.event_id
                 and scope.dependency_id = support.dependency_id
                where evidence.event_id is not null
                group by support.id
                having count(scope.id) <> 1
            ) then
                raise exception 'event Evidence publication support has ambiguous historical Dependency scope'
                    using errcode = '23514';
            end if;
        end;
        $$;

        update dependency_evidence_sufficiencies sufficiency
        set scope_link_id = scope.id
        from evidence_links evidence
        join dependency_event_scopes scope
          on scope.event_id = evidence.event_id
        where evidence.id = sufficiency.evidence_link_id
          and evidence.event_id is not null
          and scope.dependency_id = sufficiency.dependency_id;

        update operative_support support
        set scope_link_id = scope.id
        from evidence_links evidence
        join dependency_event_scopes scope
          on scope.event_id = evidence.event_id
        where evidence.id = support.evidence_link_id
          and evidence.event_id is not null
          and scope.dependency_id = support.dependency_id;

        do $$
        begin
            if exists (
                select 1
                from dependency_evidence_sufficiencies sufficiency
                join dependency_event_evidence evidence
                  on evidence.evidence_link_id = sufficiency.evidence_link_id
                where sufficiency.scope_link_id is null
            ) then
                raise exception 'event Evidence sufficiency has no preserved Dependency scope link'
                    using errcode = '23514';
            end if;
            if exists (
                select 1
                from operative_support support
                join dependency_event_evidence evidence
                  on evidence.evidence_link_id = support.evidence_link_id
                where support.scope_link_id is null
            ) then
                raise exception 'event Evidence publication support has no preserved Dependency scope link'
                    using errcode = '23514';
            end if;
        end;
        $$;
        """
    )
    op.alter_column(
        "dependency_evidence_sufficiencies", "scope_link_id", nullable=False
    )


def downgrade() -> None:
    op.alter_column("dependency_evidence_sufficiencies", "scope_link_id", nullable=True)
    op.execute(
        """
        alter table dependency_evidence_sufficiencies
            disable trigger dependency_evidence_sufficiency_scope_is_valid;
        alter table operative_support
            disable trigger operative_event_evidence_scope_is_valid;
        alter table dependency_event_evidence
            disable trigger dependency_event_evidence_is_immutable;

        update dependency_evidence_sufficiencies set scope_link_id = null;
        update operative_support set scope_link_id = null
        where evidence_link_id in (
            select evidence_link_id from dependency_event_evidence
        );
        delete from dependency_event_evidence;

        alter table dependency_event_evidence
            enable trigger dependency_event_evidence_is_immutable;
        alter table operative_support
            enable trigger operative_event_evidence_scope_is_valid;
        alter table dependency_evidence_sufficiencies
            enable trigger dependency_evidence_sufficiency_scope_is_valid;
        """
    )
