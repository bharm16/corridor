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
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
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
EXTRACTION_OUTCOMES = ("completed", "failed", "unreadable", "no_matrix")
SUPPORT_ROLES = ("publication",)
# Where a page's text came from, most reliable first. `cells` is a
# spreadsheet source read natively (ADR-0005): its text was generated from
# the cells rather than recovered from a layout, which is what lets a
# citation against it verify exactly instead of at the 0.9 threshold print
# damage requires.
TEXT_SOURCES = ("cells", "text_layer", "ocr")
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

# How a utility conflict is to be resolved, as the document says it
# (ADR-0009). SHRP2 R15B publishes four alternatives; the first is
# decomposed along the Red/Brown split FDOT prints on its plans, which is
# where the line between "the facility moves" and "the facility stays"
# actually falls.
RESOLUTION_STRATEGIES = (
    "relocate",
    "remove",
    "abandon_in_place",
    "adjust_vertical",
    "protect_in_place",
    "change_design",
    "policy_exception",
)

# Criticality is a reading of the strategy, never a stored scale. The three
# here are FDOT's Red: the facility is moved, taken out, or deactivated —
# all of them scheduled work the utility owner must perform. Brown (a
# vertical adjustment to grade, 0.5 days by FDOT's own duration table) and
# Green (it stays) are not, and neither is a resolution that asks nothing
# of the owner at all.
#
# This set and the gold set's `critical` labelling rule are one sentence on
# purpose (ADR-0009). If they diverge, the M7 gate scores one definition
# against another and the number means nothing.
CRITICAL_STRATEGIES = frozenset({"relocate", "remove", "abandon_in_place"})

# What separates two answers inside one asserted resolution value.
#
# A layout that records its strategy as marked columns can mark more than
# one: 12 of WSDOT 9424's rows do, and one of them marks answers from
# opposite sides of the line above (#105). The extractor stores both
# headings and the vocabulary decides what they mean together, so the two
# sides need one agreed separator.
#
# `;` rather than `/`, because `/` is inside a heading this corpus prints —
# `Abandon / Deactivate`. It is also already one of `verify._FIELD_SEPARATORS`,
# so a joined value tokenises into the words the page really carries and
# never reads as invented text.
#
# The trailing space joins and does not split: a reader takes the value
# apart on `;` alone and normalises whitespace per answer anyway, so it
# tolerates a value written without it. Only the writer needs the space,
# and it is here rather than at the join so that one constant governs both.
ANSWER_SEPARATOR = "; "

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


def is_claim(value: str | None) -> bool:
    """Does this asserted value say anything a source could disagree with?

    A blank cell is an absent value, not a competing one — the same
    reading the CONTRADICTION query already applied to nulls, because the
    matrix revisions add and drop columns between editions. Empty strings
    are the printed form of the same absence.

    Lives here because both readers of contradiction need it and neither
    may import the other: the exception engine computes CONTRADICTION and
    the ledger renders the "sources disagree" pill, and the ledger is the
    one that depends on the engine.
    """
    return bool(value and value.strip())


def is_critical(strategy: str | None) -> bool:
    """Does this resolution commit the External Party to substantial work?

    Takes the value rather than a Dependency: `changes.py` reads it off a
    stored report snapshot, which is a dict and not an ORM row, and a
    Dependency-shaped signature would force that caller to fake an object.

    `None` is not critical, and that is a reading of silence rather than a
    claim about the record. An inventory records conflicts without ever
    saying how they resolve — Project A's 3,235 rows assert no strategy at
    all — and treating that as critical would mark most of the corpus,
    which is the weakness ADR-0007 diagnosed in itself.
    """
    return strategy in CRITICAL_STRATEGIES


# Values a document prints where an External Party should be, meaning it
# declined to name one. `NA` is not an organization — CONTEXT.md defines an
# External Party as "the organization outside the project that owns a
# Dependency" — and 86 of Project A's rows carry it, 44 of them the whole
# last page of its oldest revision.
#
# The extractor still stores what the document printed. Dropping it would
# lose evidence; the fix is that nothing downstream treats it as a party.
PLACEHOLDER_PARTIES = frozenset(
    {"", "na", "n/a", "tbd", "none", "unknown", "no id", "-", "--", "?", "n.a."}
)


def is_placeholder_party(name: str | None) -> bool:
    """Is this the document declining to name an owner?

    Matched on the whole value, never as a substring: a real party can
    contain a placeholder's letters — `Nakina Telephone` starts with `na`
    — and blocking that would merge a named utility into the nameless
    cohort, which is worse than the defect being fixed.
    """
    return " ".join((name or "").split()).casefold() in PLACEHOLDER_PARTIES


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
    __table_args__ = (
        UniqueConstraint("project_id", "sha256"),
        UniqueConstraint(
            "project_id", "id", name="uq_documents_project_id_id"
        ),
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


class ExtractionRun(Base):
    """One immutable extraction attempt for one document and prompt version.

    A receipt exists for every terminal outcome, including failures and valid
    zero-row reads.  A redo appends another receipt; it never rewrites the
    earlier attempt or the Candidates that attempt produced.
    """

    __tablename__ = "extraction_runs"
    __table_args__ = (UniqueConstraint("document_id", "id"),)

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


class ReportRun(Base):
    """A published report, kept so the next one can say what changed.

    `ruleset_version` is stored per run for a specific reason: a figure that
    moved between two weekly reports must be attributable to a *rule* change
    or a *data* change, and those call for opposite responses. Tightening
    STALE from 14 days to 10 looks identical to a project falling behind
    unless the report remembers which ruleset produced each number.
    """

    __tablename__ = "report_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ruleset_version: Mapped[str] = mapped_column(String(32))
    # One entry per dependency: the state the report was published against.
    snapshot_json: Mapped[dict] = mapped_column(JSONB)
    output_path: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Append-only. Every ledger mutation writes here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(Text)
    # Null on legacy and non-Admission entries.  New Admissions store the
    # exact namespaced subject here; ``actor`` remains for honest display of
    # historical labels such as ``agent`` and ``demo`` rather than relabelling
    # them as people (ADR-0020).
    human_principal: Mapped[str | None] = mapped_column(Text)
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
    external_org_id: Mapped[int | None] = mapped_column(ForeignKey("external_orgs.id"))
    milestone_id: Mapped[int | None] = mapped_column(ForeignKey("milestones.id"))
    external_contact: Mapped[str | None] = mapped_column(Text)
    internal_owner: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        _enum(*DEP_STATUSES, name="dep_status"),
        default="identified",
        server_default="identified",
    )
    # What the document says is to be done about the conflict, as an
    # adjudicated conclusion drawn from its Assertions. Criticality is read
    # off this rather than stored beside it (ADR-0009).
    #
    # Null means no document asserted a strategy, and most of the corpus is
    # null on purpose: an inventory records that conflicts exist without
    # ever saying how they resolve, so Project A's 3,235 rows and SH 99's
    # 1,401 assert nothing here. Only SR 789 prints a
    # `Recommended Conflict Resolution` column. Nothing defaults it,
    # because a default would be this field claiming something no document
    # said — the failure ADR-0007 was written about and then committed.
    resolution_strategy: Mapped[str | None] = mapped_column(
        _enum(*RESOLUTION_STRATEGIES, name="resolution_strategy")
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
    __table_args__ = (UniqueConstraint("dependency_id", "id"),)

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


class OperativeSupport(Base):
    """A human designation of which Evidence supports a publication scope.

    Readiness deliberately does not live here: it remains the independent
    ``EvidenceLink.satisfies_requirement`` judgment. The resolver combines
    the two roles without collapsing their meanings (ADR-0017).
    """

    __tablename__ = "operative_support"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dependency_id", "evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_operative_support_dependency_evidence",
        ),
        Index(
            "uq_operative_support_record_role",
            "dependency_id",
            "role",
            unique=True,
            postgresql_where=text("field_name is null"),
        ),
        Index(
            "uq_operative_support_field_role",
            "dependency_id",
            "role",
            "field_name",
            unique=True,
            postgresql_where=text("field_name is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    evidence_link_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(
        _enum(*SUPPORT_ROLES, name="operative_support_role")
    )
    field_name: Mapped[str | None] = mapped_column(String(64))
    designated_by: Mapped[str] = mapped_column(Text)
    designated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
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
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(_enum(*CANDIDATE_KINDS, name="candidate_kind"))
    payload_json: Mapped[dict] = mapped_column(JSONB)
    source_document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    # Null only for history that cannot be tied to exactly one attempt
    # without guessing. Every new production extraction sets this.
    extraction_run_id: Mapped[int | None] = mapped_column(BigInteger)
    source_pages: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    confidence: Mapped[float | None] = mapped_column(Float)
    # Recorded on every candidate. Without both, eval history across runs is
    # not comparable and you cannot tell which change moved the numbers.
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    # Mechanical: every citation's quote was found on its cited page, and —
    # where the extractor transcribes rather than parses — every field value
    # is text on that page too. Kept separate from `state`, which is the
    # human adjudication lifecycle. A candidate that fails either check is
    # sunk in the queue, never dropped.
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
