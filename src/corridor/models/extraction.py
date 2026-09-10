"""Extraction runs and the Extracted Proposals they produce, never records.

An extractor's output is a proposal, always -- ADR-0081 freezes the legacy path
where `adjudicate` wrote a Constraint Record directly. The run row carries the
``llm_model`` and prompt version so two runs' numbers can be compared, and the
unreadable-cell reading profile records what a run was allowed to look at
before it looked, so a later reading cannot widen its own scope.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import (
    Base,
    EXTRACTION_OUTCOMES,
    REVISION_COMPARISON_STATES,
    _enum,
)


__all__ = [
    "ActiveExtractionRun",
    "ActiveRunDeclaration",
    "ExtractedProposal",
    "ExtractedProposalFact",
    "ExtractionRun",
    "ExtractionRunCandidate",
    "ExtractorConfiguration",
    "PREDATES_OBSERVATION_BINDING",
    "RevisionComparisonFinding",
    "RevisionComparisonRun",
    "ScannedPageObservation",
    "UnreadableCellReadingProfile",
    "UnreadableCellReadingRun",
    "UnreadableCellReadingStep",
    "UnreadableCellResolution",
]


class ExtractedProposal(Base):
    """One immutable proposal identity grouping spine Facts by reference."""

    __tablename__ = "extracted_proposals"
    __table_args__ = (
        UniqueConstraint(
            "extraction_run_id", "subject_key", name="uq_extracted_proposal_subject"
        ),
        UniqueConstraint(
            "project_id",
            "document_id",
            "extraction_run_id",
            "id",
            name="uq_extracted_proposal_scope_id",
        ),
        # Project- and rendition-scoped identities so a Support Assessment
        # can hold its proposal and its segments to one project and one
        # document by foreign key (#530).
        UniqueConstraint(
            "project_id", "id", name="uq_extracted_proposals_project_id"
        ),
        UniqueConstraint(
            "project_id",
            "document_id",
            "id",
            name="uq_extracted_proposals_document_scope_id",
        ),
        ForeignKeyConstraint(
            ["document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
            name="fk_extracted_proposal_run_document",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    extraction_run_id: Mapped[int] = mapped_column(BigInteger, index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(Text)
    candidate_metadata_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExtractionRunCandidate(Base):
    """Immutable Candidate membership for a snapshot-free Extraction Run."""

    __tablename__ = "extraction_run_candidates"
    __table_args__ = (
        UniqueConstraint(
            "extraction_run_id", "candidate_id", name="uq_extraction_run_candidate"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id"), index=True
    )
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), unique=True)


class ExtractedProposalFact(Base):
    """One immutable Fact reference within an Extracted Proposal."""

    __tablename__ = "extracted_proposal_facts"
    __table_args__ = (
        UniqueConstraint("proposal_id", "fact_id", name="uq_extracted_proposal_fact"),
        UniqueConstraint("proposal_id", "ordinal", name="uq_extracted_proposal_ordinal"),
        ForeignKeyConstraint(
            ["project_id", "document_id", "extraction_run_id", "proposal_id"],
            [
                "extracted_proposals.project_id",
                "extracted_proposals.document_id",
                "extracted_proposals.extraction_run_id",
                "extracted_proposals.id",
            ],
            name="fk_extracted_proposal_fact_proposal_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "extraction_run_id", "fact_id"],
            [
                "facts.project_id",
                "facts.document_id",
                "facts.extraction_run_id",
                "facts.id",
            ],
            name="fk_extracted_proposal_fact_fact_scope",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(BigInteger)
    extraction_run_id: Mapped[int] = mapped_column(BigInteger)
    proposal_id: Mapped[int] = mapped_column(BigInteger, index=True)
    fact_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


class ExtractorConfiguration(Base):
    """One sealed extractor configuration, stored once and keyed by its digest.

    The configuration a run was produced under is a value, not a possession:
    every run of the same deployed extractor seals byte-identical prompts,
    schemas, post-processor sources, request controls, and runtime, so a copy
    per run is the same object written a thousand times.  This registry stores
    it once; ``ExtractionRun.extractor_config_sha256`` is the reference (#605).

    Rows are immutable — a trigger refuses every update and delete — because a
    run that references a configuration is asserting what it actually ran, and
    a configuration that could be edited afterwards would let that assertion
    become false without anything appending a row to say so.
    """

    __tablename__ = "extractor_configurations"
    __table_args__ = (
        CheckConstraint(
            "config_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_extractor_configurations_digest",
        ),
        # The receipt shape lives in one SQL function rather than being
        # restated here and on extraction_runs: two copies of a shape rule are
        # the same defect this table removes, one level up.
        CheckConstraint(
            "extractor_configuration_receipt_is_valid(config_json)",
            name="ck_extractor_configurations_receipt",
        ),
    )

    config_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_json: Mapped[dict] = mapped_column(JSONB)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExtractionRun(Base):
    """One immutable extraction attempt for one document and prompt version.

    A receipt exists for every terminal outcome, including failures and valid
    zero-row reads.  A redo appends another receipt; it never rewrites the
    earlier attempt or the Candidates that attempt produced.
    """

    __tablename__ = "extraction_runs"
    __table_args__ = (
        UniqueConstraint("document_id", "id"),
        # The run references its sealed configuration; it no longer owns a
        # copy of it (#605). Historical rows that predate the seal reference
        # nothing, and stay explicitly unknown rather than being given today's
        # deployed configuration as though it were theirs.
        ForeignKeyConstraint(
            ["extractor_config_sha256"],
            ["extractor_configurations.config_sha256"],
            name="fk_extraction_runs_extractor_configuration",
        ),
        CheckConstraint(
            """
            (
                prompt_sha256 is null
                and schema_sha256 is null
                and postprocessor_sha256 is null
                and extractor_config_json is null
                and extractor_config_sha256 is null
                and token_usage_json is null
            )
            or
            (
                prompt_sha256 is not null
                and schema_sha256 is not null
                and postprocessor_sha256 is not null
                and extractor_config_sha256 is not null
                and token_usage_json is not null
                and prompt_sha256 ~ '^[0-9a-f]{64}$'
                and schema_sha256 ~ '^[0-9a-f]{64}$'
                and postprocessor_sha256 ~ '^[0-9a-f]{64}$'
                and extractor_config_sha256 ~ '^[0-9a-f]{64}$'
                -- The sealed configuration is one row in
                -- extractor_configurations, reached by the digest above
                -- (#605). This proves the referenced receipt is well formed
                -- and agrees with the run's own columns; a legacy row that
                -- still carries its inline copy must additionally hold the
                -- identical object, so the copy can never drift from the
                -- registry row it duplicates.
                and extraction_run_configuration_is_valid(
                    extractor_config_sha256,
                    extractor_config_json,
                    prompt_version,
                    schema_version,
                    model,
                    prompt_sha256,
                    schema_sha256,
                    postprocessor_sha256
                )
                and jsonb_typeof(token_usage_json) = 'object'
                and token_usage_json ?& array[
                    'scope', 'document_ids', 'measurement'
                ]
                and jsonb_typeof(token_usage_json -> 'scope') = 'string'
                and jsonb_typeof(token_usage_json -> 'measurement') = 'string'
                and token_usage_json ->> 'scope' in ('run', 'batch')
                and jsonb_typeof(token_usage_json -> 'document_ids') = 'array'
                and jsonb_array_length(token_usage_json -> 'document_ids') > 0
                and (
                    (
                        token_usage_json ->> 'scope' = 'run'
                        and jsonb_array_length(
                            token_usage_json -> 'document_ids'
                        ) = 1
                    )
                    or (
                        token_usage_json ->> 'scope' = 'batch'
                        and jsonb_array_length(
                            token_usage_json -> 'document_ids'
                        ) > 1
                    )
                )
                and (
                    (
                        token_usage_json ->> 'measurement' = 'unavailable'
                        and token_usage_json ? 'reason'
                        and jsonb_typeof(
                            token_usage_json -> 'reason'
                        ) = 'string'
                        and length(trim(token_usage_json ->> 'reason')) > 0
                    )
                    or (
                        token_usage_json ->> 'measurement' = 'exact'
                        and token_usage_json ?& array[
                            'prompt_tokens', 'completion_tokens',
                            'reasoning_tokens', 'cached_tokens'
                        ]
                        and jsonb_typeof(
                            token_usage_json -> 'prompt_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'completion_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'reasoning_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'cached_tokens'
                        ) = 'number'
                        and token_usage_json ->> 'prompt_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'completion_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'reasoning_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'cached_tokens' ~ '^[0-9]+$'
                        and (token_usage_json ->> 'prompt_tokens')::numeric >= 0
                        and (token_usage_json ->> 'completion_tokens')::numeric >= 0
                        and (token_usage_json ->> 'reasoning_tokens')::numeric >= 0
                        and (token_usage_json ->> 'cached_tokens')::numeric >= 0
                    )
                )
                and extraction_token_usage_membership_is_valid(
                    document_id,
                    token_usage_json
                )
            ) is true
            """,
            name="ck_extraction_runs_config_receipt_shape",
        ),
        CheckConstraint(
            '''
            case when prompt_version = 'matrix_structure_ids_v1' or row_accounting_json ->> 'schema_version' = 'native-matrix-row-accounting-v1'
            then (
            row_accounting_json is null or (
                jsonb_typeof(row_accounting_json) = 'object'
                and row_accounting_json ?& array[
                    'schema_version', 'reader_version', 'reader_path',
                    'detected_row_count', 'accounted_row_count',
                    'extracted_row_count', 'blank_row_count',
                    'skipped_row_count', 'unaccounted_rows', 'rows',
                    'native_mapping', 'field_materialization'
                ]
                and prompt_version = 'matrix_structure_ids_v1'
                and row_accounting_json ->> 'reader_version' = prompt_version
                and row_accounting_json ->> 'reader_path' = 'native_matrix_cells'
                and row_accounting_json ->> 'schema_version' = 'native-matrix-row-accounting-v1'
                and jsonb_typeof(row_accounting_json -> 'rows') = 'array'
                and jsonb_typeof(row_accounting_json -> 'unaccounted_rows') = 'array'
                and jsonb_typeof(row_accounting_json -> 'native_mapping') = 'object'
                and jsonb_typeof(row_accounting_json #> '{native_mapping,pages}') = 'array'
                and row_accounting_json #>> '{native_mapping,identity}' ~ '^[0-9a-f]{64}$'
                and row_accounting_json #>> '{native_mapping,reading_sha256}' ~ '^[0-9a-f]{64}$'
                and jsonb_typeof(row_accounting_json -> 'field_materialization') = 'array'
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.field_materialization[*] ? (
                        @.type() == "object" && @.row_id.type() == "string" && @.row_id != ""
                        && @.local_row_id.type() == "string" && @.local_row_id != ""
                        && @.page.type() == "number" && @.page > 0
                        && @.field.type() == "string" && @.field != ""
                        && (@.status == "materialized" || @.status == "refused" || @.status == "not_extracted")
                        && @.reason.type() == "string" && @.reason != ""
                        && @.value_source_ids.type() == "array" && @.value_source_ids.size() > 0
                        && @.context_source_ids.type() == "array"
                    )'
                )) = jsonb_array_length(row_accounting_json -> 'field_materialization')
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.native_mapping.pages[*].reading.rows[*].fields.keyvalue()'
                )) = jsonb_array_length(row_accounting_json -> 'field_materialization')
                and row_accounting_json ->> 'detected_row_count' ~ '^[0-9]+$'
                and row_accounting_json ->> 'accounted_row_count' ~ '^[0-9]+$'
                and row_accounting_json ->> 'extracted_row_count' ~ '^[0-9]+$'
                and row_accounting_json ->> 'blank_row_count' ~ '^[0-9]+$'
                and row_accounting_json ->> 'skipped_row_count' ~ '^[0-9]+$'
                and jsonb_array_length(row_accounting_json -> 'rows') =
                    (row_accounting_json ->> 'detected_row_count')::integer
                and (row_accounting_json ->> 'accounted_row_count')::integer =
                    (row_accounting_json ->> 'extracted_row_count')::integer +
                    (row_accounting_json ->> 'blank_row_count')::integer +
                    (row_accounting_json ->> 'skipped_row_count')::integer
                and jsonb_array_length(row_accounting_json -> 'unaccounted_rows') =
                    (row_accounting_json ->> 'detected_row_count')::integer -
                    (row_accounting_json ->> 'accounted_row_count')::integer
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.rows[*] ? (@.disposition == "extracted")'
                )) = (row_accounting_json ->> 'extracted_row_count')::integer
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.rows[*] ? (@.disposition == "blank")'
                )) = (row_accounting_json ->> 'blank_row_count')::integer
                and jsonb_array_length(jsonb_path_query_array(
                    row_accounting_json, '$.rows[*] ? (@.disposition == "skipped")'
                )) = (row_accounting_json ->> 'skipped_row_count')::integer
            ) is true
            ) else (
            row_accounting_json is null or (
                jsonb_typeof(row_accounting_json) = 'object'
                and row_accounting_json ->> 'reader_version' = prompt_version
                and (
                    (
                        row_accounting_json ?& array[
                            'schema_version', 'reader_version', 'reader_path',
                            'detected_row_count', 'accounted_row_count',
                            'extracted_row_count', 'blank_row_count',
                            'skipped_row_count', 'unaccounted_rows', 'rows'
                        ]
                        and row_accounting_json ->> 'schema_version' =
                            'matrix-row-accounting-v1'
                        and jsonb_typeof(row_accounting_json -> 'rows') = 'array'
                        and jsonb_typeof(
                            row_accounting_json -> 'unaccounted_rows'
                        ) = 'array'
                        and row_accounting_json ->> 'detected_row_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'accounted_row_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'extracted_row_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'blank_row_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'skipped_row_count' ~ '^[0-9]+$'
                        and jsonb_array_length(row_accounting_json -> 'rows') =
                            (row_accounting_json ->> 'detected_row_count')::integer
                        and (row_accounting_json ->> 'accounted_row_count')::integer =
                            (row_accounting_json ->> 'extracted_row_count')::integer +
                            (row_accounting_json ->> 'blank_row_count')::integer +
                            (row_accounting_json ->> 'skipped_row_count')::integer
                        and jsonb_array_length(
                            row_accounting_json -> 'unaccounted_rows'
                        ) =
                            (row_accounting_json ->> 'detected_row_count')::integer -
                            (row_accounting_json ->> 'accounted_row_count')::integer
                    ) or (
                        row_accounting_json ?& array[
                            'schema_version', 'reader_version', 'reader_path',
                            'document_id', 'detected_segment_count',
                            'read_segment_count', 'proposed_fact_count',
                            'unread_segment_ids',
                            'proposed_subject_candidate_ids',
                            'unproposed_subject_candidate_ids'
                        ]
                        and row_accounting_json ->> 'schema_version' =
                            'prose-segment-accounting-v1'
                        and row_accounting_json ->> 'reader_path' =
                            'prose_interpretation'
                        and row_accounting_json ->> 'document_id' ~ '^[0-9]+$'
                        and (row_accounting_json ->> 'document_id')::bigint = document_id
                        and row_accounting_json ->> 'detected_segment_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'read_segment_count' ~ '^[0-9]+$'
                        and row_accounting_json ->> 'proposed_fact_count' ~ '^[0-9]+$'
                        and jsonb_typeof(
                            row_accounting_json -> 'unread_segment_ids'
                        ) = 'array'
                        and jsonb_typeof(
                            row_accounting_json -> 'proposed_subject_candidate_ids'
                        ) = 'array'
                        and jsonb_typeof(
                            row_accounting_json -> 'unproposed_subject_candidate_ids'
                        ) = 'array'
                        and (row_accounting_json ->> 'read_segment_count')::integer +
                            jsonb_array_length(
                                row_accounting_json -> 'unread_segment_ids'
                            ) =
                            (row_accounting_json ->> 'detected_segment_count')::integer
                    )
                )
            )
            ) end
            ''',
            name="ck_extraction_runs_row_accounting_shape",
        ),
        CheckConstraint(
            '''
            case when prompt_version = 'matrix_structure_ids_v1'
            then (
            outcome <> 'completed' or (
                row_accounting_json is not null
                and row_accounting_json ->> 'schema_version' = 'native-matrix-row-accounting-v1'
                and jsonb_array_length(row_accounting_json -> 'unaccounted_rows') = 0
                and (row_accounting_json ->> 'accounted_row_count')::integer =
                    (row_accounting_json ->> 'detected_row_count')::integer
                and (row_accounting_json ->> 'extracted_row_count')::integer = candidate_count
            ) is true
            ) else (
            not (
                outcome = 'completed'
                and prompt_version in (
                    'sheet_native_v2', 'matrix_tiered_v4',
                    'prose_interpretation_v1'
                )
            ) or (
                row_accounting_json is not null
                and (
                    (
                        row_accounting_json ->> 'schema_version' =
                            'matrix-row-accounting-v1'
                        and jsonb_array_length(
                            row_accounting_json -> 'unaccounted_rows'
                        ) = 0
                        and (row_accounting_json ->> 'accounted_row_count')::integer =
                            (row_accounting_json ->> 'detected_row_count')::integer
                        and (row_accounting_json ->> 'extracted_row_count')::integer =
                            candidate_count
                    ) or (
                        row_accounting_json ->> 'schema_version' =
                            'prose-segment-accounting-v1'
                        and (row_accounting_json ->> 'proposed_fact_count')::integer =
                            candidate_count
                    )
                )
            )
            ) end
            ''',
            name="ck_extraction_runs_completed_row_accounting",
        ),
        Index(
            "ix_extraction_runs_completed_prompt_document",
            "prompt_version",
            "document_id",
            postgresql_where=text("outcome = 'completed' and page_errors = 0"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    prompt_version: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(
        _enum(*EXTRACTION_OUTCOMES, name="extraction_outcome"),
        default="completed",
        server_default="completed",
    )
    candidate_count: Mapped[int] = mapped_column(Integer)
    page_errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    model: Mapped[str | None] = mapped_column(String(64))
    schema_version: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    # The extractor-time Candidate payloads owned by this run. Candidate
    # review state and edited payloads remain mutable; this snapshot does not.
    candidate_inputs_json: Mapped[list | None] = mapped_column(JSONB)
    # Exact bytes and strict request controls are sealed when the extractor
    # starts. Historical rows remain null rather than being reconstructed from
    # whatever source happens to be deployed today.
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    schema_sha256: Mapped[str | None] = mapped_column(String(64))
    postprocessor_sha256: Mapped[str | None] = mapped_column(String(64))
    # Superseded for new writes by extractor_config_sha256 (#605): the sealed
    # receipt lives once in extractor_configurations. Rows written before that
    # registry existed keep their inline copy, which is readable through
    # ``extraction_runs.extractor_configuration``; no bulk rewrite removes it.
    extractor_config_json: Mapped[dict | None] = mapped_column(JSONB)
    extractor_config_sha256: Mapped[str | None] = mapped_column(String(64))
    token_usage_json: Mapped[dict | None] = mapped_column(JSONB)
    row_accounting_json: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ActiveExtractionRun(Base):
    """The explicitly declared run whose Candidates are operative for a document."""

    __tablename__ = "active_extraction_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
        ),
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id"), primary_key=True
    )
    extraction_run_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    declared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ActiveRunDeclaration(Base):
    """One appended human act declaring a document's Active Run.

    The chain is explicit: every declaration names its predecessor, exactly
    one root exists per document, and the current declaration is the one no
    later declaration has superseded — a chain fact, never an id or
    timestamp order (ADR-0019). ``active_extraction_runs`` remains the
    one-row projection every reader joins; this table is why that row is
    what it is. History is immutable below the service boundary.
    """

    __tablename__ = "active_run_declarations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
        ),
        # The predecessor must be a declaration for the same document, which
        # needs the (document_id, id) identity to exist as an FK target.
        UniqueConstraint(
            "document_id", "id", name="uq_active_run_declarations_document_id_id"
        ),
        ForeignKeyConstraint(
            ["document_id", "predecessor_declaration_id"],
            ["active_run_declarations.document_id", "active_run_declarations.id"],
            name="fk_active_run_declarations_predecessor",
        ),
        # A declaration is superseded at most once, and a document has at
        # most one root: together they make the history one linear chain.
        UniqueConstraint(
            "predecessor_declaration_id",
            name="uq_active_run_declarations_predecessor",
        ),
        Index(
            "uq_active_run_declarations_one_root",
            "document_id",
            unique=True,
            postgresql_where=text("predecessor_declaration_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    extraction_run_id: Mapped[int] = mapped_column(BigInteger)
    declared_by: Mapped[str] = mapped_column(String(128))
    declared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    predecessor_declaration_id: Mapped[int | None] = mapped_column(BigInteger)


class RevisionComparisonRun(Base):
    """An immutable receipt for comparing two exact Extraction Runs.

    Input snapshots live on the receipt because a Candidate's review state and
    edited payload may change later.  A reviewer reading this row must still
    see exactly what the matcher saw when it produced its findings.
    """

    __tablename__ = "revision_comparison_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "predecessor_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_revision_comparison_predecessor_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "successor_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_revision_comparison_successor_project",
        ),
        ForeignKeyConstraint(
            ["predecessor_document_id", "predecessor_extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
            name="fk_revision_comparison_predecessor_run",
        ),
        ForeignKeyConstraint(
            ["successor_document_id", "successor_extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
            name="fk_revision_comparison_successor_run",
        ),
        CheckConstraint(
            "predecessor_document_id <> successor_document_id",
            name="ck_revision_comparison_distinct_documents",
        ),
        CheckConstraint(
            "finding_count >= 0", name="ck_revision_comparison_finding_count"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    predecessor_document_id: Mapped[int] = mapped_column(BigInteger)
    successor_document_id: Mapped[int] = mapped_column(BigInteger)
    predecessor_extraction_run_id: Mapped[int] = mapped_column(BigInteger)
    successor_extraction_run_id: Mapped[int] = mapped_column(BigInteger)
    predecessor_schema_version: Mapped[str | None] = mapped_column(String(64))
    successor_schema_version: Mapped[str | None] = mapped_column(String(64))
    predecessor_prompt_version: Mapped[str] = mapped_column(String(64))
    successor_prompt_version: Mapped[str] = mapped_column(String(64))
    predecessor_model: Mapped[str | None] = mapped_column(String(64))
    successor_model: Mapped[str | None] = mapped_column(String(64))
    matcher_version: Mapped[str] = mapped_column(String(64))
    matcher_config: Mapped[dict] = mapped_column(JSONB)
    predecessor_inputs_json: Mapped[list] = mapped_column(JSONB)
    successor_inputs_json: Mapped[list] = mapped_column(JSONB)
    finding_count: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Null only while the service inserts this receipt's findings inside the
    # creating transaction.  The database permits exactly one transition to
    # a value, after the stored count is exact, and rejects commit while null.
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RevisionComparisonFinding(Base):
    """One partition of rows in a Revision Comparison receipt.

    Most findings name one row on either side.  ``ambiguous`` deliberately
    permits sets on both sides so uncertainty is persisted rather than forced
    into a one-to-one claim the evidence cannot support. ``unmatched`` is a
    side-specific row for which incomplete identity prevents an honest
    added/dropped conclusion.
    """

    __tablename__ = "revision_comparison_findings"
    __table_args__ = (
        UniqueConstraint("revision_comparison_run_id", "ordinal"),
        CheckConstraint(
            "match_score is null or (match_score >= 0 and match_score <= 1)",
            name="ck_revision_comparison_match_score",
        ),
        CheckConstraint(
            "(state = 'added' and cardinality(predecessor_candidate_ids) = 0 "
            "and cardinality(successor_candidate_ids) = 1) or "
            "(state = 'dropped' and cardinality(predecessor_candidate_ids) = 1 "
            "and cardinality(successor_candidate_ids) = 0) or "
            "(state = 'unmatched' and "
            "((cardinality(predecessor_candidate_ids) = 1 "
            "and cardinality(successor_candidate_ids) = 0) or "
            "(cardinality(predecessor_candidate_ids) = 0 "
            "and cardinality(successor_candidate_ids) = 1))) or "
            "(state in ('unchanged', 'changed') "
            "and cardinality(predecessor_candidate_ids) = 1 "
            "and cardinality(successor_candidate_ids) = 1) or "
            "(state = 'ambiguous' "
            "and cardinality(predecessor_candidate_ids) > 0 "
            "and cardinality(successor_candidate_ids) > 0)",
            name="ck_revision_comparison_finding_shape",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_comparison_run_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(
        _enum(*REVISION_COMPARISON_STATES, name="revision_comparison_state")
    )
    predecessor_candidate_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    successor_candidate_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    match_score: Mapped[float | None] = mapped_column(Float)
    field_changes: Mapped[list] = mapped_column(JSONB)
    matcher_detail: Mapped[dict] = mapped_column(JSONB)


class UnreadableCellReadingProfile(Base):
    """One declared, versioned eligibility profile for the second-read harness.

    ADR-0064: a page enters the reading harness only under a named profile with
    declared deterministic OCR-quality checks, page scope, tool and model
    identities, and per-cell/per-page budgets — declared before it can run,
    refused visibly when missing. Append-only: a changed bound is a new
    attributable declaration, never an edit of an earlier one, so a receipt can
    always name the exact profile it ran under. The harness cannot widen its own
    scope: eligibility is computed by Corridor from these declared checks.
    """

    __tablename__ = "unreadable_cell_reading_profiles"
    __table_args__ = (
        CheckConstraint(
            "length(trim(profile_version)) > 0",
            name="ck_unreadable_cell_profile_version",
        ),
        CheckConstraint(
            "min_readable_text_chars between 1 and 100000",
            name="ck_unreadable_cell_profile_min_chars",
        ),
        CheckConstraint(
            "max_cells_per_page between 1 and 10000",
            name="ck_unreadable_cell_profile_max_cells",
        ),
        CheckConstraint(
            "max_image_ops_per_cell between 1 and 100",
            name="ck_unreadable_cell_profile_max_image_ops",
        ),
        CheckConstraint(
            "max_reads_per_cell between 1 and 100",
            name="ck_unreadable_cell_profile_max_reads",
        ),
        CheckConstraint(
            "max_corpus_reads_per_cell between 1 and 100",
            name="ck_unreadable_cell_profile_max_corpus_reads",
        ),
        CheckConstraint(
            "timeout_seconds between 1 and 600",
            name="ck_unreadable_cell_profile_timeout",
        ),
        CheckConstraint(
            "jsonb_typeof(page_scope_json) = 'array'",
            name="ck_unreadable_cell_profile_page_scope",
        ),
        CheckConstraint(
            "jsonb_typeof(image_op_identities_json) = 'array'",
            name="ck_unreadable_cell_profile_image_ops",
        ),
        CheckConstraint(
            "jsonb_typeof(read_identities_json) = 'array'",
            name="ck_unreadable_cell_profile_reads",
        ),
        CheckConstraint(
            "length(trim(created_by)) > 0",
            name="ck_unreadable_cell_profile_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    profile_version: Mapped[str] = mapped_column(String(64))
    # The deterministic OCR-quality check: a page whose usable text falls short
    # of this and whose text was never generated from cells is unreadable.
    min_readable_text_chars: Mapped[int] = mapped_column(Integer)
    page_scope_json: Mapped[list] = mapped_column(JSONB)
    image_op_identities_json: Mapped[list] = mapped_column(JSONB)
    read_identities_json: Mapped[list] = mapped_column(JSONB)
    max_cells_per_page: Mapped[int] = mapped_column(Integer)
    max_image_ops_per_cell: Mapped[int] = mapped_column(Integer)
    max_reads_per_cell: Mapped[int] = mapped_column(Integer)
    max_corpus_reads_per_cell: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UnreadableCellReadingRun(Base):
    """One immutable, non-authoritative attempt to read one unreadable cell.

    Every attempt — a mechanical page rescue, a bounded reading-harness run, or a
    refusal before either — leaves exactly one receipt. ``terminal_state`` says
    what happened; ``page_image_sha256`` pins the exact rendition the run read, so
    a page image that changed mid-run is caught as ``stale_input``. The receipt is
    never Ledger authority: model or OCR reads are candidate generation only.
    """

    __tablename__ = "unreadable_cell_reading_runs"
    __table_args__ = (
        CheckConstraint(
            "terminal_state in ('rescued', 'corroborated', 'reading_only', "
            "'failure', 'stale_input', 'budget_exhausted', 'refused', "
            "'validation_refused', 'runtime_failure')",
            name="ck_unreadable_cell_run_state",
        ),
        CheckConstraint(
            "page_image_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_unreadable_cell_run_image_sha",
        ),
        CheckConstraint(
            "read_fingerprint is null or read_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_unreadable_cell_run_fingerprint",
        ),
        CheckConstraint(
            "non_authoritative", name="ck_unreadable_cell_run_non_auth"
        ),
        CheckConstraint(
            "length(trim(cell_key)) > 0", name="ck_unreadable_cell_run_cell_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    cell_key: Mapped[str] = mapped_column(String(128))
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("unreadable_cell_reading_profiles.id")
    )
    profile_version: Mapped[str | None] = mapped_column(String(64))
    page_image_sha256: Mapped[str] = mapped_column(String(64))
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
    terminal_state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(160))
    outcome_json: Mapped[dict | None] = mapped_column(JSONB)
    validator_outcome: Mapped[str] = mapped_column(String(32))
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UnreadableCellReadingStep(Base):
    """Redacted ordered receipt of one image op, read, or corpus read."""

    __tablename__ = "unreadable_cell_reading_steps"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "ordinal", name="uq_unreadable_cell_step_ordinal"
        ),
        CheckConstraint(
            "step_type in ('image_op', 'read', 'corpus_read')",
            name="ck_unreadable_cell_step_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("unreadable_cell_reading_runs.id"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(128))
    arguments_json: Mapped[dict] = mapped_column(JSONB)
    result_summary_json: Mapped[dict] = mapped_column(JSONB)
    request_sha256: Mapped[str] = mapped_column(String(64))
    result_sha256: Mapped[str] = mapped_column(String(64))


class ScannedPageObservation(Base):
    """One processing observation of one page by the OCR provider (#809).

    The row is the ``analyze_page`` binding ingest consumed for a page of a
    Document rendition, as references rather than copies: the authorization
    record by id; the cache scope by digest (the request boundary, operation
    and feature types, request configuration and adapter and rasterizer
    identities — the reader/configuration identity); the submitted raster, the
    raw response exactly as retained and the normalized reading, each by
    digest; and the model version and request id the provider reported. Its
    identity is those references together, so a page re-read out of the same
    retained response converges on the row it already has and a different
    response, raster, scope or record is a new observation beside it.

    It exists because the observation used to live only in Class B files —
    the adapter's identity file, the raw OCR receipt, the cost receipt — which
    retention deletes; a resolution could name none of them. An Extraction
    Run foreign key was rejected: no run exists on the scanned route (ingest
    hands the adapter the source digest under that name), and a placeholder
    run to satisfy a key would make the run table lie. The token layer
    manifest was rejected as the owner because it is de-duplicated on layer
    content and carries neither the raster, the scope nor the record.
    """

    __tablename__ = "scanned_page_observations"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "page_no",
            "authorization_record_id",
            "scope_digest",
            "raster_sha256",
            "raw_response_sha256",
            "reading_sha256",
            name="uq_scanned_page_observation_identity",
        ),
        CheckConstraint("page_no > 0", name="ck_scanned_page_observation_page"),
        CheckConstraint(
            "length(btrim(authorization_record_id)) > 0",
            name="ck_scanned_page_observation_record",
        ),
        CheckConstraint(
            "rendition_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_scanned_page_observation_rendition",
        ),
        CheckConstraint(
            "scope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_scanned_page_observation_scope",
        ),
        CheckConstraint(
            "raster_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_scanned_page_observation_raster",
        ),
        CheckConstraint(
            "raw_response_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_scanned_page_observation_response",
        ),
        CheckConstraint(
            "reading_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_scanned_page_observation_reading",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    rendition_sha256: Mapped[str] = mapped_column(String(64))
    page_no: Mapped[int] = mapped_column(Integer)
    authorization_record_id: Mapped[str] = mapped_column(String(128))
    scope_digest: Mapped[str] = mapped_column(String(64))
    raster_sha256: Mapped[str] = mapped_column(String(64))
    raw_response_sha256: Mapped[str] = mapped_column(String(64))
    reading_sha256: Mapped[str] = mapped_column(String(64))
    provider_model_version: Mapped[str | None] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# The one reason a scanned reading may carry no observation: it was written
# before the binding existed (#809), and nothing retained binds it to one.
PREDATES_OBSERVATION_BINDING = "predates_observation_binding"


class UnreadableCellResolution(Base):
    """Append-only three-state value for one unreadable cell (ADR-0064).

    The current value of a cell is the latest row for its
    ``(project, document, page, cell_key)``. ``state`` is one of: ``unconfirmed``
    (a reading with full provenance but no corroboration — flagged, never Ready),
    ``corroborated`` (the value is literal text on a readable source, whose
    citation is verified by code — never by model agreement), ``absent`` (the
    source genuinely has no value), or ``admitted`` (a corroborated value the
    gated cross-document admission class has promoted to record-contributing).
    ``origin`` says which pass appended it; a corroboration that arrives later
    upgrades an ``unconfirmed`` row automatically without any human step.

    What read the cell is one of two things, never both: ``run_id`` names the
    reading harness's run, and ``observation_id`` names the provider
    observation a scanned reading was read out of (#809), with
    ``source_region_id`` the routed region the cell fell in. A corroboration
    or admission appended over a row carries that row's observation forward,
    because it corroborates *that* observation's value; a new observation of
    the same cell key is a new ``unconfirmed`` row bound to its own
    observation, and inherits nothing. A scanned reading written before the
    binding existed carries ``observation_unbound_reason`` instead of a guess.
    """

    __tablename__ = "unreadable_cell_resolutions"
    __table_args__ = (
        Index(
            "ix_unreadable_cell_resolutions_cell",
            "project_id",
            "document_id",
            "page_no",
            "cell_key",
        ),
        CheckConstraint(
            "state in ('unconfirmed', 'corroborated', 'absent', 'admitted')",
            name="ck_unreadable_cell_resolution_state",
        ),
        CheckConstraint(
            "origin in ('harness', 'corroboration_upgrade', 'admission', "
            "'human_decision')",
            name="ck_unreadable_cell_resolution_origin",
        ),
        CheckConstraint(
            "policy_sha256 is null or policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_unreadable_cell_resolution_sha",
        ),
        CheckConstraint(
            "length(trim(cell_key)) > 0",
            name="ck_unreadable_cell_resolution_cell_key",
        ),
        CheckConstraint(
            "(run_id is null or observation_id is null) "
            "and (observation_id is null or observation_unbound_reason is null) "
            "and (observation_unbound_reason is null "
            f"or observation_unbound_reason = '{PREDATES_OBSERVATION_BINDING}') "
            "and (origin <> 'harness' or run_id is not null "
            "or observation_id is not null "
            "or observation_unbound_reason is not null)",
            name="ck_unreadable_cell_resolution_observation",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    cell_key: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24))
    value: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("unreadable_cell_reading_runs.id")
    )
    observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("scanned_page_observations.id"), index=True
    )
    source_region_id: Mapped[str | None] = mapped_column(String(64))
    observation_unbound_reason: Mapped[str | None] = mapped_column(String(48))
    corroboration_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id")
    )
    corroboration_page_no: Mapped[int | None] = mapped_column(Integer)
    corroboration_quote: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str | None] = mapped_column(String(64))
    policy_sha256: Mapped[str | None] = mapped_column(String(64))
    # Set only for an origin='human_decision' row; the answer key for the
    # ADR-0050 replay. Null for every machine origin.
    recorded_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
