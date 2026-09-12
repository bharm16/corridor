"""The ADR-0081 spine: segments, facts, assessments, and accepted revisions.

A Source Segment is exact source text with a typed locator and a digest; a
Source Fact is what the source says, captured, and never the accepted record.
The record changes only through a Project Record Revision, and only Adopt
Baseline establishes the first accepted state in bulk. This ordering is the
correction ADR-0075 made to the legacy path, where an extractor's conclusion
was written straight into the record and the source claim beneath it was lost
(ADR-0001).
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.fact_types import (
    EFFECTIVE_SINGLE_VALUE_FACT_TYPES,
    PDF_MARKED_RESOLUTION_TRANSFORMATION,
    PDF_TEXT_TRANSFORMATION,
    SINGLE_VALUED_FACT_TYPES,
    STRUCTURED_DATE_FACT_TYPES,
    STRUCTURED_SATELLITE_FACT_TYPES,
    STRUCTURED_TEXT_FACT_TYPES,
)
from corridor.models.base import Base


__all__ = [
    "BASELINE_FORMAT_KINDS",
    "BASELINE_SOURCE_KINDS",
    "BaselineAdoption",
    "BaselineFormat",
    "BaselineFormatManifest",
    "BaselineFormatObject",
    "BaselineSource",
    "BaselineSourceRow",
    "Fact",
    "FactAppliesTo",
    "FactClosureResult",
    "FactClosureSource",
    "FactDecision",
    "FactDisposition",
    "FactSource",
    "FactStatementTiming",
    "MinutesCapture",
    "MinutesQuestionDisposition",
    "OnboardingAct",
    "OnboardingGrant",
    "OnboardingGrantEvent",
    "OnboardingPreview",
    "ProjectRecordRevision",
    "SUPPORT_ASSESSMENT_EVIDENCE_ROLES",
    "SUPPORT_ASSESSMENT_OUTCOMES",
    "SourceFactAppendReceipt",
    "SourceSegment",
    "SupportAssessment",
    "SupportAssessmentSource",
]


class SourceSegment(Base):
    """One immutable, addressable piece of an exact Document rendition.

    A populated workbook cell carries mandatory ``sheet_name`` and
    ``cell_range`` columns.  A prose span carries a page and exact character
    bounds. ``kind`` identifies the enforced locator shape; callers never
    interpret an untyped JSON object.
    """

    __tablename__ = "source_segments"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "document_id", "id", name="uq_source_segments_scope_id"
        ),
        UniqueConstraint(
            "project_id", "id", name="uq_source_segments_project_id"
        ),
        Index(
            "uq_source_segments_document_kind_ordinal",
            "document_id", "kind", "ordinal", unique=True,
            postgresql_where=text("reading_sha256 is null"),
        ),
        UniqueConstraint(
            "document_id", "reading_sha256", "kind", "ordinal",
            name="uq_source_segments_reading_ordinal",
        ),
        UniqueConstraint(
            "document_id",
            "kind",
            "sheet_name",
            "cell_range",
            name="uq_source_segments_spreadsheet_locator",
        ),
        Index(
            "uq_source_segments_prose_locator",
            "document_id", "kind", "page_no", "start_offset", "end_offset",
            unique=True, postgresql_where=text("reading_sha256 is null"),
        ),
        UniqueConstraint(
            "document_id", "reading_sha256", "page_no", "span_stream",
            "start_offset", "end_offset", name="uq_source_segments_native_span",
        ),
        UniqueConstraint(
            "document_id", "reading_sha256", "page_no", "table_index",
            "cell_row", "cell_column", name="uq_source_segments_pdf_cell",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_source_segments_document_scope",
        ),
        CheckConstraint(
            "kind in ('spreadsheet_cell', 'prose_span', 'recorded_verbal_statement', 'pdf_span', 'pdf_cell', 'email_span')",
            name="ck_source_segments_kind",
        ),
        CheckConstraint(
            "length(exact_text) > 0", name="ck_source_segments_exact_text"
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_segments_content_sha256",
        ),
        CheckConstraint("ordinal > 0", name="ck_source_segments_ordinal"),
        CheckConstraint(
            "(kind = 'spreadsheet_cell' and document_id is not null "
            "and recorded_verbal_origin_id is null and length(sheet_name) > 0 and "
            "cell_range ~ '^[A-Z]+[1-9][0-9]*$' and page_no is null and "
            "start_offset is null and end_offset is null) or "
            "(kind = 'prose_span' and document_id is not null "
            "and recorded_verbal_origin_id is null and sheet_name is null "
            "and cell_range is null "
            "and page_no > 0 and start_offset >= 0 and end_offset > start_offset) or "
            "(kind = 'pdf_span' and document_id is not null "
            "and recorded_verbal_origin_id is null and sheet_name is null "
            "and cell_range is null and page_no > 0 and start_offset >= 0 "
            "and end_offset > start_offset and span_stream in ('page', 'clipped') "
            "and table_index is null and cell_row is null and cell_column is null "
            "and row_span is null and column_span is null) or "
            "(kind = 'pdf_cell' and document_id is not null "
            "and recorded_verbal_origin_id is null and sheet_name is null "
            "and cell_range is null and page_no > 0 and start_offset is null "
            "and end_offset is null and span_stream is null "
            "and table_index >= 0 and cell_row >= 0 and cell_column >= 0 "
            "and row_span > 0 and column_span > 0) or "
            "(kind = 'recorded_verbal_statement' and document_id is null "
            "and recorded_verbal_origin_id is not null and sheet_name is null "
            "and cell_range is null and page_no is null and start_offset is null "
            "and end_offset is null) or "
            "(kind = 'email_span' and document_id is not null and recorded_verbal_origin_id is null "
            "and sheet_name is null and cell_range is null and page_no is null "
            "and start_offset >= 0 and end_offset > start_offset)",
            name="ck_source_segments_locator",
        ),
        CheckConstraint(
            "(kind in ('pdf_span', 'pdf_cell') "
            "and rendition_sha256 is not null and rendition_sha256 ~ '^[0-9a-f]{64}$' "
            "and reading_sha256 is not null and reading_sha256 ~ '^[0-9a-f]{64}$' "
            "and reader_identity is not null and jsonb_typeof(reader_identity) = 'object' "
            "and location_json is not null and jsonb_typeof(location_json) = 'object') or "
            "(kind not in ('pdf_span', 'pdf_cell') and rendition_sha256 is null "
            "and reading_sha256 is null and reader_identity is null and location_json is null "
            "and span_stream is null and table_index is null and cell_row is null "
            "and cell_column is null and row_span is null and column_span is null) or "
            "(kind = 'email_span' and rendition_sha256 is null and reading_sha256 is null "
            "and reader_identity is null and location_json is not null "
            "and location_json ->> 'scheme' = 'email-mime-v1' "
            "and span_stream is null and table_index is null and cell_row is null "
            "and cell_column is null and row_span is null and column_span is null)",
            name="ck_source_segments_reading",
        ),
        CheckConstraint(
            "kind <> 'email_span' or (start_offset is not null and end_offset is not null "
            "and location_json is not null and "
            "coalesce(location_json ->> 'scheme' = 'email-mime-v1', false) "
            "and coalesce(location_json ->> 'section' in "
            "('header', 'body', 'quoted_history', 'signature', 'draft', 'html', 'attachment'), false) "
            "and coalesce(jsonb_typeof(location_json -> 'part_path') = 'array', false))",
            name="ck_source_segments_email_complete",
        ),
        CheckConstraint(
            "kind not in ('pdf_span', 'pdf_cell') or (page_no is not null and "
            "((kind = 'pdf_span' and span_stream is not null and start_offset is not null "
            "and end_offset is not null) or (kind = 'pdf_cell' and table_index is not null "
            "and cell_row is not null and cell_column is not null "
            "and row_span is not null and column_span is not null)))",
            name="ck_source_segments_native_complete",
        ),
        ForeignKeyConstraint(
            ["project_id", "recorded_verbal_origin_id"],
            ["recorded_verbal_origins.project_id", "recorded_verbal_origins.id"],
            name="fk_source_segments_recorded_verbal_origin_scope",
        ),
        Index(
            "uq_source_segments_recorded_verbal_origin",
            "recorded_verbal_origin_id",
            unique=True,
            postgresql_where=text("kind = 'recorded_verbal_statement'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # The spine-native origin of a Recorded Verbal Statement (#512, ADR-0081
    # stage 1).  The legacy statement is reachable only through
    # ``recorded_verbal_origin_statements``, never from this table.
    recorded_verbal_origin_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    exact_text: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    sheet_name: Mapped[str | None] = mapped_column(Text)
    cell_range: Mapped[str | None] = mapped_column(String(32))
    page_no: Mapped[int | None] = mapped_column(Integer)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    rendition_sha256: Mapped[str | None] = mapped_column(String(64))
    reading_sha256: Mapped[str | None] = mapped_column(String(64))
    reader_identity: Mapped[dict | None] = mapped_column(JSONB)
    location_json: Mapped[dict | None] = mapped_column(JSONB)
    span_stream: Mapped[str | None] = mapped_column(String(16))
    table_index: Mapped[int | None] = mapped_column(Integer)
    cell_row: Mapped[int | None] = mapped_column(Integer)
    cell_column: Mapped[int | None] = mapped_column(Integer)
    row_span: Mapped[int | None] = mapped_column(Integer)
    column_span: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


_STRUCTURED_TEXT_FACT_TYPES_SQL = ", ".join(
    f"'{value}'" for value in STRUCTURED_TEXT_FACT_TYPES
)
_STRUCTURED_DATE_FACT_TYPES_SQL = ", ".join(
    f"'{value}'" for value in STRUCTURED_DATE_FACT_TYPES
)
_SINGLE_VALUED_FACT_TYPES_SQL = ", ".join(
    f"'{value}'" for value in SINGLE_VALUED_FACT_TYPES
)
_EFFECTIVE_SINGLE_VALUE_FACT_TYPES_SQL = ", ".join(
    f"'{value}'" for value in EFFECTIVE_SINGLE_VALUE_FACT_TYPES
)
_STRUCTURED_SATELLITE_FACT_TYPES_SQL = ", ".join(
    f"'{value}'" for value in STRUCTURED_SATELLITE_FACT_TYPES
)


class Fact(Base):
    """One typed source observation, pending any Project Record decision."""

    __tablename__ = "facts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "document_id", "id", name="uq_facts_scope_id"
        ),
        UniqueConstraint("project_id", "id", name="uq_facts_project_id"),
        UniqueConstraint(
            "project_id",
            "document_id",
            "extraction_run_id",
            "id",
            name="uq_facts_run_scope_id",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_facts_document_scope",
        ),
        ForeignKeyConstraint(
            ["document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
            name="fk_facts_extraction_run_document",
        ),
        CheckConstraint(
            f"fact_type in ({_SINGLE_VALUED_FACT_TYPES_SQL}, "
            f"{_STRUCTURED_SATELLITE_FACT_TYPES_SQL}, "
            "'statement_wording', 'statement_timing', "
            "'supporting_documentation_in_use', 'email_header', 'email_attachment')",
            name="ck_facts_type",
        ),
        CheckConstraint(
            f"(fact_type in ({_SINGLE_VALUED_FACT_TYPES_SQL}) "
            "and subject_kind = 'source_row' and length(trim(subject_key)) > 0) "
            f"or (fact_type in ({_STRUCTURED_SATELLITE_FACT_TYPES_SQL}) "
            "and subject_kind = 'source_row' and length(trim(subject_key)) > 0) "
            "or (fact_type in "
            "('statement_wording', 'statement_timing', 'applies_to', 'closure_result') "
            "and subject_kind = 'statement_candidate' "
            "and length(trim(subject_key)) > 0) "
            "or (fact_type = 'supporting_documentation_in_use' "
            "and subject_kind = 'record_subject' "
            "and length(trim(subject_key)) > 0) or "
            "(fact_type in ('email_header', 'email_attachment') and subject_kind = 'email_message' "
            "and length(trim(subject_key)) > 0)",
            name="ck_facts_subject",
        ),
        CheckConstraint(
            "(document_id is not null and extraction_run_id is not null "
            "and fact_type not in "
            "('supporting_documentation_in_use')) "
            "or (document_id is null and extraction_run_id is null "
            "and fact_type in "
            "('statement_wording', 'statement_timing', 'applies_to', "
            "'supporting_documentation_in_use'))",
            name="ck_facts_source_binding",
        ),
        CheckConstraint(
            f"(fact_type in ({_STRUCTURED_TEXT_FACT_TYPES_SQL}) and text_value is not null "
            "and length(trim(text_value)) > 0 and date_value is null "
            "and date_range_start is null and date_range_end is null "
            "and (fact_type = 'external_org' or external_org_value_id is null) "
            "and document_value_id is null "
            f"and (transformation in ('trim_cell_text_v1', '{PDF_TEXT_TRANSFORMATION}') "
            "or (fact_type = 'resolution_strategy' "
            f"and transformation = '{PDF_MARKED_RESOLUTION_TRANSFORMATION}'))) or "
            f"(fact_type in ({_STRUCTURED_DATE_FACT_TYPES_SQL}) and text_value is null "
            "and date_value is not null and date_range_start is null "
            "and date_range_end is null and external_org_value_id is null "
            "and document_value_id is null and transformation = 'iso_date_cell_v1') or "
            "(fact_type = 'applies_to' and text_value is null "
            "and date_value is null and date_range_start is null "
            "and date_range_end is null and external_org_value_id is null "
            "and document_value_id is null "
            "and transformation = 'structured_reference_set_v1') or "
            "(fact_type = 'closure_result' and text_value is null "
            "and date_value is null and date_range_start is null "
            "and date_range_end is null and external_org_value_id is null "
            "and document_value_id is null "
            "and transformation = 'typed_closure_result_v1') or "
            "(fact_type = 'statement_wording' and text_value is not null "
            "and length(trim(text_value)) > 0 and date_value is null "
            "and date_range_start is null and date_range_end is null "
            "and external_org_value_id is null and document_value_id is null "
            "and transformation = 'exact_prose_span_v1') or "
            "(fact_type = 'statement_timing' and text_value is null "
            "and date_value is null and date_range_start is null "
            "and date_range_end is null and external_org_value_id is null "
            "and document_value_id is null "
            "and transformation = 'typed_statement_timing_v1') or "
            "(fact_type = 'supporting_documentation_in_use' "
            "and text_value is null and date_value is null "
            "and date_range_start is null and date_range_end is null "
            "and external_org_value_id is null "
            "and document_value_id is not null "
            "and transformation = 'supporting_document_revision_v1') or "
            "(fact_type in ('email_header', 'email_attachment') and text_value is not null "
            "and length(text_value) > 0 and date_value is null and date_range_start is null "
            "and date_range_end is null and external_org_value_id is null "
            "and document_value_id is null and transformation = 'exact_email_part_v1')",
            name="ck_facts_typed_value",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0", name="ck_facts_recorded_by"
        ),
        CheckConstraint(
            "content_sha256 is null or content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_facts_content_sha256",
        ),
        # The Fact identity digest is permanent state, not a command's
        # precondition (#457): ``append_fact`` refuses a Fact without one and
        # returns the row that already carries it, and the constraint is what
        # makes a second copy unrepresentable rather than unlikely.
        UniqueConstraint("content_sha256", name="uq_facts_content_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    # Null only for a document-less human-gated verbal Fact (statement_wording
    # or statement_timing); every other Fact type stays document- and run-bound
    # (ADR-0033/0074, ck_facts_source_binding).
    document_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    extraction_run_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    fact_type: Mapped[str] = mapped_column(String(64))
    subject_kind: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(Text)
    text_value: Mapped[str | None] = mapped_column(Text)
    date_value: Mapped[date | None] = mapped_column(Date)
    date_range_start: Mapped[date | None] = mapped_column(Date)
    date_range_end: Mapped[date | None] = mapped_column(Date)
    external_org_value_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_orgs.id")
    )
    document_value_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    transformation: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FactSource(Base):
    """One role-tagged segment supporting a Fact in the same rendition."""

    __tablename__ = "fact_sources"
    __table_args__ = (
        UniqueConstraint(
            "fact_id", "role", "ordinal", name="uq_fact_sources_role_ordinal"
        ),
        UniqueConstraint(
            "fact_id", "source_segment_id", "role", name="uq_fact_sources_link"
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "fact_id"],
            ["facts.project_id", "facts.document_id", "facts.id"],
            name="fk_fact_sources_fact_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_fact_sources_segment_scope",
        ),
        # Project-scoped identity so a document-less verbal Fact Source still
        # proves its Fact and segment exist; the document-scoped keys above hold
        # vacuously when document_id is null (ADR-0074).
        ForeignKeyConstraint(
            ["project_id", "fact_id"],
            ["facts.project_id", "facts.id"],
            name="fk_fact_sources_fact_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_segment_id"],
            ["source_segments.project_id", "source_segments.id"],
            name="fk_fact_sources_segment_project",
        ),
        CheckConstraint(
            "role in ('value_source', 'context', 'attribution_source')",
            name="ck_fact_sources_role",
        ),
        CheckConstraint("ordinal > 0", name="ck_fact_sources_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    fact_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_segment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)


class MinutesCapture(Base):
    """One immutable five-capability minutes reading and its unresolved outcomes."""

    __tablename__ = "minutes_captures"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_minutes_capture_scope"),
        UniqueConstraint("project_id", "source_family", "source_revision", name="uq_minutes_capture_revision"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    extraction_run_id: Mapped[int] = mapped_column(ForeignKey("extraction_runs.id"))
    source_family: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[str] = mapped_column(Text)
    input_sha256: Mapped[str] = mapped_column(String(64))
    accepted_revision_id: Mapped[int] = mapped_column(ForeignKey("project_record_revisions.id"))
    output_json: Mapped[dict] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class MinutesQuestionDisposition(Base):
    """One append-only disposition of one source question (#833).

    A ``minutes_captures`` outcome the reader emits as a question
    (``status == 'unresolved'``) has, until this relation, no permitted ending
    on the Review page. This records one — a Row-2 evidence-bound resolution
    that re-enters comparison, a Row-3 recorded interpretation, a Row-4 named
    clarification request, or a Row-5 scoped exclusion — bound to the exact
    source question through the capture revision it was observed on and the
    wording Source Segment, never by mutating the immutable capture outcome.

    ``question_identity`` is the stable carry key (see
    ``minutes_question_disposition.question_identity``): a digest of the source
    family, the normalized wording, and the sorted reason set, so a later
    revision that changed any of them re-asks the question rather than
    inheriting this answer. The rows are append-only; a correction is a new row
    that names its predecessor in ``supersedes_id`` once, and ``decision_generation``
    refuses a stale or concurrent submission. Only a resolve appends Proposed
    Deltas (through the existing capture+comparison path); its
    ``produced_delta_ids`` name them. Nothing here writes an accepted value.
    """

    __tablename__ = "minutes_question_dispositions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_minutes_question_disposition_scope"
        ),
        ForeignKeyConstraint(
            ["project_id", "capture_id"],
            ["minutes_captures.project_id", "minutes_captures.id"],
            name="fk_minutes_question_disposition_capture",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersedes_id"],
            [
                "minutes_question_dispositions.project_id",
                "minutes_question_dispositions.id",
            ],
            name="fk_minutes_question_disposition_supersedes",
        ),
        CheckConstraint(
            "disposition in ('resolve', 'interpret', 'exclude', 'clarify')",
            name="ck_minutes_question_disposition_kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    capture_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_family: Mapped[str] = mapped_column(Text)
    segment_id: Mapped[int] = mapped_column(ForeignKey("source_segments.id"))
    question_identity: Mapped[str] = mapped_column(String(64), index=True)
    disposition: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(Text)
    evidence_segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_segments.id")
    )
    produced_delta_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    detail: Mapped[dict] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str] = mapped_column(Text)
    decision_generation: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class FactAppliesTo(Base):
    """One exact Constraint member of a structured Applies To Fact."""

    __tablename__ = "fact_applies_to"
    __table_args__ = (
        UniqueConstraint("fact_id", "dependency_id", name="uq_fact_applies_to_member"),
        UniqueConstraint("fact_id", "ordinal", name="uq_fact_applies_to_ordinal"),
        ForeignKeyConstraint(
            ["project_id", "fact_id"],
            ["facts.project_id", "facts.id"],
            name="fk_fact_applies_to_fact_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "dependency_id"],
            ["dependencies.project_id", "dependencies.id"],
            name="fk_fact_applies_to_dependency_scope",
        ),
        CheckConstraint("ordinal > 0", name="ck_fact_applies_to_ordinal"),
        UniqueConstraint("fact_id", "record_subject_key", name="uq_fact_applies_to_record_subject"),
        CheckConstraint("num_nonnulls(dependency_id, record_subject_key) = 1", name="ck_fact_applies_to_target"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    fact_id: Mapped[int] = mapped_column(BigInteger, index=True)
    dependency_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    record_subject_key: Mapped[str | None] = mapped_column(Text)
    source_segment_id: Mapped[int | None] = mapped_column(ForeignKey("source_segments.id"))
    reference_text: Mapped[str | None] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer)


class FactClosureResult(Base):
    """The typed result attached to one interpretation-bearing closure Fact."""

    __tablename__ = "fact_closure_results"
    __table_args__ = (
        UniqueConstraint("fact_id", name="uq_fact_closure_result_fact"),
        ForeignKeyConstraint(
            ["project_id", "fact_id"],
            ["facts.project_id", "facts.id"],
            name="fk_fact_closure_result_fact_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "successor_dependency_id"],
            ["dependencies.project_id", "dependencies.id"],
            name="fk_fact_closure_result_successor_scope",
        ),
        CheckConstraint(
            "closure_kind in ('source_marked_resolved', 'constraint_closed', "
            "'constraint_remains_open', 'completion_reported')",
            name="ck_fact_closure_result_kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    fact_id: Mapped[int] = mapped_column(BigInteger, index=True)
    closure_kind: Mapped[str] = mapped_column(String(48))
    successor_dependency_id: Mapped[int | None] = mapped_column(BigInteger)


class FactClosureSource(Base):
    """One governing source segment for a typed closure result."""

    __tablename__ = "fact_closure_sources"
    __table_args__ = (
        UniqueConstraint(
            "fact_id", "source_segment_id", name="uq_fact_closure_source_segment"
        ),
        UniqueConstraint("fact_id", "ordinal", name="uq_fact_closure_source_ordinal"),
        ForeignKeyConstraint(
            ["project_id", "document_id", "fact_id"],
            ["facts.project_id", "facts.document_id", "facts.id"],
            name="fk_fact_closure_source_fact_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_fact_closure_source_segment_scope",
        ),
        CheckConstraint("ordinal > 0", name="ck_fact_closure_source_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(BigInteger, index=True)
    fact_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_segment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


class FactStatementTiming(Base):
    """One source-preserving timing a Recorded Verbal Statement stated.

    A verbal timing carries the party's exact wording at day, month, or
    approximate precision, mirroring ``dependency_event_timings`` so the spine
    timing stays byte-exact with the legacy timing it dual-writes beside
    (ADR-0074).  A statement may state several timings (a change of promise
    states the ``previous`` and the ``new``), so the set is carried here rather
    than as a single scalar on the Fact.
    """

    __tablename__ = "fact_statement_timings"
    __table_args__ = (
        UniqueConstraint(
            "fact_id", "timing_role", name="uq_fact_statement_timing_role"
        ),
        ForeignKeyConstraint(
            ["project_id", "fact_id"],
            ["facts.project_id", "facts.id"],
            name="fk_fact_statement_timing_fact_scope",
        ),
        CheckConstraint(
            "timing_role in ('previous', 'new')",
            name="ck_fact_statement_timing_role",
        ),
        CheckConstraint(
            "precision in ('day', 'month', 'range', 'approximate')",
            name="ck_fact_statement_timing_precision",
        ),
        CheckConstraint(
            "length(trim(text)) > 0", name="ck_fact_statement_timing_text"
        ),
        CheckConstraint(
            "(precision = 'day' and start_date is not null "
            "and end_date = start_date) "
            "or (precision = 'month' and start_date is not null "
            "and end_date is not null "
            "and start_date = date_trunc('month', start_date::timestamp)::date "
            "and end_date = (date_trunc('month', start_date::timestamp) "
            "+ interval '1 month - 1 day')::date) "
            "or (precision = 'range' and start_date is not null and end_date is not null and start_date <= end_date) "
            "or (precision = 'approximate' and start_date is null "
            "and end_date is null)",
            name="ck_fact_statement_timing_bounds",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    fact_id: Mapped[int] = mapped_column(BigInteger, index=True)
    timing_role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    precision: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)


class SourceFactAppendReceipt(Base):
    """One immutable idempotency binding for the scoped spine append command."""

    __tablename__ = "source_fact_append_receipts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_source_fact_append_key"
        ),
        UniqueConstraint(
            "project_id", "content_sha256", name="uq_source_fact_append_content"
        ),
        ForeignKeyConstraint(
            ["document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
            name="fk_source_fact_append_run_document",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_fact_append_content_sha256",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_source_fact_append_key",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    extraction_run_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


SUPPORT_ASSESSMENT_EVIDENCE_ROLES = (
    "value_support",
    "attribution",
    "timing",
    "scope",
    "context",
)
SUPPORT_ASSESSMENT_OUTCOMES = (
    "supported",
    "partially_supported",
    "contradicted",
    "unclear",
    "not_assessed",
)
_SUPPORT_ASSESSMENT_EVIDENCE_ROLES_SQL = ", ".join(
    f"'{value}'" for value in SUPPORT_ASSESSMENT_EVIDENCE_ROLES
)
_SUPPORT_ASSESSMENT_OUTCOMES_SQL = ", ".join(
    f"'{value}'" for value in SUPPORT_ASSESSMENT_OUTCOMES
)


class SupportAssessment(Base):
    """One attributable judgment that Source Segments support a proposition.

    The relation ADR-0082 decided: exactly one typed proposition (a Source
    Fact or an Extracted Proposal today; a Proposed Delta or accepted field
    joins ``ck_support_assessments_proposition`` with its own column and
    composite key, never an unchecked object-type/object-id pair), one or
    more segments through ``SupportAssessmentSource``, the evidence role,
    the assessment, and exactly one authority.  Rows are append-only and
    written only by ``append_support_assessment``; a correction supersedes
    its predecessor once, so the effective reading is the row with no
    successor and an as-of reading walks ``assessed_at`` (#530).
    """

    __tablename__ = "support_assessments"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_support_assessments_project_id"
        ),
        UniqueConstraint(
            "project_id",
            "document_id",
            "id",
            name="uq_support_assessments_scope_id",
        ),
        UniqueConstraint("content_sha256", name="uq_support_assessments_content"),
        UniqueConstraint(
            "superseded_by", name="uq_support_assessments_superseded_by"
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "fact_id"],
            ["facts.project_id", "facts.document_id", "facts.id"],
            name="fk_support_assessments_fact_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "fact_id"],
            ["facts.project_id", "facts.id"],
            name="fk_support_assessments_fact_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "extracted_proposal_id"],
            [
                "extracted_proposals.project_id",
                "extracted_proposals.document_id",
                "extracted_proposals.id",
            ],
            name="fk_support_assessments_proposal_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "extracted_proposal_id"],
            ["extracted_proposals.project_id", "extracted_proposals.id"],
            name="fk_support_assessments_proposal_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "proposed_delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_support_assessments_delta_project",
        ),
        CheckConstraint(
            "(proposition_kind = 'source_fact' and fact_id is not null "
            "and extracted_proposal_id is null and proposed_delta_id is null) or "
            "(proposition_kind = 'extracted_proposal' "
            "and extracted_proposal_id is not null and fact_id is null "
            "and proposed_delta_id is null and document_id is not null) or "
            "(proposition_kind = 'proposed_delta' "
            "and proposed_delta_id is not null and fact_id is null "
            "and extracted_proposal_id is null)",
            name="ck_support_assessments_proposition",
        ),
        CheckConstraint(
            f"evidence_role in ({_SUPPORT_ASSESSMENT_EVIDENCE_ROLES_SQL})",
            name="ck_support_assessments_evidence_role",
        ),
        CheckConstraint(
            f"assessment in ({_SUPPORT_ASSESSMENT_OUTCOMES_SQL})",
            name="ck_support_assessments_assessment",
        ),
        CheckConstraint(
            "(human_principal is null) <> (released_policy is null)",
            name="ck_support_assessments_authority_xor",
        ),
        CheckConstraint(
            "human_principal is null or length(trim(human_principal)) > 0",
            name="ck_support_assessments_human_principal",
        ),
        CheckConstraint(
            "released_policy is null or length(trim(released_policy)) > 0",
            name="ck_support_assessments_released_policy",
        ),
        CheckConstraint(
            "(released_policy is null) = (ruleset_version is null) "
            "and (ruleset_version is null or length(trim(ruleset_version)) > 0)",
            name="ck_support_assessments_ruleset",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_support_assessments_content_sha256",
        ),
        CheckConstraint("superseded_by <> id", name="ck_support_assessments_not_self"),
        Index(
            "uq_support_assessments_effective_fact",
            "fact_id",
            "evidence_role",
            unique=True,
            postgresql_where=text("superseded_by is null and fact_id is not null"),
        ),
        Index(
            "uq_support_assessments_effective_proposal",
            "extracted_proposal_id",
            "evidence_role",
            unique=True,
            postgresql_where=text(
                "superseded_by is null and extracted_proposal_id is not null"
            ),
        ),
        Index(
            "uq_support_assessments_effective_delta",
            "proposed_delta_id",
            "evidence_role",
            unique=True,
            postgresql_where=text(
                "superseded_by is null and proposed_delta_id is not null"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    # The proposition's rendition; null only for a document-less verbal Fact.
    document_id: Mapped[int | None] = mapped_column(BigInteger)
    proposition_kind: Mapped[str] = mapped_column(String(32))
    fact_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    extracted_proposal_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    proposed_delta_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    evidence_role: Mapped[str] = mapped_column(String(32))
    assessment: Mapped[str] = mapped_column(String(32))
    human_principal: Mapped[str | None] = mapped_column(String(128))
    released_policy: Mapped[str | None] = mapped_column(String(128))
    ruleset_version: Mapped[str | None] = mapped_column(String(64))
    superseded_by: Mapped[int | None] = mapped_column(
        ForeignKey("support_assessments.id", deferrable=True, initially="DEFERRED")
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SupportAssessmentSource(Base):
    """One Source Segment a Support Assessment weighed, in the same rendition."""

    __tablename__ = "support_assessment_sources"
    __table_args__ = (
        UniqueConstraint(
            "support_assessment_id",
            "source_segment_id",
            name="uq_support_assessment_sources_segment",
        ),
        UniqueConstraint(
            "support_assessment_id",
            "ordinal",
            name="uq_support_assessment_sources_ordinal",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "support_assessment_id"],
            [
                "support_assessments.project_id",
                "support_assessments.document_id",
                "support_assessments.id",
            ],
            name="fk_support_assessment_sources_assessment_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "support_assessment_id"],
            ["support_assessments.project_id", "support_assessments.id"],
            name="fk_support_assessment_sources_assessment_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_support_assessment_sources_segment_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_segment_id"],
            ["source_segments.project_id", "source_segments.id"],
            name="fk_support_assessment_sources_segment_project",
        ),
        CheckConstraint("ordinal > 0", name="ck_support_assessment_sources_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int | None] = mapped_column(BigInteger)
    support_assessment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_segment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


class FactDisposition(Base):
    """One typed append-only source-reading correction edge."""

    __tablename__ = "fact_dispositions"
    __table_args__ = (
        UniqueConstraint("predecessor_fact_id", name="uq_fact_disposition_predecessor"),
        UniqueConstraint("successor_fact_id", name="uq_fact_disposition_successor"),
        CheckConstraint(
            "kind = 'source_reading_correction'", name="ck_fact_disposition_kind"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    predecessor_fact_id: Mapped[int] = mapped_column(ForeignKey("facts.id"))
    successor_fact_id: Mapped[int] = mapped_column(ForeignKey("facts.id"))
    kind: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectRecordRevision(Base):
    """One atomic Project Record change with exactly one authority."""

    __tablename__ = "project_record_revisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_project_record_revision_key"
        ),
        CheckConstraint(
            "(human_principal is null) <> (released_policy is null)",
            name="ck_project_record_revision_authority_xor",
        ),
        # Every command refuses a blank key in its own body; the column used
        # to accept one, and a blank key is the same non-identity for all of
        # them, so the revision family's dedup identity has to exist (#457).
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_project_record_revisions_idempotency_key",
        ),
        # The key a report reading's composite binding resolves against, so a
        # Report Run bound to another project's revision is unrepresentable
        # rather than merely unlikely (#602).
        UniqueConstraint(
            "id", "project_id", name="uq_project_record_revisions_project_revision"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    predecessor_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    command_type: Mapped[str] = mapped_column(String(64))
    human_principal: Mapped[str | None] = mapped_column(String(128))
    released_policy: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BaselineAdoption(Base):
    """The immutable receipt one project's operating mode is derived from (#520)."""

    __tablename__ = "project_baseline_adoptions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", name="uq_project_baseline_adoptions_project"
        ),
        CheckConstraint(
            "baseline_source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_project_baseline_adoptions_digest",
        ),
        CheckConstraint(
            "length(btrim(adopted_by_principal)) > 0",
            name="ck_project_baseline_adoptions_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    adopted_by_principal: Mapped[str] = mapped_column(String(128))
    baseline_source_sha256: Mapped[str] = mapped_column(String(64))
    importer_identity: Mapped[str] = mapped_column(String(128))
    importer_version: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    adopted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


BASELINE_SOURCE_KINDS = ("ucm_workbook", "system_export")
BASELINE_FORMAT_KINDS = ("output_template", "field_mapping")


class BaselineSource(Base):
    """The accepted data-baseline identity one project adopted (#509).

    One row per project, because a project has one initial Adopt Baseline;
    replacing it is a later record change rather than a second adoption. The
    row retains the exact bytes' digest, the customer and their own name for
    the revision, the adopted worksheet or record scope, the importer, the
    coordinator preview the named person actually adopted, and the Corridor
    operations reading that resolved the workbook mechanics first.
    """

    __tablename__ = "project_baseline_sources"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_baseline_sources_project"),
        UniqueConstraint(
            "project_id", "id", name="uq_project_baseline_sources_project_id"
        ),
        UniqueConstraint("revision_id", name="uq_project_baseline_sources_revision"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_project_baseline_sources_digest",
        ),
        CheckConstraint(
            "preview_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_project_baseline_sources_preview",
        ),
        CheckConstraint(
            "source_kind in ('ucm_workbook', 'system_export')",
            name="ck_project_baseline_sources_kind",
        ),
        CheckConstraint(
            "length(btrim(adopted_by_principal)) > 0",
            name="ck_project_baseline_sources_principal",
        ),
        CheckConstraint("byte_size > 0", name="ck_project_baseline_sources_byte_size"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    content_sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    filename: Mapped[str] = mapped_column(Text)
    source_identity: Mapped[str] = mapped_column(String(160))
    customer: Mapped[str] = mapped_column(String(160))
    source_kind: Mapped[str] = mapped_column(String(32))
    worksheet_scope: Mapped[Any] = mapped_column(JSONB)
    unknown_columns: Mapped[Any] = mapped_column(JSONB)
    coordinator_questions: Mapped[Any] = mapped_column(JSONB)
    operations_summary: Mapped[Any] = mapped_column(JSONB)
    importer_identity: Mapped[str] = mapped_column(String(128))
    importer_version: Mapped[str] = mapped_column(String(64))
    preview_fingerprint: Mapped[str] = mapped_column(String(64))
    adopted_by_principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    adopted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BaselineSourceRow(Base):
    """One adopted source row's identity, distinct from its record subject (#509).

    ``source_row_key`` locates the row in the customer's own file;
    ``record_subject_key`` is the Project Record subject it resolves to, and the
    two never merge — two rows repeating one ``business_identity`` keep separate
    subjects, so a duplicate matrix id cannot silently collapse distinct
    facilities. ``external_system_id`` and ``source_url`` preserve the
    utility-management-system record ids and document-control references the
    workbook itself printed, for later read-only deep links (#527, #528).
    """

    __tablename__ = "project_baseline_source_rows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "baseline_source_id"],
            [
                "project_baseline_sources.project_id",
                "project_baseline_sources.id",
            ],
            name="fk_project_baseline_source_rows_source",
        ),
        UniqueConstraint(
            "baseline_source_id",
            "source_row_key",
            name="uq_project_baseline_source_rows_key",
        ),
        UniqueConstraint(
            "baseline_source_id",
            "record_subject_key",
            name="uq_project_baseline_source_rows_subject",
        ),
        CheckConstraint(
            "row_number > 0", name="ck_project_baseline_source_rows_row_number"
        ),
        CheckConstraint(
            "(excluded and record_subject_key is null "
            "and exclusion_reason is not null) "
            "or (not excluded and record_subject_key is not null "
            "and exclusion_reason is null)",
            name="ck_project_baseline_source_rows_exclusion",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    baseline_source_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_row_key: Mapped[str] = mapped_column(String(160))
    sheet_name: Mapped[str] = mapped_column(Text)
    row_number: Mapped[int] = mapped_column(Integer)
    business_identity: Mapped[str | None] = mapped_column(String(128))
    record_subject_key: Mapped[str | None] = mapped_column(String(160))
    external_system_id: Mapped[str | None] = mapped_column(String(160))
    source_url: Mapped[str | None] = mapped_column(Text)
    excluded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    exclusion_reason: Mapped[str | None] = mapped_column(String(64))


class BaselineFormat(Base):
    """One registered output-template or field-mapping identity (#509).

    Held apart from ``BaselineSource`` on purpose: a later output template or
    column mapping is registered on its own attributable act, supersedes its
    predecessor, and changes no accepted value. Registering one is never a
    second Adopt Baseline.
    """

    __tablename__ = "project_baseline_formats"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_project_baseline_formats_key"
        ),
        UniqueConstraint(
            "superseded_by", name="uq_project_baseline_formats_superseded_by"
        ),
        CheckConstraint(
            "format_kind in ('output_template', 'field_mapping')",
            name="ck_project_baseline_formats_kind",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_project_baseline_formats_digest",
        ),
        CheckConstraint(
            "length(btrim(registered_by_principal)) > 0",
            name="ck_project_baseline_formats_principal",
        ),
        Index(
            "uq_project_baseline_formats_effective",
            "project_id",
            "format_kind",
            unique=True,
            postgresql_where=text("superseded_by is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    format_kind: Mapped[str] = mapped_column(String(32))
    format_identity: Mapped[str] = mapped_column(String(160))
    format_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    registered_by_principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    superseded_by: Mapped[int | None] = mapped_column(
        ForeignKey(
            "project_baseline_formats.id", deferrable=True, initially="DEFERRED"
        )
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BaselineFormatManifest(Base):
    """The full declaration one registered mapping revision records (#610).

    ``BaselineFormat`` holds a mapping revision's identity, version and digest.
    That proves *which* revision a render was performed under and not *what*
    that revision declared, so reproducing a past render depended on whoever
    declared it still holding the declaration. This row is that declaration,
    stored as the exact canonical bytes the digest is taken over — the same
    shape as a Source Segment, which retains exact text beside its digest
    rather than the digest alone.

    One row per registration, immutably: the primary key is the registration's
    own id, so a stored declaration cannot outlive or precede the act that
    registered it, and the composite foreign key back to the registration's
    identity, version and digest makes a stored declaration that disagrees with
    what was registered unrepresentable rather than merely unlikely. A
    registration with no row here has no stored declaration, which is an
    explicit absence and never an empty manifest.
    """

    __tablename__ = "project_baseline_format_manifests"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "format_id",
                "project_id",
                "format_identity",
                "format_version",
                "content_sha256",
            ],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_identity",
                "project_baseline_formats.format_version",
                "project_baseline_formats.content_sha256",
            ],
            name="fk_project_baseline_format_manifests_registration",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(declaration, 'utf8')), 'hex') "
            "= content_sha256",
            name="ck_project_baseline_format_manifests_digest",
        ),
        CheckConstraint(
            "length(btrim(manifest_schema_version)) > 0",
            name="ck_project_baseline_format_manifests_schema",
        ),
    )

    format_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(BigInteger, index=True)
    format_identity: Mapped[str] = mapped_column(String(160))
    format_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    manifest_schema_version: Mapped[str] = mapped_column(String(64))
    declaration: Mapped[str] = mapped_column(Text)


class BaselineFormatObject(Base):
    """The exact retained bytes one output-template registration stands on (#690).

    ``BaselineFormat`` stores a template's *digest*, which proves which bytes
    were registered and not that anybody kept them. Initial adoption works only
    by accident: the adopted workbook is staged and registered as a Document
    before it becomes the as-adopted output template, so its bytes are in the
    content store for a reason that has nothing to do with the registration. A
    later ``register_baseline_format`` call could register a digest and supply
    no bytes at all, and storage reconciliation -- which derives its expected
    objects from Documents, Processing Artifacts, page renders and token layers
    -- would neither miss them nor protect them.

    So this is the registration's storage binding: an output template is not
    registered until its exact bytes have first been retained through #487 and
    verified against the registration digest. The composite foreign key carries
    the registration's identity, version and digest, so a binding that names
    different content is unrepresentable; the storage key is a check-constrained
    function of the digest, so it is derived rather than chosen. A preparation
    that cannot prove the object fails with a bounded reason and never falls
    back to another template.
    """

    __tablename__ = "project_baseline_format_objects"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "format_id",
                "project_id",
                "format_identity",
                "format_version",
                "content_sha256",
            ],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_identity",
                "project_baseline_formats.format_version",
                "project_baseline_formats.content_sha256",
            ],
            name="fk_project_baseline_format_objects_registration",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_project_baseline_format_objects_digest",
        ),
        CheckConstraint(
            "storage_key = substr(content_sha256, 1, 2) || '/' "
            "|| content_sha256 || file_suffix",
            name="ck_project_baseline_format_objects_key",
        ),
        CheckConstraint(
            "file_suffix = '' or file_suffix ~ '^[.][A-Za-z0-9.]{1,16}$'",
            name="ck_project_baseline_format_objects_suffix",
        ),
        CheckConstraint(
            "byte_count > 0",
            name="ck_project_baseline_format_objects_bytes",
        ),
        CheckConstraint(
            "length(btrim(retained_by_principal)) > 0",
            name="ck_project_baseline_format_objects_principal",
        ),
        Index(
            "ix_project_baseline_format_objects_project",
            "project_id",
            "format_id",
        ),
    )

    format_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    format_identity: Mapped[str] = mapped_column(String(160))
    format_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(160))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    file_suffix: Mapped[str] = mapped_column(String(32))
    retained_by_principal: Mapped[str] = mapped_column(String(128))
    retained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FactDecision(Base):
    """One typed Record Inclusion decision whose effectiveness may be superseded."""

    __tablename__ = "fact_decisions"
    __table_args__ = (
        # One EFFECTIVE decision per fact: a compensating Human Record
        # Decision re-decides the same fact after its predecessor is
        # superseded (ADR-0074 stage 3), so uniqueness is partial.
        Index(
            "uq_fact_decision_fact",
            "fact_id",
            unique=True,
            postgresql_where=text("superseded_by is null"),
        ),
        CheckConstraint(
            "disposition in ('include', 'do_not_add', 'restore')",
            name="ck_fact_decision_disposition",
        ),
        # One revision decides one Fact once (#457). The decision's dedup
        # identity is its revision's idempotency key, and the commands read
        # a revision's decision back by ``revision_id``; without this a
        # revision could carry the same Fact twice and that read would be
        # picking one of a set.
        UniqueConstraint(
            "revision_id", "fact_id", name="uq_fact_decisions_revision_fact"
        ),
        Index(
            "uq_fact_decision_effective",
            "project_id",
            "subject_key",
            "fact_type",
            unique=True,
            postgresql_where=text(
                "superseded_by is null and fact_type in "
                f"({_EFFECTIVE_SINGLE_VALUE_FACT_TYPES_SQL})"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    fact_id: Mapped[int] = mapped_column(ForeignKey("facts.id"))
    subject_key: Mapped[str] = mapped_column(Text)
    fact_type: Mapped[str] = mapped_column(String(64))
    revision_id: Mapped[int] = mapped_column(ForeignKey("project_record_revisions.id"))
    # What the Project Record does with the fact: 'include' projects it,
    # 'do_not_add' suppresses the statement's facts, 'restore' compensates a
    # predecessor and contributes nothing itself (ADR-0074 stage 3). Readers
    # branch on this, never on the revision's command name.
    disposition: Mapped[str] = mapped_column(
        String(32), server_default=text("'include'")
    )
    superseded_by: Mapped[int | None] = mapped_column(
        ForeignKey("fact_decisions.id", deferrable=True, initially="DEFERRED")
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- The limited onboarding authorization ADR-0099 decides (#827) -----------
#
# The authorization itself is the control plane's (ADR-0083, a different
# database). These four relations are what the customer environment holds: the
# grant a restricted operations actor recorded here, what happened to it
# afterwards, the preview the coordinator approves, and the retained proof that
# a permitted onboarding act committed while the grant was valid.
#
# All four are append-only and written only by their own commands; the mappings
# below are readings, and nothing in the application writes them through the
# ORM.


class OnboardingGrant(Base):
    """One project's record of a limited onboarding authorization (#827)."""

    __tablename__ = "project_onboarding_grants"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_project_onboarding_grants_project_id"),
        UniqueConstraint(
            "project_id",
            "authorization_id",
            "grant_version",
            name="uq_project_onboarding_grants_version",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    authorization_id: Mapped[str] = mapped_column(String(128))
    grant_version: Mapped[int] = mapped_column(Integer)
    customer: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(128))
    permitted_operations: Mapped[list[str]] = mapped_column(ARRAY(Text))
    source_scope: Mapped[str] = mapped_column(String(256))
    # The typed source scope (#951). ``scope_contract_version`` 0 is the
    # narrative ``source_scope`` alone -- readable history that authorizes no
    # source-dependent processing by inference -- and 1 and up name the
    # ``permitted_source_classes`` a delivery's declared class is matched
    # against. The two ``bound_source_*`` columns, when set, pin the grant to
    # one delivery's exact identity and content digest.
    scope_contract_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    permitted_source_classes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    bound_source_identity: Mapped[str | None] = mapped_column(Text)
    bound_source_sha256: Mapped[str | None] = mapped_column(String(64))
    governing_authorization_identity: Mapped[str] = mapped_column(String(128))
    governing_authorization_version: Mapped[str] = mapped_column(String(64))
    evidence_identity: Mapped[str] = mapped_column(String(256))
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    issued_by_actor: Mapped[str] = mapped_column(String(128))
    recorded_by_actor: Mapped[str] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OnboardingGrantEvent(Base):
    """What happened to one grant after it was recorded (#827).

    Withdrawal is three separate recorded facts, never one: the customer's
    request, this database's enforcement of it, and an enforcement that failed.
    ADR-0099 refuses to let a request be described as fully enforced while the
    customer database can still exercise the grant, and that is only sayable if
    the two are different rows.
    """

    __tablename__ = "project_onboarding_grant_events"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_project_onboarding_grant_events_project_id"
        ),
        ForeignKeyConstraint(
            ["project_id", "grant_id"],
            ["project_onboarding_grants.project_id", "project_onboarding_grants.id"],
            name="fk_project_onboarding_grant_events_grant",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(BigInteger)
    grant_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(48))
    requested_by: Mapped[str | None] = mapped_column(String(256))
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_by_actor: Mapped[str] = mapped_column(String(128))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OnboardingPreview(Base):
    """The server-retained preview a coordinator approves (#827).

    ``adoptable`` is the database's own answer, taken from
    ``project_operating_mode`` when the preview was retained. A preview
    regenerated after the project adopted is retained for verification and is
    not an adoptable baseline.
    """

    __tablename__ = "project_onboarding_previews"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_project_onboarding_previews_project_id"
        ),
        UniqueConstraint(
            "project_id",
            "binding_fingerprint",
            name="uq_project_onboarding_previews_fingerprint",
        ),
        ForeignKeyConstraint(
            ["project_id", "grant_id"],
            ["project_onboarding_grants.project_id", "project_onboarding_grants.id"],
            name="fk_project_onboarding_previews_grant",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    grant_id: Mapped[int] = mapped_column(BigInteger)
    source_sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(512))
    source_identity: Mapped[str] = mapped_column(String(256))
    mapping_identity: Mapped[str] = mapped_column(String(256))
    mapping_version: Mapped[str] = mapped_column(String(64))
    binding_fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    operations_resolved: Mapped[bool] = mapped_column(Boolean)
    blocking_question_count: Mapped[int] = mapped_column(Integer)
    adoptable: Mapped[bool] = mapped_column(Boolean)
    prepared_by_actor: Mapped[str] = mapped_column(String(128))
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OnboardingAct(Base):
    """Retained proof that one onboarding act committed under a valid grant (#827).

    ADR-0099 asks activation for this row rather than for an unexpired
    authorization, so a legitimate adoption does not become unusable history
    when the temporary permission lapses. ``(project_id, operation,
    request_key)`` is the exact-retry key; ``(project_id, operation)`` is what
    refuses a second act under a new key.
    """

    __tablename__ = "project_onboarding_acts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_project_onboarding_acts_project_id"
        ),
        UniqueConstraint(
            "project_id",
            "operation",
            "request_key",
            name="uq_project_onboarding_acts_request",
        ),
        UniqueConstraint(
            "project_id", "operation", name="uq_project_onboarding_acts_once"
        ),
        ForeignKeyConstraint(
            ["project_id", "grant_id"],
            ["project_onboarding_grants.project_id", "project_onboarding_grants.id"],
            name="fk_project_onboarding_acts_grant",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    grant_id: Mapped[int] = mapped_column(BigInteger)
    authorization_id: Mapped[str] = mapped_column(String(128))
    grant_version: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(48))
    request_key: Mapped[str] = mapped_column(String(160))
    material_sha256: Mapped[str] = mapped_column(String(64))
    principal: Mapped[str] = mapped_column(String(128))
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    validity: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
