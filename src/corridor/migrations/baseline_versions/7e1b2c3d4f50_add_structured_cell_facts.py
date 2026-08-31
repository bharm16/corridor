"""Add the complete controlled structured-cell Fact vocabulary.

The Stationing slice proved the envelope but left every other verified matrix
cell in mutable Candidate JSON, so current/as-of readers still saw only two
fields and downstream code kept copying the rest.  This linear successor keeps
the one-envelope design, expands its exact per-type checks, and adds satellites
only for the two values that are genuinely structured rather than scalar.  A
generic JSON value or a table per type were rejected by ADR-0067 because they
would respectively lose reference integrity or multiply the append/query seam.

Revision ID: 7e1b2c3d4f50
Revises: 8fc4c747b2d9
"""

from alembic import op


revision = "7e1b2c3d4f50"
down_revision = "8fc4c747b2d9"
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


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """Expand the typed envelope without weakening per-type value checks."""

    text_types = _sql_values(TEXT_FACT_TYPES)
    date_types = _sql_values(DATE_FACT_TYPES)
    single_types = _sql_values((*TEXT_FACT_TYPES, *DATE_FACT_TYPES))
    satellite_types = _sql_values(SATELLITE_FACT_TYPES)
    automatic_types = _sql_values((*TEXT_FACT_TYPES, *DATE_FACT_TYPES, "applies_to"))
    op.execute(
        f"""
        alter table facts drop constraint ck_facts_type;
        alter table facts drop constraint ck_facts_subject;
        alter table facts drop constraint ck_facts_typed_value;

        alter table facts add constraint ck_facts_type check (
            fact_type in ({single_types}, {satellite_types}, 'statement_wording')
        );
        alter table facts add constraint ck_facts_subject check (
            (fact_type in ({single_types})
             and subject_kind = 'source_row' and length(trim(subject_key)) > 0)
            or
            (fact_type in ({satellite_types})
             and subject_kind = 'source_row' and length(trim(subject_key)) > 0)
            or
            (fact_type = 'statement_wording'
             and subject_kind = 'statement_candidate'
             and length(trim(subject_key)) > 0)
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
        );

        alter table facts add constraint uq_facts_project_id unique (project_id, id);
        alter table dependencies add constraint uq_dependencies_project_id
            unique (project_id, id);

        create table fact_applies_to (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            fact_id bigint not null,
            dependency_id bigint not null,
            ordinal integer not null,
            constraint uq_fact_applies_to_member unique (fact_id, dependency_id),
            constraint uq_fact_applies_to_ordinal unique (fact_id, ordinal),
            constraint fk_fact_applies_to_fact_scope
                foreign key (project_id, fact_id) references facts(project_id, id),
            constraint fk_fact_applies_to_dependency_scope
                foreign key (project_id, dependency_id)
                references dependencies(project_id, id),
            constraint ck_fact_applies_to_ordinal check (ordinal > 0)
        );
        create index ix_fact_applies_to_project_id on fact_applies_to(project_id);
        create index ix_fact_applies_to_fact_id on fact_applies_to(fact_id);
        create index ix_fact_applies_to_dependency_id on fact_applies_to(dependency_id);

        create table fact_closure_results (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            fact_id bigint not null,
            closure_kind varchar(48) not null,
            successor_dependency_id bigint,
            constraint uq_fact_closure_result_fact unique (fact_id),
            constraint fk_fact_closure_result_fact_scope
                foreign key (project_id, fact_id) references facts(project_id, id),
            constraint fk_fact_closure_result_successor_scope
                foreign key (project_id, successor_dependency_id)
                references dependencies(project_id, id),
            constraint ck_fact_closure_result_kind check (
                closure_kind in (
                    'source_marked_resolved', 'constraint_closed',
                    'constraint_remains_open'
                )
            )
        );
        create index ix_fact_closure_results_project_id
            on fact_closure_results(project_id);
        create index ix_fact_closure_results_fact_id on fact_closure_results(fact_id);

        create table fact_closure_sources (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            document_id bigint not null,
            fact_id bigint not null,
            source_segment_id bigint not null,
            ordinal integer not null,
            constraint uq_fact_closure_source_segment
                unique (fact_id, source_segment_id),
            constraint uq_fact_closure_source_ordinal unique (fact_id, ordinal),
            constraint fk_fact_closure_source_fact_scope
                foreign key (project_id, document_id, fact_id)
                references facts(project_id, document_id, id),
            constraint fk_fact_closure_source_segment_scope
                foreign key (project_id, document_id, source_segment_id)
                references source_segments(project_id, document_id, id),
            constraint ck_fact_closure_source_ordinal check (ordinal > 0)
        );
        create index ix_fact_closure_sources_project_id
            on fact_closure_sources(project_id);
        create index ix_fact_closure_sources_document_id
            on fact_closure_sources(document_id);
        create index ix_fact_closure_sources_fact_id on fact_closure_sources(fact_id);
        create index ix_fact_closure_sources_source_segment_id
            on fact_closure_sources(source_segment_id);

        create function enforce_structured_fact_satellite_append_only()
        returns trigger language plpgsql as $$
        begin
            raise exception 'structured Fact satellites are append-only';
        end; $$;
        create trigger trg_fact_applies_to_append_only
            before update or delete on fact_applies_to
            for each row execute function enforce_structured_fact_satellite_append_only();
        create trigger trg_fact_closure_results_append_only
            before update or delete on fact_closure_results
            for each row execute function enforce_structured_fact_satellite_append_only();
        create trigger trg_fact_closure_sources_append_only
            before update or delete on fact_closure_sources
            for each row execute function enforce_structured_fact_satellite_append_only();

        drop index uq_fact_decision_effective;
        create unique index uq_fact_decision_effective
            on fact_decisions(project_id, subject_key, fact_type)
            where superseded_by is null and fact_type in ({single_types});

        create function include_structured_cell_fact_decision(
            p_project_id bigint,
            p_fact_id bigint,
            p_subject_key text,
            p_fact_type varchar,
            p_idempotency_key varchar,
            p_policy varchar
        ) returns jsonb language plpgsql security definer set search_path = public as $$
        declare
            existing_revision bigint;
            existing_fact bigint;
            current_decision bigint;
            current_fact bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_decision bigint;
            eligible boolean;
        begin
            if p_policy <> 'structured-cell-record-inclusion-v1'
               or p_fact_type not in ({automatic_types}) then
                raise exception 'unrecognized structured-cell inclusion policy'
                    using errcode='23514';
            end if;
            select exists (
                select 1 from facts
                join active_extraction_runs
                  on active_extraction_runs.document_id = facts.document_id
                 and active_extraction_runs.extraction_run_id = facts.extraction_run_id
                join extracted_proposal_facts
                  on extracted_proposal_facts.fact_id = facts.id
                join extracted_proposals
                  on extracted_proposals.id = extracted_proposal_facts.proposal_id
                join candidates on candidates.id = extracted_proposals.candidate_id
                where facts.id = p_fact_id
                  and facts.project_id = p_project_id
                  and facts.subject_key = p_subject_key
                  and facts.fact_type = p_fact_type
                  and candidates.state in ('accepted', 'merged')
                  and candidates.merged_into is not null
                  and exists (
                      select 1 from fact_sources
                      join source_segments
                        on source_segments.id = fact_sources.source_segment_id
                      where fact_sources.fact_id = facts.id
                        and fact_sources.role = 'value_source'
                        and source_segments.kind = 'spreadsheet_cell'
                  )
                  and not exists (
                      select 1 from fact_sources
                      join source_segments
                        on source_segments.id = fact_sources.source_segment_id
                      where fact_sources.fact_id = facts.id
                        and fact_sources.role = 'value_source'
                        and source_segments.kind <> 'spreadsheet_cell'
                  )
            ) into eligible;
            if not eligible then
                raise exception 'Fact is not eligible for released structured-cell inclusion'
                    using errcode='23514';
            end if;
            select id into existing_revision from project_record_revisions
             where project_id = p_project_id and idempotency_key = p_idempotency_key;
            if existing_revision is not null then
                select id, fact_id into current_decision, existing_fact from fact_decisions
                 where revision_id = existing_revision;
                if current_decision is null or existing_fact <> p_fact_id then
                    raise exception 'Record Inclusion key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', current_decision,
                    'created', false
                );
            end if;
            select id, fact_id into current_decision, current_fact from fact_decisions
             where project_id = p_project_id and subject_key = p_subject_key
               and fact_type = p_fact_type and superseded_by is null;
            if current_decision is not null and current_fact = p_fact_id then
                select revision_id into existing_revision from fact_decisions
                 where id = current_decision;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', current_decision,
                    'created', false
                );
            end if;
            select max(id) into predecessor_revision from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, 'include_structured_cell_fact',
                null, p_policy, p_idempotency_key
            ) returning id into new_revision;
            new_decision := nextval('fact_decisions_id_seq');
            if current_decision is not null then
                update fact_decisions set superseded_by = new_decision
                 where id = current_decision;
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

        alter function include_structured_cell_fact_decision(
            bigint,bigint,text,varchar,varchar,varchar
        ) owner to corridor_fact_decision_writer;
        grant execute on function include_structured_cell_fact_decision(
            bigint,bigint,text,varchar,varchar,varchar
        ) to public;
        drop function include_stationing_fact_decision(
            bigint,bigint,text,varchar,varchar,varchar
        );
        """
    )


def downgrade() -> None:
    """Class A structured Fact history has no destructive downgrade path."""

    raise RuntimeError("structured-cell Fact migration downgrade is unsupported")
