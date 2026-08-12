"""Separate event-owned source Evidence from Dependency-specific roles.

Revision ID: d225a7c4e3f2
Revises: c224a6b4d3e2

The stable EvidenceLink identity continues to carry a document, page, quote,
verification result, and every downstream foreign key.  This additive step
records which event owns that source identity and anchors future readiness and
publication designations to the exact scope link on which a human acted.  It
intentionally does not rewrite historical Evidence; #227 performs that move.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d225a7c4e3f2"
down_revision: Union[str, Sequence[str], None] = "c224a6b4d3e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dependency_event_evidence",
        sa.Column(
            "evidence_link_id",
            sa.BigInteger(),
            sa.ForeignKey("evidence_links.id"),
            primary_key=True,
        ),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("dependency_events.id"),
            nullable=False,
        ),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_dependency_event_evidence_actor",
        ),
    )
    op.add_column(
        "dependency_evidence_sufficiencies",
        sa.Column("scope_link_id", sa.BigInteger()),
    )
    op.add_column("operative_support", sa.Column("scope_link_id", sa.BigInteger()))
    op.create_foreign_key(
        "fk_dependency_evidence_sufficiencies_scope_link",
        "dependency_evidence_sufficiencies",
        "dependency_event_scopes",
        ["scope_link_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_operative_support_scope_link",
        "operative_support",
        "dependency_event_scopes",
        ["scope_link_id"],
        ["id"],
    )
    op.drop_constraint(
        "dependency_evidence_sufficien_dependency_id_evidence_link_i_key",
        "dependency_evidence_sufficiencies",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_dependency_evidence_sufficiency_scope_evidence",
        "dependency_evidence_sufficiencies",
        ["scope_link_id", "evidence_link_id"],
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

        create function reject_dependency_event_evidence_mutation()
        returns trigger
        language plpgsql
        as $$
        begin
            if current_user <> 'corridor_statement_retirement' then
                raise exception 'External Party statement Evidence is append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;

        create trigger dependency_event_evidence_is_immutable
        before update or delete on dependency_event_evidence
        for each row execute function reject_dependency_event_evidence_mutation();

        create trigger dependency_event_evidence_reject_truncate
        before truncate on dependency_event_evidence
        for each statement execute function reject_external_party_statement_truncate();

        grant select, delete on table dependency_event_evidence
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


def downgrade() -> None:
    bind = op.get_bind()
    has_new_evidence = bind.execute(
        sa.text("select exists (select 1 from dependency_event_evidence)")
    ).scalar()
    if has_new_evidence:
        raise RuntimeError(
            "cannot downgrade event Evidence after recording event-owned citations"
        )
    op.execute(
        """
        drop trigger dependency_event_evidence_reject_truncate on dependency_event_evidence;
        drop trigger dependency_event_evidence_is_immutable on dependency_event_evidence;
        drop function reject_dependency_event_evidence_mutation();
        drop trigger operative_event_evidence_scope_is_valid on operative_support;
        drop function validate_operative_event_evidence_scope_role();
        drop trigger dependency_evidence_sufficiency_scope_is_valid on dependency_evidence_sufficiencies;
        drop function validate_dependency_event_evidence_scope_role();
        drop trigger dependency_event_evidence_is_valid on dependency_event_evidence;
        drop function validate_dependency_event_evidence();
        drop trigger evidence_links_receive_event_evidence on evidence_links;
        drop function create_dependency_event_evidence();
        """
    )
    op.drop_constraint(
        "uq_dependency_evidence_sufficiency_scope_evidence",
        "dependency_evidence_sufficiencies",
        type_="unique",
    )
    op.create_unique_constraint(
        "dependency_evidence_sufficien_dependency_id_evidence_link_i_key",
        "dependency_evidence_sufficiencies",
        ["dependency_id", "evidence_link_id"],
    )
    op.drop_constraint(
        "fk_operative_support_scope_link", "operative_support", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_dependency_evidence_sufficiencies_scope_link",
        "dependency_evidence_sufficiencies",
        type_="foreignkey",
    )
    op.drop_column("operative_support", "scope_link_id")
    op.drop_column("dependency_evidence_sufficiencies", "scope_link_id")
    op.drop_table("dependency_event_evidence")
