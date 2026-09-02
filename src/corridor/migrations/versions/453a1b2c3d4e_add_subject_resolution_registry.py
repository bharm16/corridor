"""Add exact subject resolution and human alias decisions.

Revision ID: 453a1b2c3d4e
Revises: 8fc4c747b2d9
"""

from alembic import op


revision = "453a1b2c3d4e"
down_revision = "7e1b2c3d4f50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create append-only exact lookup, candidate, decision, and ranking rows."""

    op.execute(
        """
        create table stated_by_people (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            display_name text not null,
            aliases text[] not null default '{}',
            email_normalized text,
            created_at timestamptz not null default now(),
            constraint ck_stated_by_people_display_name check (
                length(trim(display_name)) > 0
            ),
            constraint ck_stated_by_people_email check (
                email_normalized is null or (
                    length(trim(email_normalized)) > 0
                    and email_normalized = lower(trim(email_normalized))
                )
            )
        );
        create index ix_stated_by_people_project_id on stated_by_people(project_id);

        create table subject_resolution_attempts (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            source_document_id bigint not null,
            source_segment_id bigint not null,
            reference_kind varchar(48) not null,
            raw_reference text not null,
            normalized_reference text not null,
            expected_subject_type varchar(32) not null,
            usage varchar(32) not null,
            state varchar(24) not null,
            attention_reason varchar(64),
            rule_identity varchar(64) not null,
            resolved_external_org_id bigint references external_orgs(id),
            resolved_stated_by_person_id bigint references stated_by_people(id),
            resolved_dependency_id bigint references dependencies(id),
            resolved_document_id bigint references documents(id),
            content_sha256 varchar(64) not null,
            created_at timestamptz not null default now(),
            constraint uq_subject_resolution_content unique (content_sha256),
            constraint fk_subject_resolution_segment_scope foreign key (
                project_id, source_document_id, source_segment_id
            ) references source_segments(project_id, document_id, id),
            constraint ck_subject_resolution_reference_kind check (
                reference_kind in ('organization_name', 'email_sender',
                'email_domain', 'person_name', 'person_email',
                'source_identifier', 'activity_identifier', 'document_identifier')
            ),
            constraint ck_subject_resolution_expected_type check (
                expected_subject_type in ('external_org', 'person', 'constraint', 'document')
            ),
            constraint ck_subject_resolution_usage check (
                usage in ('identity', 'statement_speaker', 'affected_subject')
            ),
            constraint ck_subject_resolution_state check (
                state in ('resolved', 'unresolved', 'conflict', 'stale', 'actor_boundary')
            ),
            constraint ck_subject_resolution_reference check (
                length(trim(raw_reference)) > 0
                and length(trim(normalized_reference)) > 0
            ),
            constraint ck_subject_resolution_rule check (
                rule_identity = 'exact-registered-alias-v1'
            ),
            constraint ck_subject_resolution_sha256 check (
                content_sha256 ~ '^[0-9a-f]{64}$'
            ),
            constraint ck_subject_resolution_target check (
                (state = 'resolved' and num_nonnulls(
                    resolved_external_org_id, resolved_stated_by_person_id,
                    resolved_dependency_id, resolved_document_id
                ) = 1 and (
                    (expected_subject_type = 'external_org'
                        and resolved_external_org_id is not null)
                    or (expected_subject_type = 'person'
                        and resolved_stated_by_person_id is not null)
                    or (expected_subject_type = 'constraint'
                        and resolved_dependency_id is not null)
                    or (expected_subject_type = 'document'
                        and resolved_document_id is not null)
                ))
                or
                (state <> 'resolved' and num_nonnulls(
                    resolved_external_org_id, resolved_stated_by_person_id,
                    resolved_dependency_id, resolved_document_id
                ) = 0)
            ),
            constraint ck_subject_resolution_attention check (
                (state = 'resolved' and attention_reason is null)
                or (state <> 'resolved' and length(trim(attention_reason)) > 0)
            )
        );
        create index ix_subject_resolution_attempts_project_id
            on subject_resolution_attempts(project_id);
        create index ix_subject_resolution_attempts_source_segment_id
            on subject_resolution_attempts(source_segment_id);

        create table subject_resolution_candidates (
            id bigserial primary key,
            attempt_id bigint not null references subject_resolution_attempts(id),
            subject_type varchar(32) not null,
            subject_key varchar(96) not null,
            external_org_id bigint references external_orgs(id),
            stated_by_person_id bigint references stated_by_people(id),
            dependency_id bigint references dependencies(id),
            document_id bigint references documents(id),
            display_name text not null,
            candidate_state varchar(16) not null,
            match_source varchar(64) not null,
            constraint uq_subject_resolution_candidate_scope unique (attempt_id, id),
            constraint uq_subject_resolution_candidate unique (attempt_id, subject_key),
            constraint ck_subject_resolution_candidate_type check (
                subject_type in ('external_org', 'person', 'constraint', 'document')
            ),
            constraint ck_subject_resolution_candidate_state check (
                candidate_state in ('active', 'stale')
            ),
            constraint ck_subject_resolution_candidate_target check (
                num_nonnulls(external_org_id, stated_by_person_id,
                    dependency_id, document_id) = 1
                and (
                    (subject_type = 'external_org' and external_org_id is not null)
                    or (subject_type = 'person' and stated_by_person_id is not null)
                    or (subject_type = 'constraint' and dependency_id is not null)
                    or (subject_type = 'document' and document_id is not null)
                )
            ),
            constraint ck_subject_resolution_candidate_match_source check (
                match_source in ('registered_alias', 'human_alias_decision')
            ),
            constraint ck_subject_resolution_candidate_text check (
                length(trim(subject_key)) > 0 and length(trim(display_name)) > 0
            )
        );
        create index ix_subject_resolution_candidates_attempt_id
            on subject_resolution_candidates(attempt_id);

        create table subject_resolution_decisions (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            attempt_id bigint not null references subject_resolution_attempts(id),
            revision_id bigint not null references project_record_revisions(id),
            source_document_id bigint not null,
            source_segment_id bigint not null,
            reference_kind varchar(48) not null,
            raw_reference text not null,
            normalized_reference text not null,
            decision_kind varchar(48) not null,
            subject_type varchar(32) not null,
            external_org_id bigint references external_orgs(id),
            stated_by_person_id bigint references stated_by_people(id),
            dependency_id bigint references dependencies(id),
            document_id bigint references documents(id),
            recorded_by varchar(128) not null,
            created_at timestamptz not null default now(),
            constraint uq_subject_resolution_decision_attempt unique (attempt_id),
            constraint uq_subject_resolution_decision_revision unique (revision_id),
            constraint uq_subject_resolution_registered_alias unique (
                project_id, reference_kind, normalized_reference
            ),
            constraint fk_subject_resolution_decision_segment_scope foreign key (
                project_id, source_document_id, source_segment_id
            ) references source_segments(project_id, document_id, id),
            constraint ck_subject_resolution_decision_kind check (
                decision_kind = 'human_alias_registration'
            ),
            constraint ck_subject_resolution_decision_reference_kind check (
                reference_kind in ('organization_name', 'email_sender',
                'email_domain', 'person_name', 'person_email',
                'source_identifier', 'activity_identifier', 'document_identifier')
            ),
            constraint ck_subject_resolution_decision_type check (
                subject_type in ('external_org', 'person', 'constraint', 'document')
            ),
            constraint ck_subject_resolution_decision_target check (
                num_nonnulls(external_org_id, stated_by_person_id,
                    dependency_id, document_id) = 1
                and (
                    (subject_type = 'external_org' and external_org_id is not null)
                    or (subject_type = 'person' and stated_by_person_id is not null)
                    or (subject_type = 'constraint' and dependency_id is not null)
                    or (subject_type = 'document' and document_id is not null)
                )
            ),
            constraint ck_subject_resolution_decision_actor check (
                length(trim(recorded_by)) > 0
                and length(trim(normalized_reference)) > 0
            )
        );
        create index ix_subject_resolution_decisions_project_id
            on subject_resolution_decisions(project_id);
        create index ix_subject_resolution_decisions_attempt_id
            on subject_resolution_decisions(attempt_id);
        create index ix_subject_resolution_decisions_revision_id
            on subject_resolution_decisions(revision_id);

        create table subject_candidate_suggestions (
            id bigserial primary key,
            attempt_id bigint not null references subject_resolution_attempts(id),
            candidate_id bigint not null references subject_resolution_candidates(id),
            rank integer not null,
            model varchar(64) not null,
            prompt_version varchar(64) not null,
            created_at timestamptz not null default now(),
            constraint uq_subject_candidate_suggestion_rank unique (attempt_id, rank),
            constraint uq_subject_candidate_suggestion_candidate unique (
                attempt_id, candidate_id
            ),
            constraint fk_subject_candidate_suggestion_scope foreign key (
                attempt_id, candidate_id
            ) references subject_resolution_candidates(attempt_id, id),
            constraint ck_subject_candidate_suggestion_rank check (rank > 0),
            constraint ck_subject_candidate_suggestion_model check (
                length(trim(model)) > 0 and length(trim(prompt_version)) > 0
            )
        );
        create index ix_subject_candidate_suggestions_attempt_id
            on subject_candidate_suggestions(attempt_id);

        create function enforce_subject_resolution_append_only() returns trigger
        language plpgsql as $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'Subject resolution registry rows are append-only'
                    using errcode='23514';
            end if;
            return new;
        end; $$;
        create trigger trg_stated_by_people_append_only
            before update or delete on stated_by_people
            for each row execute function enforce_subject_resolution_append_only();
        create trigger trg_subject_resolution_attempts_append_only
            before update or delete on subject_resolution_attempts
            for each row execute function enforce_subject_resolution_append_only();
        create trigger trg_subject_resolution_candidates_append_only
            before update or delete on subject_resolution_candidates
            for each row execute function enforce_subject_resolution_append_only();
        create trigger trg_subject_candidate_suggestions_append_only
            before update or delete on subject_candidate_suggestions
            for each row execute function enforce_subject_resolution_append_only();
        revoke update, delete, truncate on stated_by_people from public;
        revoke update, delete, truncate on subject_resolution_attempts from public;
        revoke update, delete, truncate on subject_resolution_candidates from public;
        revoke update, delete, truncate on subject_candidate_suggestions from public;

        create function enforce_subject_resolution_decision_write() returns trigger
        language plpgsql as $$
        declare valid_binding boolean;
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Subject alias decision requires the typed decision command'
                    using errcode='23514';
            end if;
            if tg_op <> 'INSERT' then
                raise exception 'Subject alias decisions are append-only'
                    using errcode='23514';
            end if;
            select exists (
                select 1
                from subject_resolution_attempts attempts
                join project_record_revisions revisions
                  on revisions.id = new.revision_id
                where attempts.id = new.attempt_id
                  and attempts.project_id = new.project_id
                  and attempts.source_document_id = new.source_document_id
                  and attempts.source_segment_id = new.source_segment_id
                  and attempts.reference_kind = new.reference_kind
                  and attempts.raw_reference = new.raw_reference
                  and attempts.normalized_reference = new.normalized_reference
                  and attempts.expected_subject_type = new.subject_type
                  and attempts.state not in ('resolved', 'actor_boundary')
                  and revisions.project_id = new.project_id
                  and revisions.command_type = 'register_subject_alias'
                  and revisions.human_principal = new.recorded_by
                  and revisions.released_policy is null
            ) into valid_binding;
            if not valid_binding then
                raise exception 'Subject alias decision binding is invalid'
                    using errcode='23514';
            end if;
            return new;
        end; $$;
        create trigger trg_subject_resolution_decisions_guard
            before insert or update or delete on subject_resolution_decisions
            for each row execute function enforce_subject_resolution_decision_write();

        create function record_subject_alias_decision(
            p_project_id bigint,
            p_attempt_id bigint,
            p_subject_type varchar,
            p_subject_id bigint,
            p_human_principal varchar,
            p_idempotency_key varchar
        ) returns jsonb language plpgsql security definer set search_path = public as $$
        declare
            source_attempt subject_resolution_attempts%rowtype;
            existing_revision bigint;
            existing_decision bigint;
            existing_subject_type varchar;
            existing_subject_id bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_decision bigint;
            target_is_active boolean;
        begin
            if length(trim(p_human_principal)) = 0
               or length(trim(p_idempotency_key)) = 0 then
                raise exception 'Subject alias decision requires human and idempotency identity'
                    using errcode='23514';
            end if;
            select * into source_attempt from subject_resolution_attempts
             where id = p_attempt_id and project_id = p_project_id;
            if source_attempt.id is null
               or source_attempt.state in ('resolved', 'actor_boundary')
               or source_attempt.expected_subject_type <> p_subject_type then
                raise exception 'Subject alias decision attempt is ineligible'
                    using errcode='23514';
            end if;
            if p_subject_type = 'external_org' then
                select exists (
                    select 1 from external_orgs where id = p_subject_id
                ) into target_is_active;
            elsif p_subject_type = 'person' then
                select exists (
                    select 1 from stated_by_people people
                    where people.id = p_subject_id
                      and people.project_id = p_project_id
                ) into target_is_active;
            elsif p_subject_type = 'constraint' then
                select exists (
                    select 1 from dependencies
                    where id = p_subject_id and project_id = p_project_id
                      and dismissed_at is null
                ) into target_is_active;
            elsif p_subject_type = 'document' then
                select exists (
                    select 1 from documents
                    where id = p_subject_id and project_id = p_project_id
                      and superseded_by is null
                ) into target_is_active;
            else
                target_is_active := false;
            end if;
            if not target_is_active then
                raise exception 'Subject alias decision target is absent, stale, or out of scope'
                    using errcode='23514';
            end if;

            select revisions.id into existing_revision
              from project_record_revisions revisions
             where revisions.project_id = p_project_id
               and revisions.idempotency_key = p_idempotency_key;
            if existing_revision is not null then
                select decisions.id, decisions.subject_type,
                    case decisions.subject_type
                        when 'external_org' then decisions.external_org_id
                        when 'person' then decisions.stated_by_person_id
                        when 'constraint' then decisions.dependency_id
                        when 'document' then decisions.document_id
                    end
                  into existing_decision, existing_subject_type, existing_subject_id
                  from subject_resolution_decisions decisions
                 where decisions.revision_id = existing_revision;
                if existing_decision is null
                   or existing_subject_type <> p_subject_type
                   or existing_subject_id <> p_subject_id then
                    raise exception 'Subject alias decision key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', existing_decision,
                    'created', false
                );
            end if;

            select max(id) into predecessor_revision from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, 'register_subject_alias',
                p_human_principal, null, p_idempotency_key
            ) returning id into new_revision;

            insert into subject_resolution_decisions (
                project_id, attempt_id, revision_id, source_document_id,
                source_segment_id, reference_kind, raw_reference,
                normalized_reference, decision_kind, subject_type,
                external_org_id, stated_by_person_id, dependency_id, document_id,
                recorded_by
            ) values (
                p_project_id, source_attempt.id, new_revision,
                source_attempt.source_document_id, source_attempt.source_segment_id,
                source_attempt.reference_kind, source_attempt.raw_reference,
                source_attempt.normalized_reference, 'human_alias_registration',
                p_subject_type,
                case when p_subject_type = 'external_org' then p_subject_id end,
                case when p_subject_type = 'person' then p_subject_id end,
                case when p_subject_type = 'constraint' then p_subject_id end,
                case when p_subject_type = 'document' then p_subject_id end,
                p_human_principal
            ) returning id into new_decision;
            return jsonb_build_object(
                'revision_id', new_revision,
                'decision_id', new_decision,
                'created', true
            );
        end; $$;

        alter table subject_resolution_decisions owner to corridor_fact_decision_writer;
        alter sequence subject_resolution_decisions_id_seq owner to corridor_fact_decision_writer;
        alter function record_subject_alias_decision(bigint,bigint,varchar,bigint,varchar,varchar)
            owner to corridor_fact_decision_writer;
        revoke insert, update, delete, truncate on subject_resolution_decisions from public;
        grant select on subject_resolution_decisions to public;
        grant select on subject_resolution_attempts, external_orgs,
            stated_by_people, dependencies, documents
            to corridor_fact_decision_writer;
        grant execute on function record_subject_alias_decision(
            bigint,bigint,varchar,bigint,varchar,varchar
        ) to public;
        """
    )


def downgrade() -> None:
    """Class A identity decisions have no destructive migration path."""

    raise RuntimeError("subject resolution migration downgrade is unsupported")
