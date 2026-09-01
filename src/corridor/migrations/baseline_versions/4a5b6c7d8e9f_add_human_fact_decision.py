"""A human-authored spine decision command; the guard accepts the human branch.

Revision ID: 4a5b6c7d8e9f
Revises: 3f4a5b6c7d8e
"""

from alembic import op


revision = "4a5b6c7d8e9f"
down_revision = "3f4a5b6c7d8e"
branch_labels = None
depends_on = None


HUMAN_COMMAND_TYPES = (
    "record_verbal_statement",
    "correct_statement_scope",
    "correct_statement_facts",
    "mark_do_not_add",
    "resolve_discrepancy",
    "designate_support",
)


def upgrade() -> None:
    """Let a Human Record Decision write the spine, behind the same guard (#451)."""

    command_list = ", ".join(f"'{name}'" for name in HUMAN_COMMAND_TYPES)
    op.execute(
        f"""
        -- The revision already models human_principal XOR released_policy
        -- (ADR-0071). The guard previously accepted only the released-policy
        -- branch; a Human Record Decision needs the human branch (ADR-0070).
        create or replace function enforce_fact_decision_write()
            returns trigger language plpgsql as $$
        declare valid_binding boolean;
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Fact decision requires the typed decision command'
                    using errcode='23514';
            end if;
            if tg_op = 'DELETE' then
                raise exception 'Fact decisions are append-preserving'
                    using errcode='23514';
            end if;
            if tg_op = 'UPDATE' then
                if new.id is distinct from old.id
                   or new.project_id is distinct from old.project_id
                   or new.fact_id is distinct from old.fact_id
                   or new.subject_key is distinct from old.subject_key
                   or new.fact_type is distinct from old.fact_type
                   or new.revision_id is distinct from old.revision_id
                   or new.decided_at is distinct from old.decided_at
                   or old.superseded_by is not null
                   or new.superseded_by is null then
                    raise exception 'Fact decision may only become superseded once'
                        using errcode='23514';
                end if;
                return new;
            end if;
            select exists (
                select 1 from facts
                join project_record_revisions
                  on project_record_revisions.id = new.revision_id
                where facts.id = new.fact_id
                  and facts.project_id = new.project_id
                  and facts.subject_key = new.subject_key
                  and facts.fact_type = new.fact_type
                  and project_record_revisions.project_id = new.project_id
                  and (
                      (project_record_revisions.released_policy is not null
                       and project_record_revisions.human_principal is null)
                      or
                      (project_record_revisions.human_principal is not null
                       and project_record_revisions.released_policy is null)
                  )
            ) into valid_binding;
            if not valid_binding then
                raise exception 'Fact decision binding is invalid' using errcode='23514';
            end if;
            return new;
        end; $$;

        -- One attributable human decision: append a revision (human_principal,
        -- no policy), supersede a named predecessor when correcting, and refuse
        -- a stale supersession for the set-valued types that no unique index
        -- guards (statement_wording, statement_timing, applies_to).
        create function record_human_fact_decision(
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
        grant execute on function record_human_fact_decision(
            bigint,bigint,text,varchar,varchar,text,varchar,bigint
        ) to public;
        """
    )


def downgrade() -> None:
    """Authority changes are forward-only; a downgrade is unsupported."""

    raise RuntimeError("human fact decision migration downgrade is unsupported")
