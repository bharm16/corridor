"""Add the coordinate_statement human command for Cited statement inclusion.

Including a Cited statement from Meeting Notes is a distinct human action from
capturing a Recorded Verbal Statement (ADR-0074): reusing the verbal command
would make the Audit Trail falsely claim a verbal capture. Both commands record
the same typed inclusion decision; only the command name and source provenance
differ. This extends the command allow-list in ``record_human_fact_decision``.

Revision ID: 7d8e9f0a1b23
Revises: 6c7d8e9f0a12
"""

from alembic import op


revision = "7d8e9f0a1b23"
down_revision = "6c7d8e9f0a12"
branch_labels = None
depends_on = None


HUMAN_COMMAND_TYPES = (
    "record_verbal_statement",
    "coordinate_statement",
    "correct_statement_scope",
    "correct_statement_facts",
    "mark_do_not_add",
    "resolve_discrepancy",
    "designate_support",
)


def upgrade() -> None:
    """Accept coordinate_statement as a Human Record Decision command (#451)."""

    command_list = ", ".join(f"'{name}'" for name in HUMAN_COMMAND_TYPES)
    op.execute(
        f"""
        create or replace function record_human_fact_decision(
            p_project_id bigint,
            p_fact_id bigint,
            p_subject_key text,
            p_fact_type varchar,
            p_command_type varchar,
            p_human_principal text,
            p_idempotency_key varchar,
            p_expected_predecessor bigint
        ) returns jsonb language plpgsql security definer set search_path = public as $$
        declare
            existing_revision bigint;
            existing_fact bigint;
            live_predecessor bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_decision bigint;
        begin
            if p_human_principal is null or length(trim(p_human_principal)) = 0 then
                raise exception 'Human Record Decision requires an attributed principal'
                    using errcode='23514';
            end if;
            if p_command_type not in ({command_list}) then
                raise exception 'unrecognized human record decision command'
                    using errcode='23514';
            end if;
            if not exists (
                select 1 from facts
                where facts.id = p_fact_id and facts.project_id = p_project_id
                  and facts.subject_key = p_subject_key
                  and facts.fact_type = p_fact_type
            ) then
                raise exception 'Human Record Decision names a fact that does not exist'
                    using errcode='23514';
            end if;
            select id into existing_revision from project_record_revisions
             where project_id = p_project_id and idempotency_key = p_idempotency_key;
            if existing_revision is not null then
                select fact_id into existing_fact from fact_decisions
                 where revision_id = existing_revision;
                if existing_fact is null or existing_fact <> p_fact_id then
                    raise exception 'Human Record Decision key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id',
                    (select id from fact_decisions where revision_id = existing_revision),
                    'created', false
                );
            end if;
            if p_expected_predecessor is not null then
                select id into live_predecessor from fact_decisions
                 where id = p_expected_predecessor and project_id = p_project_id
                   and superseded_by is null;
                if live_predecessor is null then
                    raise exception 'Human Record Decision predecessor is stale'
                        using errcode='23514';
                end if;
            end if;
            select max(id) into predecessor_revision from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, p_command_type,
                p_human_principal, null, p_idempotency_key
            ) returning id into new_revision;
            new_decision := nextval('fact_decisions_id_seq');
            if p_expected_predecessor is not null then
                update fact_decisions set superseded_by = new_decision
                 where id = p_expected_predecessor;
            end if;
            insert into fact_decisions (
                id, project_id, fact_id, subject_key, fact_type, revision_id, superseded_by
            ) values (
                new_decision, p_project_id, p_fact_id, p_subject_key,
                p_fact_type, new_revision, null
            );
            return jsonb_build_object(
                'revision_id', new_revision,
                'decision_id', new_decision,
                'created', true
            );
        end; $$;

        alter function record_human_fact_decision(
            bigint,bigint,text,varchar,varchar,text,varchar,bigint
        ) owner to corridor_fact_decision_writer;
        """
    )


def downgrade() -> None:
    """Command allow-list changes are forward-only; a downgrade is unsupported."""

    raise RuntimeError("coordinate_statement command migration downgrade is unsupported")
