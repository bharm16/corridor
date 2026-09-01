"""Decision dispositions, same-fact compensation, and support designation.

Stage 3 of #451 puts the remaining Human Record Decisions on the spine
(ADR-0074).  Three of them cannot be expressed by the existing shape:

- Do Not Add is a statement-level record disposition, not a fact value.  The
  decision itself now says what the Project Record does with its fact — a
  `disposition` column (`include`, `do_not_add`, `restore`) — so readers
  branch on the decision, never on the revision's command name.  An active
  `do_not_add` decision on a statement's wording anchor suppresses that
  subject's Project Record facts in `current_project_record`.
- A compensating decision acts "against the same fact" (restore after Do Not
  Add, resolving a support designation).  One-decision-per-fact therefore
  becomes one EFFECTIVE decision per fact: `uq_fact_decision_fact` is now
  partial on `superseded_by is null`, keeping the invariant that matters
  while letting an append-only chain re-decide the same fact.
- Supporting Documentation in Use is a relationship fact between one Project
  Record subject and one immutable document revision
  (`supporting_documentation_in_use`, value in `document_value_id`,
  subject_kind `record_subject`).  It is human-designated and document-less:
  the registered document row is the identity, so there is no segment to
  replay.

`restore_do_not_add` and `resolve_support` join the command allow-list:
restoring a Do Not Add is a distinct human action from marking one, and
resolving a support designation is distinct from designating one — reusing
the forward command would make the Audit Trail falsely claim a second act of
the same kind (the reasoning that gave Cited inclusion its own
`coordinate_statement` command).

Revision ID: 8e9f0a1b2c34
Revises: 7d8e9f0a1b23
"""

from alembic import op


revision = "8e9f0a1b2c34"
down_revision = "7d8e9f0a1b23"
branch_labels = None
depends_on = None


TEXT_FACT_TYPES = (
    "utility_id",
    "external_org",
    "external_org_contact",
    "utility_type",
    "utility_subtype",
    "utility_function",
    "operational_status",
    "size",
    "material",
    "oh_ug",
    "row_placement",
    "orientation",
    "baseline",
    "station_from",
    "station_to",
    "offset_from",
    "offset_to",
    "sue_level",
    "conflict_description",
    "resolution_strategy",
    "notes",
    "alignment",
    "location_start",
    "location_end",
    "offset_side",
    "potential_conflict",
    "data_source",
    "marked_resolution",
)
DATE_FACT_TYPES = ("committed_date", "action_due_date", "need_date")
SATELLITE_FACT_TYPES = ("applies_to", "closure_result")

HUMAN_COMMAND_TYPES = (
    "record_verbal_statement",
    "coordinate_statement",
    "correct_statement_scope",
    "correct_statement_facts",
    "mark_do_not_add",
    "restore_do_not_add",
    "resolve_discrepancy",
    "designate_support",
    "resolve_support",
)


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """Add decision dispositions and the support designation fact type."""

    text_types = _sql_values(TEXT_FACT_TYPES)
    date_types = _sql_values(DATE_FACT_TYPES)
    single_types = _sql_values((*TEXT_FACT_TYPES, *DATE_FACT_TYPES))
    satellite_types = _sql_values(SATELLITE_FACT_TYPES)
    command_list = _sql_values(HUMAN_COMMAND_TYPES)
    op.execute(
        f"""
        -- The relationship fact type: one Project Record subject, one
        -- immutable document revision, no source segment to replay.
        alter table facts drop constraint ck_facts_type;
        alter table facts drop constraint ck_facts_subject;
        alter table facts drop constraint ck_facts_source_binding;
        alter table facts drop constraint ck_facts_typed_value;

        alter table facts add constraint ck_facts_type check (
            fact_type in (
                {single_types}, {satellite_types},
                'statement_wording', 'statement_timing',
                'supporting_documentation_in_use'
            )
        );
        alter table facts add constraint ck_facts_subject check (
            (fact_type in ({single_types})
             and subject_kind = 'source_row' and length(trim(subject_key)) > 0)
            or
            (fact_type in ({satellite_types})
             and subject_kind = 'source_row' and length(trim(subject_key)) > 0)
            or
            (fact_type in
                 ('statement_wording', 'statement_timing', 'applies_to')
             and subject_kind = 'statement_candidate'
             and length(trim(subject_key)) > 0)
            or
            (fact_type = 'supporting_documentation_in_use'
             and subject_kind = 'record_subject'
             and length(trim(subject_key)) > 0)
        );
        alter table facts add constraint ck_facts_source_binding check (
            (document_id is not null and extraction_run_id is not null
             and fact_type not in
                 ('statement_timing', 'supporting_documentation_in_use'))
            or
            (document_id is null and extraction_run_id is null
             and fact_type in
                 ('statement_wording', 'statement_timing', 'applies_to',
                  'supporting_documentation_in_use'))
        );
        alter table facts add constraint ck_facts_typed_value check (
            (fact_type in ({text_types})
             and text_value is not null and length(trim(text_value)) > 0
             and date_value is null and date_range_start is null
             and date_range_end is null
             and (fact_type = 'external_org' or external_org_value_id is null)
             and document_value_id is null
             and transformation = 'trim_cell_text_v1')
            or
            (fact_type in ({date_types})
             and text_value is null and date_value is not null
             and date_range_start is null and date_range_end is null
             and external_org_value_id is null and document_value_id is null
             and transformation = 'iso_date_cell_v1')
            or
            (fact_type = 'applies_to'
             and text_value is null and date_value is null
             and date_range_start is null and date_range_end is null
             and external_org_value_id is null and document_value_id is null
             and transformation = 'structured_reference_set_v1')
            or
            (fact_type = 'closure_result'
             and text_value is null and date_value is null
             and date_range_start is null and date_range_end is null
             and external_org_value_id is null and document_value_id is null
             and transformation = 'typed_closure_result_v1')
            or
            (fact_type = 'statement_wording'
             and text_value is not null and length(trim(text_value)) > 0
             and date_value is null and date_range_start is null
             and date_range_end is null and external_org_value_id is null
             and document_value_id is null
             and transformation = 'exact_prose_span_v1')
            or
            (fact_type = 'statement_timing'
             and text_value is null and date_value is null
             and date_range_start is null and date_range_end is null
             and external_org_value_id is null and document_value_id is null
             and transformation = 'typed_statement_timing_v1')
            or
            (fact_type = 'supporting_documentation_in_use'
             and text_value is null and date_value is null
             and date_range_start is null and date_range_end is null
             and external_org_value_id is null
             and document_value_id is not null
             and transformation = 'supporting_document_revision_v1')
        );

        -- The relationship fact's identity is the registered document row in
        -- document_value_id; there is no segment to replay, so the deferred
        -- value-source requirement exempts it (mirroring the released
        -- contract's empty required_roles).
        create or replace function require_fact_value_source()
            returns trigger language plpgsql as $$
        begin
            if new.fact_type = 'supporting_documentation_in_use' then
                return null;
            end if;
            if not exists (
                select 1 from fact_sources
                where fact_id = new.id and role = 'value_source'
            ) then
                raise exception 'source-backed Fact requires a value source';
            end if;
            return null;
        end; $$;

        -- The decision says what the Project Record does with its fact.
        alter table fact_decisions
            add column disposition varchar(32) not null default 'include',
            add constraint ck_fact_decision_disposition check (
                disposition in ('include', 'do_not_add', 'restore')
            );

        -- One EFFECTIVE decision per fact: compensating decisions re-decide
        -- the same fact after their predecessor is superseded.  The released
        -- table declared the unique inline, so it carries the auto-assigned
        -- name rather than the model's.
        alter table fact_decisions drop constraint fact_decisions_fact_id_key;
        create unique index uq_fact_decision_fact
            on fact_decisions (fact_id) where superseded_by is null;

        -- The guard keeps every recorded column immutable; disposition joins
        -- that list.  Supersession stays the only permitted update.
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
                   or new.disposition is distinct from old.disposition
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

        drop function record_human_fact_decision(
            bigint,bigint,text,varchar,varchar,text,varchar,bigint
        );
        create function record_human_fact_decision(
            p_project_id bigint,
            p_fact_id bigint,
            p_subject_key text,
            p_fact_type varchar,
            p_command_type varchar,
            p_disposition varchar,
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
            if p_disposition not in ('include', 'do_not_add', 'restore') then
                raise exception 'unrecognized human record decision disposition'
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
                id, project_id, fact_id, subject_key, fact_type,
                revision_id, disposition, superseded_by
            ) values (
                new_decision, p_project_id, p_fact_id, p_subject_key,
                p_fact_type, new_revision, p_disposition, null
            );
            return jsonb_build_object(
                'revision_id', new_revision,
                'decision_id', new_decision,
                'created', true
            );
        end; $$;

        alter function record_human_fact_decision(
            bigint,bigint,text,varchar,varchar,varchar,text,varchar,bigint
        ) owner to corridor_fact_decision_writer;

        -- Only including decisions project, and an active Do Not Add on a
        -- statement's wording anchor suppresses that subject's facts
        -- (ADR-0074 stage 3).
        create or replace view current_project_record as
        select
            decisions.project_id,
            proposals.candidate_id,
            candidates.merged_into as dependency_id,
            decisions.subject_key,
            decisions.fact_type,
            facts.text_value,
            facts.date_value,
            facts.date_range_start,
            facts.date_range_end,
            facts.external_org_value_id,
            facts.document_value_id,
            decisions.id as decision_id,
            facts.id as fact_id,
            decisions.revision_id,
            decisions.decided_at
        from fact_decisions decisions
        join facts on facts.id = decisions.fact_id
        left join extracted_proposals proposals
          on proposals.project_id = facts.project_id
         and proposals.document_id = facts.document_id
         and proposals.extraction_run_id = facts.extraction_run_id
         and proposals.subject_key = facts.subject_key
        left join candidates on candidates.id = proposals.candidate_id
        where decisions.superseded_by is null
          and decisions.disposition = 'include'
          and not exists (
              select 1 from fact_decisions suppression
              where suppression.project_id = decisions.project_id
                and suppression.subject_key = decisions.subject_key
                and suppression.fact_type = 'statement_wording'
                and suppression.superseded_by is null
                and suppression.disposition = 'do_not_add'
          );
        """
    )


def downgrade() -> None:
    """Recorded dispositions are Class A history; no destructive path exists."""

    raise RuntimeError(
        "decision disposition migration downgrade is unsupported"
    )
