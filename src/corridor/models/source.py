"""Registered sources and their pages: what a source is and how it was read.

A Document is a registered source; its pages carry the text and the image a
citation verifies against, and its renditions record which engine produced
that text. Recorded verbal origin is here rather than in the spine because a
statement made in a meeting still needs a source to cite, and the origin row is
that source. Quarantine and page-processing failures are recorded rather than
raised: a source that cannot be read is a fact about the project, not an
exception the pipeline swallows.
"""

from datetime import date, datetime
from hashlib import sha256

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base, DOC_TYPES, PARSE_STATUSES, TEXT_SOURCES, _enum


__all__ = [
    "DocPage",
    "Document",
    "DocumentQuarantine",
    "DocumentRenditionDerivation",
    "PageProcessingFailure",
    "PageRenderDerivative",
    "RecordedVerbalOrigin",
    "RecordedVerbalOriginBackfillReceipt",
    "RecordedVerbalOriginFactDigest",
    "RecordedVerbalOriginStatement",
    "TokenLayerManifest",
]


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("project_id", "sha256"),
        UniqueConstraint("project_id", "id", name="uq_documents_project_id_id"),
        UniqueConstraint(
            "project_id", "registry_id", name="uq_documents_project_registry_id"
        ),
        ForeignKeyConstraint(
            ["project_id", "superseded_by"],
            ["documents.project_id", "documents.id"],
            name="fk_documents_superseded_by_same_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "supersession_source_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_documents_supersession_source_same_project",
        ),
        ForeignKeyConstraint(
            ["supersession_source_document_id", "supersession_source_page"],
            ["doc_pages.document_id", "doc_pages.page_no"],
            name="fk_documents_supersession_source_page",
            use_alter=True,
        ),
        CheckConstraint(
            "superseded_by is null or superseded_by <> id",
            name="ck_documents_no_self_supersession",
        ),
        # A document cannot be the authority for its own replacement: the
        # replacement postdates it, so the page a reader would check to
        # confirm the edge predates the fact (ADR-0015). The successor is
        # deliberately still lawful — a revision stating what it replaces
        # is an ordinary way agencies declare a chain.
        CheckConstraint(
            "supersession_source_document_id is null "
            "or supersession_source_document_id <> id",
            name="ck_documents_no_self_attested_supersession",
        ),
        ForeignKeyConstraint(
            ["source_delivery_id", "project_id"],
            ["source_deliveries.id", "source_deliveries.project_id"],
            name="fk_documents_source_delivery",
        ),
        CheckConstraint(
            "numbering_scheme in ('project-unique', 'per-party')",
            name="ck_documents_numbering_scheme",
        ),
        CheckConstraint(
            "(superseded_by is null and superseded_on is null "
            "and supersession_source_document_id is null "
            "and supersession_source_page is null) or "
            "(superseded_by is not null and registry_id is not null "
            "and superseded_on is not null "
            "and supersession_source_document_id is not null "
            "and supersession_source_page is not null "
            "and supersession_source_page > 0)",
            name="ck_documents_complete_supersession",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # A human-curated corpus identifier. Database ids remain the internal
    # identity, while this stable name lets a manifest declare edges before
    # ingest has assigned database ids. Once set it is immutable at the
    # database boundary, because declared supersession provenance names
    # documents by this id. It is nullable for legacy and ad-hoc documents
    # that are not participants in declared registry relations.
    registry_id: Mapped[str | None] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(Text)
    doc_type: Mapped[str] = mapped_column(_enum(*DOC_TYPES, name="doc_type"))
    # How this matrix names its rows (ADR-0030) — declared registry
    # metadata, meaningful for `doc_type == "matrix"` and left at its
    # default elsewhere. Identity is derived under it in one place,
    # corridor.identity, and never guessed from repeated numbers.
    numbering_scheme: Mapped[str] = mapped_column(
        String(32),
        default="project-unique",
        server_default="project-unique",
    )
    # Corpus provenance. A citation that bottoms out at "a file on my
    # laptop" is not a citation.
    source_url: Mapped[str | None] = mapped_column(Text)
    # The ledger row of the delivery that carried these bytes in (#675).
    # Nullable because the corpus path registers documents that arrived
    # through no transport at all, and because a coverage reading must be
    # able to say "this source dereferences no delivery" rather than guess
    # one. It is the only link a Proposed Delta has to the append-only
    # Source Delivery boundary its issue was confirmed against, reached
    # through the delta's own group.
    source_delivery_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doc_date: Mapped[date | None] = mapped_column(Date)
    pages: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(
        _enum(*PARSE_STATUSES, name="parse_status"),
        default="pending",
        server_default="pending",
    )
    # How this document was read: pages per extraction tier, and printed
    # headers it read more than one way before majority resolution (#101).
    # Both were set as ad-hoc attributes on this object by the extractor
    # and read back with `getattr` defaults, so neither survived the run
    # that produced them — a resumed run reported no fallback at all, in a
    # pipeline whose own comment says "a fallback nobody counts is a
    # fallback nobody notices".
    extraction_tiers: Mapped[dict | None] = mapped_column(JSONB)
    header_disagreements: Mapped[int | None] = mapped_column(Integer)
    # Registry metadata, never inferred from dates, filenames, retrieval
    # order, or similarity (ADR-0015). A database trigger requires every
    # successor and source document named here to be registered already, and
    # the source pointer names the page of that registered index that
    # declared the authority's replacement date.
    superseded_by: Mapped[int | None] = mapped_column(BigInteger)
    superseded_on: Mapped[date | None] = mapped_column(Date)
    supersession_source_document_id: Mapped[int | None] = mapped_column(BigInteger)
    supersession_source_page: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RecordedVerbalOrigin(Base):
    """One recorder's attestation: the spine-native origin of a verbal (#512).

    ADR-0074 gave the ``recorded_verbal_statement`` segment a ``statement_id``
    foreign key to ``dependency_events``, which made the evidence spine depend
    on a legacy Project Record aggregate.  ADR-0081 stage 1 demotes that key to
    lineage and puts the identity here instead: the project, the named recorder,
    when the recording was made, the day of the conversation, the exact words
    and their digest, the correction chain, and a stable identifier of its own.
    A Recorded Verbal Statement's origin is a recorder's attestation, not a
    document proposal (ADR-0082), so there are no bytes to dereference and the
    digest is what the attestation certifies.

    ``recorded_at`` is the attestation time supplied by the writer, never a
    server clock this row reads for itself: the backfill preserves the original
    legacy recording time, and ``created_at`` records separately when the row
    was appended.

    ``corrects_origin_id`` is the re-attestation chain — one origin corrects at
    most one predecessor, and is corrected by at most one successor.  A legacy
    ``supersedes_event_id`` is deliberately *not* mapped into it: a later
    attributable timing is a Change to Promised Timing, a new statement, not a
    correction of an earlier attestation (ADR-0036).
    """

    __tablename__ = "recorded_verbal_origins"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_recorded_verbal_origins_project_id"
        ),
        UniqueConstraint(
            "corrects_origin_id", name="uq_recorded_verbal_origins_corrects"
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_recorded_verbal_origins_recorder",
        ),
        CheckConstraint(
            "length(exact_text) > 0", name="ck_recorded_verbal_origins_exact_text"
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_recorded_verbal_origins_content_sha256",
        ),
        CheckConstraint(
            "corrects_origin_id is null or corrects_origin_id <> id",
            name="ck_recorded_verbal_origins_corrects_other",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    recorded_by: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    conversation_date: Mapped[date | None] = mapped_column(Date)
    exact_text: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    corrects_origin_id: Mapped[int | None] = mapped_column(
        ForeignKey("recorded_verbal_origins.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RecordedVerbalOriginStatement(Base):
    """The temporary legacy mapping of one origin to its statement row (#512).

    The legacy foreign key lives only here.  No target table and no identity
    digest references ``dependency_events`` any more (ADR-0081 stage 1); the
    dual-write of stages 1 through 5 still needs the legacy row, and the one
    reader that resolves a legacy statement to its spine segment joins through
    this mapping.  The relation is one-to-one in both directions, so a legacy
    statement can never acquire a second origin, and it retires whole with the
    dual-write at ADR-0081 stage 6.
    """

    __tablename__ = "recorded_verbal_origin_statements"
    __table_args__ = (
        UniqueConstraint(
            "statement_id", name="uq_recorded_verbal_origin_statements_statement"
        ),
    )

    origin_id: Mapped[int] = mapped_column(
        ForeignKey("recorded_verbal_origins.id"), primary_key=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RecordedVerbalOriginBackfillReceipt(Base):
    """One attributable receipt per verbal the #512 backfill reconciled.

    Exactly one row per dual-written verbal, and every one of its three
    references is unique, so the reconciliation is one-to-one by construction:
    a dropped, duplicated, or re-pointed row cannot be represented.  The
    executor is recorded separately from the recorder the origin preserves, so
    the migration never becomes the semantic author of the attestation.

    ``legacy_statement_id`` is deliberately a plain identifier rather than a
    foreign key: the receipt is the permanent record of what this transition
    did, and it must stay readable after ADR-0081 stage 6 retires the legacy
    table.  The live key lives on the compatibility mapping, which retires
    with it.
    """

    __tablename__ = "recorded_verbal_origin_backfill_receipts"
    __table_args__ = (
        UniqueConstraint(
            "origin_id", name="uq_recorded_verbal_backfill_receipts_origin"
        ),
        UniqueConstraint(
            "legacy_statement_id",
            name="uq_recorded_verbal_backfill_receipts_statement",
        ),
        UniqueConstraint(
            "source_segment_id",
            name="uq_recorded_verbal_backfill_receipts_segment",
        ),
        CheckConstraint(
            "length(trim(executed_by)) > 0",
            name="ck_recorded_verbal_backfill_receipts_executor",
        ),
        CheckConstraint(
            "fact_count >= 0", name="ck_recorded_verbal_backfill_receipts_fact_count"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    origin_id: Mapped[int] = mapped_column(ForeignKey("recorded_verbal_origins.id"))
    legacy_statement_id: Mapped[int] = mapped_column(BigInteger)
    source_segment_id: Mapped[int] = mapped_column(ForeignKey("source_segments.id"))
    fact_count: Mapped[int] = mapped_column(Integer)
    migration_revision: Mapped[str] = mapped_column(String(32))
    executed_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RecordedVerbalOriginFactDigest(Base):
    """The exact Fact digest the #512 backfill replaced, and what it became.

    Removing the legacy statement from the Fact identity changes the digest of
    every Fact a dual-written verbal already carried.  The backfill first
    reproduces the stored digest from the stored row; only a digest it has
    reproduced exactly is replaced, and both values are recorded here, so the
    change is attributable and reversible rather than silent.
    """

    __tablename__ = "recorded_verbal_origin_fact_digests"
    __table_args__ = (
        UniqueConstraint(
            "fact_id", name="uq_recorded_verbal_origin_fact_digests_fact"
        ),
        CheckConstraint(
            "prior_content_sha256 ~ '^[0-9a-f]{64}$' "
            "and content_sha256 ~ '^[0-9a-f]{64}$' "
            "and prior_content_sha256 <> content_sha256",
            name="ck_recorded_verbal_origin_fact_digests_change",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("recorded_verbal_origin_backfill_receipts.id"), index=True
    )
    fact_id: Mapped[int] = mapped_column(ForeignKey("facts.id"))
    prior_content_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))


class DocumentRenditionDerivation(Base):
    """One retained format conversion, without equivalence or Supersession."""

    __tablename__ = "document_rendition_derivations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "source_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_rendition_derivation_source_same_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "derived_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_rendition_derivation_derived_same_project",
        ),
        CheckConstraint(
            "source_document_id <> derived_document_id",
            name="ck_rendition_derivation_distinct_documents",
        ),
        CheckConstraint(
            "kind = 'format_conversion' and source_format = 'xls' "
            "and derived_format = 'xlsx'",
            name="ck_rendition_derivation_kind",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' and "
            "derived_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rendition_derivation_hashes",
        ),
        CheckConstraint(
            "length(trim(tool)) > 0 and length(trim(tool_version)) > 0",
            name="ck_rendition_derivation_tool",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_document_id: Mapped[int] = mapped_column(BigInteger)
    derived_document_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    source_format: Mapped[str] = mapped_column(String(16))
    derived_format: Mapped[str] = mapped_column(String(16))
    source_sha256: Mapped[str] = mapped_column(String(64))
    derived_sha256: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentQuarantine(Base):
    """One recorded restriction on what may be done with one registered document.

    A document whose relationship semantics Corridor does not model — a
    Utility Work Schedule's Dependent Activity chain (#149) — is registered,
    visible, and deliberately not interpreted. This row is why: durable,
    queryable, and never only in an operator's memory or a process's logs.

    **Each row names the processing stage it prohibits (#919).** Either
    document reading is prohibited — no ordinary rich parsing, rendering, OCR
    or downstream extraction — or semantic extraction alone is, in which case
    authorized, bounded reading and Source Segment creation may proceed. The
    row used to be keyed by ``document_id`` with one free-text ``reason``, so a
    second restriction could only be recorded by overwriting the first and a
    mapping repair could silently clear a safety finding. Several independent
    restrictions now coexist as several rows, and the effective permission is
    their intersection.

    The row is append-only, and PostgreSQL enforces it: the one change a hold
    accepts is the attributable release below, written once, with the evidence
    that removes the restriction's cause. ``corridor.processing_holds`` is the
    one module that reads and writes this relation — no caller builds the row
    itself, because the stage, the authority permitted to impose it and the
    evidence it must carry are that module's rules.
    """

    __tablename__ = "document_quarantines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    # Which processing stage this restriction prohibits, in the maintainer's
    # own two boundaries: ``document_reading`` or ``semantic_extraction``.
    prohibited_stage: Mapped[str] = mapped_column(String(32))
    # Why, machine-readably. The explanatory ``reason`` below is for people and
    # is never parsed to recover a permission.
    reason_code: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    imposed_by_authority: Mapped[str] = mapped_column(String(32))
    imposed_by: Mapped[str] = mapped_column(String(128))
    evidence: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    released_by_authority: Mapped[str | None] = mapped_column(
        String(32), default=None
    )
    released_by: Mapped[str | None] = mapped_column(String(128), default=None)
    release_evidence: Mapped[str | None] = mapped_column(Text, default=None)


class DocPage(Base):
    __tablename__ = "doc_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_no"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    # 1-based, because citations are written for humans: [D12 p.4] must mean
    # the page a reader sees, not an array index.
    page_no: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # Evidence display needs the rendered page as well as its text: a quote
    # is checked against the text and shown against the image.
    image_path: Mapped[str | None] = mapped_column(Text)
    # OCR output is materially less reliable than a real text layer, so a
    # citation resting on it deserves to be visibly different rather than
    # indistinguishable from one read straight out of the PDF.
    text_source: Mapped[str] = mapped_column(
        _enum(*TEXT_SOURCES, name="text_source"),
        default="text_layer",
        server_default="text_layer",
    )
    # Rebuildable Class B processing evidence (ADRs 0068/0072). Existing
    # pages predate the inventory and remain readable through text_source;
    # every production PDF ingest now writes both fields together.
    inventory_json: Mapped[dict | None] = mapped_column(JSONB)
    routing_json: Mapped[dict | None] = mapped_column(JSONB)


class PageProcessingFailure(Base):
    """A retained OCR attempt that did not complete its page-region contract."""

    __tablename__ = "page_processing_failures"
    __table_args__ = (
        CheckConstraint("page_number > 0"),
        CheckConstraint("length(engine) > 0"),
        CheckConstraint("length(region_id) > 0"),
        CheckConstraint("length(error_type) > 0"),
        CheckConstraint("length(error_message) > 0"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The Document and human page number survive a reparse; DocPage rows are
    # rebuildable and replaced during recovery. Stable ownership preserves a
    # failed attempt after a later successful retry.
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    engine: Mapped[str] = mapped_column(String(64))
    configuration_json: Mapped[dict] = mapped_column(JSONB)
    region_id: Mapped[str] = mapped_column(String(64))
    scope_json: Mapped[dict] = mapped_column(JSONB)
    error_type: Mapped[str] = mapped_column(String(160))
    error_message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PageRenderDerivative(Base):
    """One regenerable, profile-bound page or region render (ADR-0072 Class B)."""

    __tablename__ = "page_render_derivatives"
    __table_args__ = (
        UniqueConstraint("derivative_key"),
        CheckConstraint("page_number > 0"),
        CheckConstraint("length(profile_name) > 0"),
        CheckConstraint("length(profile_id) > 0"),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("artifact_bytes > 0"),
        CheckConstraint("retention_class = 'intermediary_processing'"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    derivative_key: Mapped[str] = mapped_column(String(64))
    profile_name: Mapped[str] = mapped_column(String(32))
    profile_id: Mapped[str] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    artifact_path: Mapped[str] = mapped_column(Text)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    artifact_bytes: Mapped[int] = mapped_column(BigInteger)
    manifest_json: Mapped[dict] = mapped_column(JSONB)
    retention_class: Mapped[str] = mapped_column(
        String(32), server_default="intermediary_processing"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TokenLayerManifest(Base):
    """One native or OCR token layer's manifest (ADR-0073, Class B).

    The positioned tokens live in a content-addressed JSON artifact; this row
    records the engine identity, page, origin, token count, quality summary,
    and artifact digest. The artifact is registered as a Class B
    ProcessingArtifact, so the retention TTL, reachability check, and holds
    apply to token layers unchanged — and never to a promoted citation.
    """

    __tablename__ = "token_layers"
    __table_args__ = (
        UniqueConstraint("layer_key"),
        CheckConstraint("page_no > 0"),
        CheckConstraint("origin in ('native', 'ocr')"),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("artifact_bytes > 0"),
        CheckConstraint("token_count >= 0"),
        CheckConstraint("retention_class = 'intermediary_processing'"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(8))
    layer_key: Mapped[str] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    engine_json: Mapped[dict] = mapped_column(JSONB)
    token_count: Mapped[int] = mapped_column(Integer)
    quality_json: Mapped[dict] = mapped_column(JSONB)
    artifact_path: Mapped[str] = mapped_column(Text)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    artifact_bytes: Mapped[int] = mapped_column(BigInteger)
    retention_class: Mapped[str] = mapped_column(
        String(32), server_default="intermediary_processing"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
