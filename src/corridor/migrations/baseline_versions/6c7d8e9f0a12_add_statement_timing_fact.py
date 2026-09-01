"""A statement_timing typed Fact and its document-less verbal home.

A Recorded Verbal Statement states its timing at day, month, or approximate
precision, and may state more than one (a change of promise states the previous
and the new).  The structured date types are single ISO days on a source row and
cannot carry that precision, so #451 adds a set-valued `statement_timing` Fact
whose members live in a typed `fact_statement_timings` satellite, mirroring the
`applies_to` and `dependency_event_timings` shapes (ADR-0074 decision 2).

A verbal Fact has no source Document (ADR-0033/0068): the recorder's words are
the source, already a document-less `recorded_verbal_statement` segment (stage
1b, migration 5b6c7d8e9f01).  This migration lets the Fact itself be
document-less the same way — `facts.document_id` and `extraction_run_id` become
nullable for the human-gated verbal Fact types only (`statement_wording`,
`statement_timing`, and a verbal `applies_to` scope), and a project-scoped Fact
Source foreign key keeps referential integrity when the document scope is null.
Every other Fact type stays document-bound exactly as before.  Applies To is
dual-use: a spreadsheet cell keeps its document; a verbal scope is document-less.

ADR-0074's illustrative satellite sketch named a single `iso_date`; that cannot
represent an approximate timing (no anchor date) or a month range, so the
satellite mirrors the released `dependency_event_timings` bounds instead
(`precision` with `start_date`/`end_date`), keeping the spine timing byte-exact
with the legacy timing it will dual-write beside in stage 2.

Revision ID: 6c7d8e9f0a12
Revises: 5b6c7d8e9f01
"""

from alembic import op


revision = "6c7d8e9f0a12"
down_revision = "5b6c7d8e9f01"
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
    """Add the statement_timing Fact type and its document-less verbal home."""

    text_types = _sql_values(TEXT_FACT_TYPES)
    date_types = _sql_values(DATE_FACT_TYPES)
    single_types = _sql_values((*TEXT_FACT_TYPES, *DATE_FACT_TYPES))
    satellite_types = _sql_values(SATELLITE_FACT_TYPES)
    op.execute(
        f"""
        -- A human-gated verbal Fact has no Document to bind to.  Let the verbal
        -- Fact types be document-less; every other type stays document- and
        -- run-bound, and statement_timing (verbal-only) must be document-less.
        -- Applies To is dual-use: document-bound for a spreadsheet cell,
        -- document-less for a Recorded Verbal Statement's scope.
        alter table facts
            alter column document_id drop not null,
            alter column extraction_run_id drop not null,
            add constraint ck_facts_source_binding check (
                (document_id is not null and extraction_run_id is not null
                 and fact_type <> 'statement_timing')
                or
                (document_id is null and extraction_run_id is null
                 and fact_type in
                     ('statement_wording', 'statement_timing', 'applies_to'))
            );

        alter table facts drop constraint ck_facts_type;
        alter table facts drop constraint ck_facts_subject;
        alter table facts drop constraint ck_facts_typed_value;

        alter table facts add constraint ck_facts_type check (
            fact_type in (
                {single_types}, {satellite_types},
                'statement_wording', 'statement_timing'
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
        );

        -- A project-scoped identity so a document-less Fact Source can prove its
        -- Fact and segment exist even when the document scope is null (the
        -- document-scoped foreign keys hold vacuously for a null document).
        alter table source_segments
            add constraint uq_source_segments_project_id unique (project_id, id);
        alter table fact_sources
            alter column document_id drop not null,
            add constraint fk_fact_sources_fact_project
                foreign key (project_id, fact_id) references facts(project_id, id),
            add constraint fk_fact_sources_segment_project
                foreign key (project_id, source_segment_id)
                references source_segments(project_id, id);

        create table fact_statement_timings (
            id bigserial primary key,
            project_id bigint not null references projects(id),
            fact_id bigint not null,
            timing_role varchar(16) not null,
            text text not null,
            precision varchar(32) not null,
            start_date date,
            end_date date,
            constraint uq_fact_statement_timing_role unique (fact_id, timing_role),
            constraint fk_fact_statement_timing_fact_scope
                foreign key (project_id, fact_id) references facts(project_id, id),
            constraint ck_fact_statement_timing_role
                check (timing_role in ('previous', 'new')),
            constraint ck_fact_statement_timing_precision
                check (precision in ('day', 'month', 'approximate')),
            constraint ck_fact_statement_timing_text check (length(trim(text)) > 0),
            constraint ck_fact_statement_timing_bounds check (
                (precision = 'day'
                    and start_date is not null and end_date = start_date)
                or (precision = 'month'
                    and start_date is not null and end_date is not null
                    and start_date = date_trunc('month', start_date::timestamp)::date
                    and end_date = (date_trunc('month', start_date::timestamp)
                        + interval '1 month - 1 day')::date)
                or (precision = 'approximate'
                    and start_date is null and end_date is null)
            )
        );
        create index ix_fact_statement_timings_project_id
            on fact_statement_timings(project_id);
        create index ix_fact_statement_timings_fact_id
            on fact_statement_timings(fact_id);
        create trigger trg_fact_statement_timings_append_only
            before update or delete on fact_statement_timings
            for each row
            execute function enforce_structured_fact_satellite_append_only();
        """
    )


def downgrade() -> None:
    """Class A verbal Fact history has no destructive migration path."""

    raise RuntimeError("statement timing fact migration downgrade is unsupported")
