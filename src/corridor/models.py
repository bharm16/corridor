"""Schema for the walking skeleton.

Three points differ from a naive reading of the spec and are easy to get
wrong, so they are called out here as well as in the ADRs:

- `Dependency.status` has no `ready` value. Readiness is computed from a
  verified `EvidenceLink` marked `satisfies_requirement` (ADR-0002).
- `Assertion` is a table, not a column. A ledger field value is an
  adjudicated conclusion; the assertions beneath it preserve what each
  source actually claimed (ADR-0001).
- `EvidenceLink.verified` means exactly one thing: the quote appears on
  the cited page. It says nothing about whether the claim is true.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DOC_TYPES = (
    "matrix",
    "minutes",
    "agreement",
    "email",
    "plan",
    "schedule",
    "spec",
    "status_report",
    "other",
)
PARSE_STATUSES = ("pending", "parsed", "failed")
TEXT_SOURCES = ("text_layer", "ocr")
DEP_TYPES = (
    "utility_relocation",
    "agreement",
    "permit",
    "row",
    "railroad",
    "access",
    "other",
)
DEP_STATUSES = ("identified", "in_progress", "committed", "blocked", "closed")
CRITICALITIES = ("critical", "high", "normal")
CANDIDATE_KINDS = ("dependency", "event")
CANDIDATE_STATES = ("pending", "accepted", "merged", "rejected")
ORG_TYPES = ("utility", "railroad", "agency", "consultant", "other")
EVENT_TYPES = (
    "commitment",
    "response",
    "slip",
    "escalation",
    "status_change",
    "closure",
)


def _enum(*values: str, name: str) -> Enum:
    """A VARCHAR plus a CHECK, not a native PG type.

    These value sets are still moving — `ready` was removed from
    DEP_STATUSES during design, and `status_report` was added to DOC_TYPES
    once the corpus research found serial reporting. Native enums make
    every such change an ALTER TYPE; a CHECK is a one-line migration.

    `create_constraint` must be passed explicitly: it has defaulted to
    False since SQLAlchemy 1.4, so omitting it yields a bare VARCHAR that
    accepts any string at all, with the enum enforced only in Python.
    """
    return Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Text)
    agency: Mapped[str | None] = mapped_column(Text)
    # Structural enforcement of the no-synthetic-samples rule: `make eval`
    # refuses to run against a project with this set, so the boundary
    # cannot be crossed by forgetting about it.
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("project_id", "sha256"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(Text)
    doc_type: Mapped[str] = mapped_column(_enum(*DOC_TYPES, name="doc_type"))
    # Corpus provenance. A citation that bottoms out at "a file on my
    # laptop" is not a citation.
    source_url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doc_date: Mapped[date | None] = mapped_column(Date)
    pages: Mapped[int | None] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(
        _enum(*PARSE_STATUSES, name="parse_status"),
        default="pending",
        server_default="pending",
    )
    # Always null in v0; supersession lands in M8.
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExternalOrg(Base):
    __tablename__ = "external_orgs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    org_type: Mapped[str] = mapped_column(
        _enum(*ORG_TYPES, name="org_type"), default="utility", server_default="utility"
    )
    # One party is named many ways across documents — "AT&T", "AT&T Texas
    # (SWBT)", "Southwestern Bell". Merge ranking blocks on the resolved
    # party, so collapsing these is a precondition for everything else.
    # Populated during adjudication; exact-name matching only in v0.
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )


class AuditLog(Base):
    """Append-only. Every ledger mutation writes here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int] = mapped_column(BigInteger)
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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


class Dependency(Base):
    __tablename__ = "dependencies"
    __table_args__ = (UniqueConstraint("project_id", "ref_code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # Minted by us, unique per project. Deliberately not the source's own
    # identifier: NHHIP's 2/13/2026 matrix carries two different conflicts
    # both labelled FOC14-69 — same owner, different locations — so source
    # IDs cannot identify a ledger record.
    ref_code: Mapped[str] = mapped_column(String(32))
    # The identifier the source used, kept for display and merge matching.
    # Not assumed unique.
    source_ref: Mapped[str | None] = mapped_column(String(64))
    dep_type: Mapped[str] = mapped_column(_enum(*DEP_TYPES, name="dep_type"))
    title: Mapped[str] = mapped_column(Text)
    location_desc: Mapped[str | None] = mapped_column(Text)
    # Stationing is the most discriminating signal in merge ranking because
    # it is numeric: "245+00" and "445+00" are near-identical as strings and
    # two thousand feet apart on the ground.
    station_from: Mapped[str | None] = mapped_column(String(32))
    station_to: Mapped[str | None] = mapped_column(String(32))
    # FKs arrive with external_orgs and milestones; those tables are not in
    # the skeleton.
    external_org_id: Mapped[int | None] = mapped_column(BigInteger)
    milestone_id: Mapped[int | None] = mapped_column(BigInteger)
    external_contact: Mapped[str | None] = mapped_column(Text)
    internal_owner: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        _enum(*DEP_STATUSES, name="dep_status"),
        default="identified",
        server_default="identified",
    )
    criticality: Mapped[str] = mapped_column(
        _enum(*CRITICALITIES, name="criticality"),
        default="normal",
        server_default="normal",
    )
    committed_date: Mapped[date | None] = mapped_column(Date)
    need_date: Mapped[date | None] = mapped_column(Date)
    # Free text in v0: what closes this. A reviewer judges whether a given
    # piece of evidence meets it. Promoting this to a typed taxonomy waits
    # until real adjudications show what closure documents look like
    # (ADR-0002).
    evidence_required: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DependencyEvent(Base):
    """Something that happened to a Dependency, in order.

    Events are appended, never edited. A `slip` does not update the earlier
    commitment — both stay, because the fact that a date moved is itself the
    thing worth recording.
    """

    __tablename__ = "dependency_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    event_type: Mapped[str] = mapped_column(_enum(*EVENT_TYPES, name="event_type"))
    # The date the event happened, which is not the date it was recorded.
    event_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceLink(Base):
    __tablename__ = "evidence_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Always set, even when the evidence is really about an event: an
    # event's evidence is also its dependency's evidence, and keeping this
    # required means `is_ready` and `last_evidenced_at` stay simple queries
    # over one column rather than a union.
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    # Set when this quote specifically supports an event rather than a field.
    event_id: Mapped[int | None] = mapped_column(ForeignKey("dependency_events.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    page_no: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)
    # The quote appears on the cited page. Nothing more.
    verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # A reviewer's judgment that this evidence meets the dependency's
    # evidence_required bar. Readiness is computed from verified AND this.
    # Defaults are server-side so that neither verification nor sufficiency
    # can be assumed by a writer that bypasses the ORM.
    satisfies_requirement: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Milestone(Base):
    """A dated event in the project schedule that Dependencies must be ready for.

    A Dependency's need date is derived from the Milestone it serves — a
    property of the *project*. That is a different thing from its committed
    date, which is what an external party said it would do, and keeping the
    two apart is most of the point of the ledger.
    """

    __tablename__ = "milestones"
    __table_args__ = (UniqueConstraint("project_id", "code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    need_date: Mapped[date | None] = mapped_column(Date)
    # Where this came from — a CSV filename in v0, a P6 XER export in M9.
    source: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Candidate(Base):
    """An extractor's proposal, not yet part of the Ledger.

    Extractors write only here. The single path into the ledger is a human
    keystroke, which is what makes an LLM pipeline auditable.
    """

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(_enum(*CANDIDATE_KINDS, name="candidate_kind"))
    payload_json: Mapped[dict] = mapped_column(JSONB)
    source_document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    source_pages: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    confidence: Mapped[float | None] = mapped_column(Float)
    # Recorded on every candidate. Without both, eval history across runs is
    # not comparable and you cannot tell which change moved the numbers.
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    # Mechanical: every citation's quote was found on its cited page. Kept
    # separate from `state`, which is the human adjudication lifecycle. A
    # candidate whose citations fail is sunk in the queue, never dropped.
    citations_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    state: Mapped[str] = mapped_column(
        _enum(*CANDIDATE_STATES, name="candidate_state"),
        default="pending",
        server_default="pending",
    )
    merged_into: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    adjudicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Assertion(Base):
    __tablename__ = "assertions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    field_name: Mapped[str] = mapped_column(String(64))
    asserted_value: Mapped[str | None] = mapped_column(Text)
    evidence_link_id: Mapped[int] = mapped_column(ForeignKey("evidence_links.id"))
    # The source document's own date, which is what orders competing claims
    # and drives last_evidenced_at.
    doc_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
