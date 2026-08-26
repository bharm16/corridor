"""Schema for the walking skeleton.

Three points differ from a naive reading of the spec and are easy to get
wrong, so they are called out here as well as in the ADRs:

- A Dependency has no mutable lifecycle status. Ready, Dismissal, Commitment
  Closure, Exceptions, and Coordination Plans supply the supported states
  (ADR-0002, ADR-0044).
- `Assertion` is a table, not a column. A ledger field value is an
  adjudicated conclusion; the assertions beneath it preserve what each
  source actually claimed (ADR-0001).
- `EvidenceLink.verified` means exactly one thing: the quote appears on
  the cited page. It says nothing about whether the claim is true.
"""

from datetime import date, datetime
from hashlib import sha256

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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

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
# How a matrix names its rows — declared at registration, like a
# document's date, and never inferred from the data (ADR-0030).
# `project-unique`: one number names one conflict across the project
# (the TxDOT UCM form, whose retired rows exist to keep numbers stable).
# `per-party`: each External Party's list counts from 1, so a row's name
# is the party and the number together (the FDOT roundabout form).
NUMBERING_SCHEMES = ("project-unique", "per-party")
EXTRACTION_OUTCOMES = (
    "completed",
    "failed",
    "unreadable",
    "no_matrix",
    "quarantined",
)
REVISION_COMPARISON_STATES = (
    "added",
    "dropped",
    "unchanged",
    "changed",
    "ambiguous",
    "unmatched",
)
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
    "committed_date_change",
    "response",
    "escalation",
    "status_change",
    "closure",
)
EVENT_SOURCE_KINDS = ("cited", "verbal")
TIMING_PRECISIONS = ("day", "month", "approximate", "legacy_unknown")
STATEMENT_ATTRIBUTION_STATES = ("resolved", "unresolved")
STATEMENT_SCOPE_MODES = ("unknown", "selected", "all_active", "carried_forward")
TIMING_CHANGE_DIRECTIONS = ("earlier", "later", "unknown")


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

    These value sets are still moving — `status_report` was added to DOC_TYPES
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


def _statement_attribution_state(context) -> str:
    """Default direct ORM rows honestly from their resolved-party field."""
    return (
        "resolved"
        if context.get_current_parameters().get("stated_external_org_id") is not None
        else "unresolved"
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
    # The parties that are the project's own side — its engineer, its
    # consultants. Stated configuration, never inferred: an event whose
    # actor is one of these is the project taking an action item, and it
    # can never carry an External Party's commitment (ADR-0026). SH 99's
    # minutes are mostly LJA's own commitments, which is why this exists.
    project_side_parties: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# The three families that write records or move support under an
# authorized policy (ADRs 0022, 0026, 0027). ADR-0028 joined their
# approval and run tables — the shapes were identical, and copies drift —
# while each family keeps its own outcome table, whose shape is the
# receipt.
POLICY_FAMILIES = (
    "automatic-carry-forward",
    "event-admission",
    "dependency-admission",
)

_POLICY_FAMILY_CHECK = (
    "family in ('automatic-carry-forward', 'event-admission', 'dependency-admission')"
)


class PolicyApproval(Base):
    """One immutable human authorization of a policy family's rules.

    ADR-0028: one table for every family's signatures, each row naming
    its family. The family-carrying unique keys are what let each outcome
    table keep its "my outcomes point only at my runs" rule in the
    schema rather than in code review.
    """

    __tablename__ = "policy_approvals"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_policy_approvals_project_id"),
        UniqueConstraint("family", "id", name="uq_policy_approvals_family_id"),
        UniqueConstraint(
            "project_id",
            "family",
            "id",
            name="uq_policy_approvals_project_family_id",
        ),
        CheckConstraint(_POLICY_FAMILY_CHECK, name="ck_policy_approvals_family"),
        CheckConstraint(
            "jsonb_typeof(policy_json) = 'object'",
            name="ck_policy_approvals_object",
        ),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_policy_approvals_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    family: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(Text)
    policy_json: Mapped[dict] = mapped_column(JSONB)
    policy_sha256: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PolicyRun(Base):
    """One immutable batch receipt for a policy pass.

    `applied_count` is the neutral name for what a family applied —
    carried support, admitted events, admitted dependencies. A deferred
    database trigger reconciles both counts against the family's own
    outcome table at commit, for every family: the check Carry-Forward
    alone used to carry (ADR-0028).
    """

    __tablename__ = "policy_runs"
    __table_args__ = (
        UniqueConstraint("family", "id", name="uq_policy_runs_family_id"),
        UniqueConstraint(
            "project_id",
            "family",
            "id",
            name="uq_policy_runs_project_family_id",
        ),
        CheckConstraint(_POLICY_FAMILY_CHECK, name="ck_policy_runs_family"),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_policy_runs_sha256",
        ),
        CheckConstraint(
            "applied_count >= 0 and abstained_count >= 0",
            name="ck_policy_runs_counts",
        ),
        ForeignKeyConstraint(
            ["project_id", "family", "policy_approval_id"],
            [
                "policy_approvals.project_id",
                "policy_approvals.family",
                "policy_approvals.id",
            ],
            name="fk_policy_runs_approval_project_family",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(String(32))
    # Null for normal Corridor-managed policies. Historical project-approved
    # Carry-Forward runs retain the approval they originally named.
    policy_approval_id: Mapped[int | None] = mapped_column(BigInteger)
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    abstention_reason_version: Mapped[str] = mapped_column(String(64))
    applied_count: Mapped[int] = mapped_column(Integer)
    abstained_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
    __table_args__ = (
        UniqueConstraint("document_id", "id"),
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
                and extractor_config_json is not null
                and extractor_config_sha256 is not null
                and token_usage_json is not null
                and prompt_sha256 ~ '^[0-9a-f]{64}$'
                and schema_sha256 ~ '^[0-9a-f]{64}$'
                and postprocessor_sha256 ~ '^[0-9a-f]{64}$'
                and extractor_config_sha256 ~ '^[0-9a-f]{64}$'
                and jsonb_typeof(extractor_config_json) = 'object'
                and extractor_config_json ?& array[
                    'receipt_version', 'extractor', 'prompt_version', 'model',
                    'schema_version', 'prompt_sha256', 'schema_sha256',
                    'postprocessor_sha256', 'request_controls', 'runtime'
                ]
                and jsonb_typeof(
                    extractor_config_json -> 'receipt_version'
                ) = 'number'
                and extractor_config_json ->> 'receipt_version' = '1'
                and jsonb_typeof(
                    extractor_config_json -> 'extractor'
                ) = 'string'
                and length(trim(extractor_config_json ->> 'extractor')) > 0
                and jsonb_typeof(
                    extractor_config_json -> 'prompt_version'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'schema_version'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'prompt_sha256'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'schema_sha256'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'postprocessor_sha256'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'request_controls'
                ) = 'object'
                and jsonb_typeof(
                    extractor_config_json -> 'runtime'
                ) = 'object'
                and extractor_config_json -> 'runtime' ?& array[
                    'python_implementation', 'python_version',
                    'dependency_lock_sha256', 'packages'
                ]
                and jsonb_typeof(
                    extractor_config_json -> 'runtime' ->
                        'python_implementation'
                ) = 'string'
                and length(trim(
                    extractor_config_json -> 'runtime' ->>
                        'python_implementation'
                )) > 0
                and jsonb_typeof(
                    extractor_config_json -> 'runtime' -> 'python_version'
                ) = 'string'
                and length(trim(
                    extractor_config_json -> 'runtime' ->> 'python_version'
                )) > 0
                and jsonb_typeof(
                    extractor_config_json -> 'runtime' ->
                        'dependency_lock_sha256'
                ) = 'string'
                and extractor_config_json -> 'runtime' ->>
                    'dependency_lock_sha256' ~ '^[0-9a-f]{64}$'
                and jsonb_typeof(
                    extractor_config_json -> 'runtime' -> 'packages'
                ) = 'object'
                and extractor_config_json ->> 'prompt_version' = prompt_version
                and extractor_config_json ->> 'schema_version' = schema_version
                and extractor_config_json ->> 'prompt_sha256' = prompt_sha256
                and extractor_config_json ->> 'schema_sha256' = schema_sha256
                and extractor_config_json ->> 'postprocessor_sha256' =
                    postprocessor_sha256
                and (
                    (
                        model is null
                        and jsonb_typeof(
                            extractor_config_json -> 'model'
                        ) = 'null'
                    )
                    or (
                        model is not null
                        and jsonb_typeof(
                            extractor_config_json -> 'model'
                        ) = 'string'
                        and extractor_config_json ->> 'model' = model
                    )
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
    # starts, then copied here. Historical rows remain null rather than being
    # reconstructed from whatever source happens to be deployed today.
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    schema_sha256: Mapped[str | None] = mapped_column(String(64))
    postprocessor_sha256: Mapped[str | None] = mapped_column(String(64))
    extractor_config_json: Mapped[dict | None] = mapped_column(JSONB)
    extractor_config_sha256: Mapped[str | None] = mapped_column(String(64))
    token_usage_json: Mapped[dict | None] = mapped_column(JSONB)
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


class DocumentQuarantine(Base):
    """The durable project-level fact that a registered document is held out.

    A document whose relationship semantics Corridor does not model — a
    Utility Work Schedule's Dependent Activity chain (#149) — is registered,
    visible, and deliberately unread. This row is why: durable, queryable,
    and never only in an operator's memory or a process's logs.
    """

    __tablename__ = "document_quarantines"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id"), primary_key=True
    )
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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


class CohortReceipt(Base):
    """The immutable membership of one derived rehearsal cohort.

    Membership is a pure function of a sealed Revision Comparison, the
    verification state in its successor inputs snapshot, one External Party
    name, and one rule version — so re-deriving yields identical members
    and an identical digest, and the receipt can be checked rather than
    trusted. Members are registry identities, never database ids. The
    queue's rehearsal lane reads exactly this set, and mutations outside
    it refuse (#173, #175).
    """

    __tablename__ = "cohort_receipts"
    __table_args__ = (
        UniqueConstraint(
            "revision_comparison_run_id",
            "rule_version",
            "external_org",
            name="uq_cohort_receipts_one_per_rule",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    revision_comparison_run_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    predecessor_extraction_run_id: Mapped[int] = mapped_column(BigInteger)
    successor_extraction_run_id: Mapped[int] = mapped_column(BigInteger)
    external_org: Mapped[str] = mapped_column(Text)
    rule_version: Mapped[str] = mapped_column(String(64))
    matcher_version: Mapped[str] = mapped_column(String(64))
    members: Mapped[list] = mapped_column(JSONB)
    member_count: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventCohortReceipt(Base):
    """The immutable membership of one derived event cohort.

    A sibling of CohortReceipt for cohorts no Revision Comparison selects
    (docs/sh99-date-rehearsal.md): membership is a pure function of the
    declared Active Runs the rule reads and one rule version, derived from
    the event Candidate stream. Members are document identities — conflict
    refs — never database ids; the lane that reads the set resolves them
    at read time against the pinned input runs, and mutations outside the
    set refuse, exactly as the rehearsal receipt works (#173, #175).
    """

    __tablename__ = "event_cohort_receipts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "rule_version",
            name="uq_event_cohort_receipts_one_per_rule",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    rule_version: Mapped[str] = mapped_column(String(64))
    input_run_ids: Mapped[list] = mapped_column(JSONB)
    members: Mapped[list] = mapped_column(JSONB)
    member_count: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventAdmissionOutcome(Base):
    """One immutable event outcome within an event-admission receipt.

    An admitted outcome names the DependencyEvent it created; an
    abstention names the check that failed, under a stated reason
    vocabulary. Neither carries a model's opinion — no model verdict can
    appear in an admission path (ADR-0026).
    """

    __tablename__ = "event_admission_outcomes"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('admitted', 'abstained')",
            name="ck_event_admission_outcome_value",
        ),
        CheckConstraint(
            "("
            "outcome = 'admitted' and reason is null "
            "and dependency_event_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and dependency_event_id is null"
            ")",
            name="ck_event_admission_outcome_kind",
        ),
        # The family rule stays in the schema (ADR-0028): this outcome
        # can only ever point at an event-admission run.
        CheckConstraint(
            "family = 'event-admission'",
            name="ck_event_admission_outcome_family",
        ),
        ForeignKeyConstraint(
            ["family", "policy_run_id"],
            ["policy_runs.family", "policy_runs.id"],
            name="fk_event_admission_outcome_run_family",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    policy_run_id: Mapped[int] = mapped_column(BigInteger, index=True)
    family: Mapped[str] = mapped_column(String(32), server_default="event-admission")
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    outcome: Mapped[str] = mapped_column(String(9))
    reason: Mapped[str | None] = mapped_column(String(64))
    dependency_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_events.id")
    )
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    scope_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_event_scope_decisions.id")
    )
    candidate_disposition_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_dispositions.id")
    )
    audit_log_id: Mapped[int | None] = mapped_column(ForeignKey("audit_log.id"))
    eligibility_json: Mapped[dict | None] = mapped_column(JSONB)
    eligibility_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventAdmissionAcceptanceReceipt(Base):
    """Immutable real-state proof for one Event Admission activation attempt."""

    __tablename__ = "event_admission_acceptance_receipts"
    __table_args__ = (
        CheckConstraint(
            "status in ('passed', 'failed')",
            name="ck_event_admission_acceptance_status",
        ),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_event_admission_acceptance_policy_sha256",
        ),
        CheckConstraint(
            "receipt_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_event_admission_acceptance_receipt_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    source_revision: Mapped[str] = mapped_column(String(64))
    migration_head: Mapped[str] = mapped_column(String(64))
    predecessor_policy_version: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    reason_version: Mapped[str] = mapped_column(String(64))
    selection_rule: Mapped[str] = mapped_column(String(128))
    receipt_json: Mapped[dict] = mapped_column(JSONB)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventAdmissionActivation(Base):
    """Append-only activation or suspension of one proved policy version."""

    __tablename__ = "event_admission_activations"
    __table_args__ = (
        CheckConstraint(
            "action in ('activate', 'suspend')",
            name="ck_event_admission_activation_action",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_event_admission_activation_reason",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_event_admission_activation_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    acceptance_receipt_id: Mapped[int] = mapped_column(
        ForeignKey("event_admission_acceptance_receipts.id")
    )
    action: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(128))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DependencyAdmissionOutcome(Base):
    """One immutable candidate outcome within an admission receipt.

    `admitted` names the Dependency the primary candidate became;
    `merged` names the Dependency an identical sibling corroborates;
    `abstained` names the check that failed.
    """

    __tablename__ = "dependency_admission_outcomes"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('admitted', 'merged', 'abstained')",
            name="ck_dependency_admission_outcome_value",
        ),
        CheckConstraint(
            "("
            "outcome in ('admitted', 'merged') and reason is null "
            "and dependency_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and dependency_id is null"
            ")",
            name="ck_dependency_admission_outcome_kind",
        ),
        # The family rule stays in the schema (ADR-0028): this outcome
        # can only ever point at a dependency-admission run.
        CheckConstraint(
            "family = 'dependency-admission'",
            name="ck_dependency_admission_outcome_family",
        ),
        CheckConstraint(
            "eligibility_sha256 is null or eligibility_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_dependency_admission_outcome_eligibility_sha256",
        ),
        CheckConstraint(
            "(outcome = 'abstained' and "
            "((eligibility_json is null and eligibility_sha256 is null) or "
            "(eligibility_json is not null and eligibility_sha256 is not null))) "
            "or (outcome in ('admitted', 'merged') and "
            "eligibility_json is null and eligibility_sha256 is null)",
            name="ck_dependency_admission_outcome_eligibility_shape",
        ),
        ForeignKeyConstraint(
            ["family", "policy_run_id"],
            ["policy_runs.family", "policy_runs.id"],
            name="fk_dependency_admission_outcome_run_family",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    policy_run_id: Mapped[int] = mapped_column(BigInteger, index=True)
    family: Mapped[str] = mapped_column(
        String(32), server_default="dependency-admission"
    )
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    outcome: Mapped[str] = mapped_column(String(9))
    reason: Mapped[str | None] = mapped_column(String(64))
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    eligibility_json: Mapped[dict | None] = mapped_column(JSONB)
    eligibility_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkDecision(Base):
    """One appended project decision about exactly one Coordination Subject.

    A Work Decision proves only what the project decided and when
    (ADR-0025): it is not Evidence, states nothing about what a document or
    External Party said, and can never set Criticality, a Resolution
    Strategy, Ready, or an External Party's status or commitment. The typed
    receipt is the record — the audit log carries only a pointer event.
    One linear chain per Dependency or Commitment Lineage and field; their
    current values are projections of the chain tails.  A statement-level
    plan belongs to durable factual lineage, never to a Dependency selected
    later by a scope decision (ADR-0038).
    """

    __tablename__ = "work_decisions"
    __table_args__ = (
        UniqueConstraint(
            "dependency_id", "id", name="uq_work_decisions_dependency_id_id"
        ),
        UniqueConstraint(
            "commitment_lineage_id",
            "id",
            name="uq_work_decisions_commitment_lineage_id_id",
        ),
        ForeignKeyConstraint(
            ["dependency_id", "predecessor_decision_id"],
            ["work_decisions.dependency_id", "work_decisions.id"],
            name="fk_work_decisions_predecessor",
        ),
        ForeignKeyConstraint(
            ["commitment_lineage_id", "predecessor_decision_id"],
            ["work_decisions.commitment_lineage_id", "work_decisions.id"],
            name="fk_work_decisions_commitment_lineage_predecessor",
        ),
        UniqueConstraint(
            "predecessor_decision_id", name="uq_work_decisions_predecessor"
        ),
        CheckConstraint(
            "(dependency_id is not null and commitment_lineage_id is null) "
            "or (dependency_id is null and commitment_lineage_id is not null)",
            name="ck_work_decisions_exactly_one_subject",
        ),
        CheckConstraint(
            "field in ('internal_owner', 'next_action', 'milestone_impact', 'deferral')",
            name="ck_work_decisions_field",
        ),
        CheckConstraint(
            "(field <> 'deferral' and deferral_reason is null and deferral_return_date is null) "
            "or (field = 'deferral' and ((after_value is null and deferral_reason is null "
            "and deferral_return_date is null) or (after_value is not null "
            "and deferral_reason in ('waiting_for_information', 'waiting_for_external_party', "
            "'assigned_to_someone_else') and deferral_return_date is not null)))",
            name="ck_work_decisions_deferral_shape",
        ),
        Index(
            "uq_work_decisions_one_root",
            "dependency_id",
            "field",
            unique=True,
            postgresql_where=text("predecessor_decision_id is null"),
        ),
        Index(
            "uq_work_decisions_commitment_lineage_one_root",
            "commitment_lineage_id",
            "field",
            unique=True,
            postgresql_where=text("predecessor_decision_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id"), server_default=text("null")
    )
    decision_type: Mapped[str] = mapped_column(String(32))
    field: Mapped[str] = mapped_column(String(32))
    before_value: Mapped[str | None] = mapped_column(Text)
    after_value: Mapped[str | None] = mapped_column(Text)
    recorded_by: Mapped[str] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    predecessor_decision_id: Mapped[int | None] = mapped_column(BigInteger)
    action_due_date_reason: Mapped[str | None] = mapped_column(
        String(64), server_default=text("null")
    )
    no_follow_up_reason: Mapped[str | None] = mapped_column(
        String(64), server_default=text("null")
    )
    cancellation_reason: Mapped[str | None] = mapped_column(
        String(64), server_default=text("null")
    )
    note: Mapped[str | None] = mapped_column(Text, server_default=text("null"))
    # A deferral is a distinct Work Decision: unlike an unknown Action Due
    # Date, it names both why immediate attention can wait and exactly when
    # Corridor must put the item back in front of the coordinator.
    deferral_reason: Mapped[str | None] = mapped_column(
        String(64), server_default=text("null")
    )
    deferral_return_date: Mapped[date | None] = mapped_column(Date)
    # The factual state a future return condition was set against.  These
    # immutable observations let the work list reopen when the party's
    # statement, its scope, or its Milestone Impact changes.
    observed_statement_event_id: Mapped[int | None] = mapped_column(BigInteger)
    observed_scope_decision_id: Mapped[int | None] = mapped_column(BigInteger)
    observed_milestone_impact_decision_id: Mapped[int | None] = mapped_column(
        BigInteger
    )


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


class ExternalParty(Base):
    """One registered External Party with every confirmed source alias."""

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


# Physical schema and older integrations used this implementation name.
ExternalOrg = ExternalParty


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
    # Document-only reports are a separate comparison lineage: comparing one
    # against the ordinary report would leak a verbal date through its old
    # snapshot into an otherwise citation-only surface.
    document_only: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )


class ExternalReportArtifact(Base):
    """One immutable, already-rendered External Report PDF.

    Rendering is deliberately separate from human release.  This table owns
    the exact PDF and frozen Report context a project person can later choose;
    a release receipt copies those bytes so its retention never depends on an
    artifact URL, path, or regenerating ReportRun.
    """

    __tablename__ = "external_report_artifacts"
    __table_args__ = (
        CheckConstraint("format = 'pdf'", name="ck_external_report_artifacts_pdf_only"),
        CheckConstraint(
            "pdf_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_external_report_artifacts_pdf_sha256",
        ),
        CheckConstraint(
            "octet_length(pdf_bytes) > 5",
            name="ck_external_report_artifacts_nonempty_pdf",
        ),
        CheckConstraint(
            "provenance_mode in ('all-supported-sources', 'document-only')",
            name="ck_external_report_artifacts_provenance_mode",
        ),
        CheckConstraint(
            "jsonb_typeof(evaluation_context_json) = 'object'",
            name="ck_external_report_artifacts_evaluation_object",
        ),
        CheckConstraint(
            "jsonb_typeof(record_context_json) = 'object'",
            name="ck_external_report_artifacts_context_object",
        ),
        CheckConstraint(
            "length(trim(artifact_name)) > 0",
            name="ck_external_report_artifacts_artifact_name",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    artifact_name: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16), server_default="pdf")
    pdf_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_on: Mapped[date] = mapped_column(Date)
    ruleset_version: Mapped[str] = mapped_column(String(64))
    evaluation_context_json: Mapped[dict] = mapped_column(JSONB)
    provenance_mode: Mapped[str] = mapped_column(String(32))
    record_context_json: Mapped[dict] = mapped_column(JSONB)
    rendered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def digest_is_valid(self) -> bool:
        """Whether the retained bytes still match the rendered artifact digest."""
        return sha256(self.pdf_bytes).hexdigest() == self.pdf_sha256


class ExternalReportRelease(Base):
    """One immutable authorization of one fixed External Report PDF.

    A working ``ReportRun`` lets the next internal report describe change;
    it is deliberately not an artifact authority.  This receipt instead
    owns the exact PDF bytes and every context identity that was released,
    so later Ledger, Evaluation, or rendering changes cannot alter the
    recipient's record (ADR-0040).
    """

    __tablename__ = "external_report_releases"
    __table_args__ = (
        CheckConstraint("format = 'pdf'", name="ck_external_report_releases_pdf_only"),
        CheckConstraint(
            "pdf_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_external_report_releases_pdf_sha256",
        ),
        CheckConstraint(
            "octet_length(pdf_bytes) > 5",
            name="ck_external_report_releases_nonempty_pdf",
        ),
        CheckConstraint(
            "provenance_mode in ('all-supported-sources', 'document-only')",
            name="ck_external_report_releases_provenance_mode",
        ),
        CheckConstraint(
            "jsonb_typeof(record_context_json) = 'object'",
            name="ck_external_report_releases_context_object",
        ),
        CheckConstraint(
            "evaluation_context_json is null or "
            "jsonb_typeof(evaluation_context_json) = 'object'",
            name="ck_external_report_releases_evaluation_object",
        ),
        CheckConstraint(
            "length(trim(artifact_name)) > 0",
            name="ck_external_report_releases_artifact_name",
        ),
        CheckConstraint(
            "length(trim(released_by)) > 0",
            name="ck_external_report_releases_released_by",
        ),
        CheckConstraint(
            "released_by_display is not null and length(trim(released_by_display)) > 0",
            name="ck_external_report_releases_released_by_display",
        ),
        UniqueConstraint("artifact_id", name="uq_external_report_releases_artifact_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_report_artifacts.id")
    )
    artifact_name: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16), server_default="pdf")
    pdf_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_on: Mapped[date] = mapped_column(Date)
    ruleset_version: Mapped[str] = mapped_column(String(64))
    evaluation_context_json: Mapped[dict | None] = mapped_column(JSONB)
    provenance_mode: Mapped[str] = mapped_column(String(32))
    record_context_json: Mapped[dict] = mapped_column(JSONB)
    released_by: Mapped[str] = mapped_column(String(128))
    released_by_display: Mapped[str | None] = mapped_column(Text)
    released_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def digest_is_valid(self) -> bool:
        """Whether the bytes retrieved from the sealed store match the receipt."""
        return sha256(self.pdf_bytes).hexdigest() == self.pdf_sha256


class LegacyLedgerArchive(Base):
    """One immutable receipt for retiring a development-era Ledger graph.

    The JSON owns every row needed for standalone historical readback.  The
    scalar counts and ref-code high-water mark make the destructive operation
    auditable without asking active Ledger tables that are empty afterwards.
    """

    __tablename__ = "legacy_ledger_archives"
    __table_args__ = (
        UniqueConstraint("project_id"),
        CheckConstraint(
            "jsonb_typeof(content_json) = 'object'",
            name="ck_legacy_ledger_archive_content_object",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_legacy_ledger_archive_sha256",
        ),
        CheckConstraint(
            "dependency_count >= 0 and assertion_count >= 0 "
            "and evidence_link_count >= 0 and audit_log_count >= 0",
            name="ck_legacy_ledger_archive_counts",
        ),
        CheckConstraint(
            "ref_code_high_watermark >= 0",
            name="ck_legacy_ledger_archive_ref_high_watermark",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    format_version: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[dict] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    dependency_count: Mapped[int] = mapped_column(Integer)
    assertion_count: Mapped[int] = mapped_column(Integer)
    evidence_link_count: Mapped[int] = mapped_column(Integer)
    audit_log_count: Mapped[int] = mapped_column(Integer)
    ref_code_high_watermark: Mapped[int] = mapped_column(Integer)
    retired_by: Mapped[str] = mapped_column(Text)
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
    milestone_registration_id: Mapped[int | None] = mapped_column(
        ForeignKey("milestone_registrations.id"),
        deferred=True,
        server_default=text("null"),
    )
    external_contact: Mapped[str | None] = mapped_column(Text)
    internal_owner: Mapped[str | None] = mapped_column(Text)
    # The step the project decided must happen next, and either the date the
    # project set for it or the structured reason the date remains unknown —
    # all projections of Work Decision receipts (ADR-0025/0038). Neither is a
    # document claim.
    next_action: Mapped[str | None] = mapped_column(Text)
    action_due_date: Mapped[date | None] = mapped_column(Date)
    action_due_date_reason: Mapped[str | None] = mapped_column(String(64))
    deferral_reason: Mapped[str | None] = mapped_column(String(64))
    deferral_return_date: Mapped[date | None] = mapped_column(Date)
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
    # Set when a reviewer dismisses this record as junk. A projection of
    # the newest DependencyDismissal, which is the authority — readers
    # filter on this rather than joining, the same way they read the
    # projected Committed Date (ADR-0032). Never a delete.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CommitmentLineage(Base):
    """The durable identity of one accepted External Party commitment.

    A correction to attribution or timing appends a successor statement, but
    it does not create another Coordination Plan.  The projections here are
    therefore deliberately internal project decisions, never External Party
    facts or substitutions for the statement receipts (ADR-0038).
    """

    __tablename__ = "commitment_lineages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    internal_owner: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    action_due_date: Mapped[date | None] = mapped_column(Date)
    action_due_date_reason: Mapped[str | None] = mapped_column(String(64))
    deferral_reason: Mapped[str | None] = mapped_column(String(64))
    deferral_return_date: Mapped[date | None] = mapped_column(Date)
    milestone_impact: Mapped[str | None] = mapped_column(String(32))
    milestone_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default="{}"
    )
    # A factual successor changes the fact to which a plan responds.  It
    # preserves the plan history while refusing to silently call it current.
    plan_needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectRosterEntry(Base):
    """One selectable project-team member for a Coordination Plan.

    A Work Decision keeps its human-readable owner projection for existing
    readers, but a guided save must not turn a typed project roster into a
    caller-supplied string.  The grouping receipt binds the exact roster row
    that supplied the rendered name.
    """

    __tablename__ = "project_roster_entries"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "principal_subject", name="uq_project_roster_principal"
        ),
        CheckConstraint(
            "length(trim(principal_subject)) > 0", name="ck_project_roster_principal"
        ),
        CheckConstraint(
            "length(trim(display_name)) > 0", name="ck_project_roster_display_name"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    principal_subject: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExternalPartyStatement(Base):
    """One attributable External Party Statement, with scope kept separately.

    A statement may concern no known Dependency, one Dependency, or several.
    Commitment Scope membership is the only authority for that relationship.
    Timings live in their own rows so a month or approximate phrase never has
    to pretend to be one day.
    """

    __tablename__ = "dependency_events"
    __table_args__ = (
        UniqueConstraint(
            "supersedes_event_id",
            name="uq_dependency_events_supersedes_event",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(_enum(*EVENT_TYPES, name="event_type"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # Only accepted Commitments and Committed Date Changes carry this
    # durable subject identity.  Closure is a distinct External Party fact;
    # it cannot become a Coordination Subject by borrowing this key.
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id"), server_default=text("null")
    )
    # A closure is never a Coordination Subject, but it must name the one
    # Commitment Lineage whose External Party fact it establishes as closed.
    # Matching only on affected party would wrongly close every unresolved
    # party-level Commitment.
    closes_commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id"), server_default=text("null")
    )
    supersedes_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_events.id"), server_default=text("null")
    )
    affected_external_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_orgs.id")
    )
    # The resolved organization who made the statement.  The raw wording is
    # retained separately because source spelling is provenance too.
    stated_external_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_orgs.id")
    )
    attribution_state: Mapped[str] = mapped_column(
        _enum(
            *STATEMENT_ATTRIBUTION_STATES,
            name="statement_attribution_state",
        ),
        default=_statement_attribution_state,
        server_default="unresolved",
    )
    scope_mode: Mapped[str] = mapped_column(
        _enum(*STATEMENT_SCOPE_MODES, name="statement_scope_mode"),
        default="unknown",
        server_default="unknown",
    )
    timing_direction: Mapped[str | None] = mapped_column(
        _enum(*TIMING_CHANGE_DIRECTIONS, name="timing_change_direction")
    )
    # A source is declared rather than inferred from an absent EvidenceLink:
    # missing Evidence is how a defect looks, not how a verbal looks.
    source_kind: Mapped[str] = mapped_column(
        _enum(*EVENT_SOURCE_KINDS, name="event_source_kind"),
        default="cited",
        server_default="cited",
    )
    # The exact party wording as the source or recorder stated it.  Cited
    # events must not derive this from the affected party.
    stated_party: Mapped[str | None] = mapped_column(Text)
    # The date the event happened, which is not the date it was recorded.
    event_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    timings: Mapped[list["DependencyEventTiming"]] = relationship(
        back_populates="event",
        order_by="DependencyEventTiming.id",
        cascade="all, delete-orphan",
    )
    scope_links: Mapped[list["DependencyEventScope"]] = relationship(
        back_populates="event",
        order_by="DependencyEventScope.dependency_id",
        cascade="all, delete-orphan",
    )
    scope_decisions: Mapped[list["DependencyEventScopeDecision"]] = relationship(
        back_populates="event",
        order_by="DependencyEventScopeDecision.id",
        cascade="all, delete-orphan",
    )

    @property
    def previous_timing(self) -> "DependencyEventTiming | None":
        return next(
            (timing for timing in self.timings if timing.kind == "previous"), None
        )

    @property
    def new_timing(self) -> "DependencyEventTiming | None":
        return next((timing for timing in self.timings if timing.kind == "new"), None)


# The table keeps its historical physical name; domain-facing code can use the
# current ubiquitous language without a destructive table rename.
DependencyEvent = ExternalPartyStatement


class DependencyEventTiming(Base):
    """One source-preserving timing within an External Party statement."""

    __tablename__ = "dependency_event_timings"
    __table_args__ = (
        UniqueConstraint("event_id", "kind", name="uq_dependency_event_timing_kind"),
        CheckConstraint(
            "kind in ('previous', 'new')", name="ck_dependency_event_timing_kind"
        ),
        CheckConstraint(
            "precision in ('day', 'month', 'approximate', 'legacy_unknown')",
            name="ck_dependency_event_timing_precision",
        ),
        CheckConstraint(
            "(precision = 'day' and start_date is not null and end_date = start_date) "
            "or (precision = 'month' and start_date is not null and end_date is not null "
            "and start_date = date_trunc('month', start_date::timestamp)::date "
            "and end_date = (date_trunc('month', start_date::timestamp) "
            "+ interval '1 month - 1 day')::date) "
            "or (precision in ('approximate', 'legacy_unknown') and start_date is null and end_date is null)",
            name="ck_dependency_event_timing_bounds",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    precision: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    event: Mapped[DependencyEvent] = relationship(back_populates="timings")


class DependencyEventMigrationReceipt(Base):
    """The exact legacy event row captured before statement expansion."""

    __tablename__ = "dependency_event_migration_receipts"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("dependency_events.id", ondelete="CASCADE"), primary_key=True
    )
    original_event: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DependencyEventScopeDecision(Base):
    """One attributable, append-only decision about a statement's scope.

    The statement and its scope do not share a lifecycle.  A later placement
    corrects or expands the scope by superseding this decision; it never edits
    the event or relocates one of this decision's links.
    """

    __tablename__ = "dependency_event_scope_decisions"
    __table_args__ = (
        UniqueConstraint(
            "supersedes_scope_decision_id",
            name="uq_dependency_event_scope_decision_supersedes",
        ),
        CheckConstraint(
            "scope_mode in ('unknown', 'selected', 'all_active', 'carried_forward')",
            name="ck_dependency_event_scope_decisions_mode",
        ),
        CheckConstraint(
            "length(trim(decided_by)) > 0",
            name="ck_dependency_event_scope_decisions_actor",
        ),
        Index(
            "uq_dependency_event_scope_decision_root",
            "event_id",
            unique=True,
            postgresql_where=text("supersedes_scope_decision_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    scope_mode: Mapped[str] = mapped_column(String(16))
    supersedes_scope_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_event_scope_decisions.id")
    )
    decided_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    event: Mapped[DependencyEvent] = relationship(back_populates="scope_decisions")
    links: Mapped[list["DependencyEventScope"]] = relationship(
        back_populates="scope_decision",
        order_by="DependencyEventScope.dependency_id",
    )


class DependencyEventScope(Base):
    """One exact Dependency selected by an immutable scope decision."""

    __tablename__ = "dependency_event_scopes"
    __table_args__ = (
        UniqueConstraint(
            "scope_decision_id",
            "dependency_id",
            name="uq_dependency_event_scopes_decision_dependency",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    scope_decision_id: Mapped[int] = mapped_column(
        ForeignKey("dependency_event_scope_decisions.id")
    )
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    recorded_by: Mapped[str] = mapped_column(Text)
    event: Mapped[DependencyEvent] = relationship(back_populates="scope_links")
    scope_decision: Mapped[DependencyEventScopeDecision] = relationship(
        back_populates="links"
    )


class DependencyEventEvidence(Base):
    """One source Evidence identity owned by an External Party statement.

    Citation content remains on ``EvidenceLink`` so every existing foreign key
    keeps its stable identity.  This row establishes its single event owner
    without deriving any Dependency-specific readiness or publication role.
    """

    __tablename__ = "dependency_event_evidence"
    __table_args__ = (
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_dependency_event_evidence_actor",
        ),
    )

    evidence_link_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_links.id"), primary_key=True
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    recorded_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


StatementTimingRecord = DependencyEventTiming
CommitmentScopeDecision = DependencyEventScopeDecision
CommitmentScopeMembership = DependencyEventScope
StatementEvidence = DependencyEventEvidence


class EvidenceLink(Base):
    __tablename__ = "evidence_links"
    __table_args__ = (UniqueConstraint("dependency_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Direct record Evidence owns one Dependency. Statement Evidence leaves
    # this null and is owned by DependencyEventEvidence instead; neither
    # relationship is inferred from missing data.
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    page_no: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)
    # The quote appears on the cited page. Nothing more.
    verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DependencyEvidenceSufficiency(Base):
    """A Dependency-specific sufficiency judgment on direct or event Evidence."""

    __tablename__ = "dependency_evidence_sufficiencies"
    __table_args__ = (
        UniqueConstraint(
            "scope_link_id",
            "evidence_link_id",
            name="uq_dependency_evidence_sufficiency_scope_evidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    evidence_link_id: Mapped[int] = mapped_column(ForeignKey("evidence_links.id"))
    scope_link_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_event_scopes.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OperativeSupport(Base):
    """A human designation of which Evidence supports a publication scope.

    Readiness deliberately does not live here: it remains the independent
    dependency-specific sufficiency judgment.  An event citation can scope to
    several Dependencies, so support constrains the cited Evidence itself and
    the resolver verifies that the selected record is in the event's scope.
    The resolver combines the roles without collapsing their meanings
    (ADR-0017).
    """

    __tablename__ = "operative_support"
    __table_args__ = (
        ForeignKeyConstraint(
            ["evidence_link_id"],
            ["evidence_links.id"],
            name="fk_operative_support_evidence_link",
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
    scope_link_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_event_scopes.id")
    )
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
    current_registration_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "milestone_registrations.id",
            name="fk_milestones_current_registration",
            use_alter=True,
        ),
        deferred=True,
        server_default=text("null"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MilestoneRegistration(Base):
    """One immutable registered revision of a project Milestone."""

    __tablename__ = "milestone_registrations"
    __table_args__ = (
        UniqueConstraint(
            "predecessor_registration_id",
            name="uq_milestone_registrations_predecessor",
        ),
        CheckConstraint(
            "source_sha256 is null or source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_milestone_registrations_source_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(source_row_json) = 'object'",
            name="ck_milestone_registrations_source_row",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_milestone_registrations_recorded_by",
        ),
        Index(
            "uq_milestone_registrations_one_root",
            "milestone_id",
            unique=True,
            postgresql_where=text("predecessor_registration_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"), index=True)
    source_name: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_row_json: Mapped[dict] = mapped_column(JSONB)
    recorded_by: Mapped[str] = mapped_column(String(128))
    predecessor_registration_id: Mapped[int | None] = mapped_column(
        ForeignKey("milestone_registrations.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RetiredDependencyStatus(Base):
    """The unauthoritative legacy status preserved when ADR-0044 retired it."""

    __tablename__ = "retired_dependency_statuses"

    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32))
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkDecisionMilestoneImpact(Base):
    """One exact registered Milestone named by an ``affects`` decision."""

    __tablename__ = "work_decision_milestone_impacts"
    __table_args__ = (
        UniqueConstraint(
            "work_decision_id",
            "milestone_id",
            name="uq_work_decision_milestone_impact",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    work_decision_id: Mapped[int] = mapped_column(ForeignKey("work_decisions.id"))
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"))


class Candidate(Base):
    """An extractor's proposal, not yet part of the Project Record.

    Extractors write only here. Admission requires either human Adjudication or
    one exact deterministic policy outcome with an immutable receipt.
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
    # Admission and disposition lifecycle. A Candidate that fails either check is
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


class EvidenceInvestigationRun(Base):
    """One immutable terminal attempt by the non-authoritative investigator."""

    __tablename__ = "evidence_investigation_runs"
    __table_args__ = (
        CheckConstraint(
            "terminal_status in ('options_available', 'human_judgment_needed', "
            "'abstained', 'failed')",
            name="ck_evidence_investigation_runs_terminal_status",
        ),
        CheckConstraint(
            "length(candidate_payload_sha256) = 64 and "
            "length(transport_gate_sha256) = 64",
            name="ck_evidence_investigation_runs_hashes",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id")
    )
    terminal_status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    transport_gate_sha256: Mapped[str] = mapped_column(String(64))
    candidate_payload_sha256: Mapped[str] = mapped_column(String(64))
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationStepReceipt(Base):
    """Redacted ordered transport/tool metadata for one investigation."""

    __tablename__ = "evidence_investigation_step_receipts"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_investigation_step_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_runs.id"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    opaque_references_json: Mapped[list] = mapped_column(JSONB)
    normalized_arguments_json: Mapped[dict] = mapped_column(JSONB)
    result_summary_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    request_sha256: Mapped[str] = mapped_column(String(64))
    result_sha256: Mapped[str] = mapped_column(String(64))


class EvidenceInvestigationPacketReceipt(Base):
    """Validated structured packet; explicitly never Ledger authority."""

    __tablename__ = "evidence_investigation_packet_receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_runs.id"), unique=True
    )
    packet_json: Mapped[dict] = mapped_column(JSONB)
    validator_outcome: Mapped[str] = mapped_column(String(32))
    packet_sha256: Mapped[str] = mapped_column(String(64))
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )


class EvidenceInvestigationShadowCase(Base):
    """Exact prospective model-visible case frozen before human review."""

    __tablename__ = "evidence_investigation_shadow_cases"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "read_fingerprint",
            "model",
            "prompt_version",
            "prompt_sha256",
            "adapter_contract_version",
            "tool_contract_version",
            name="uq_evidence_investigation_shadow_case_identity",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id")
    )
    candidate_payload_sha256: Mapped[str] = mapped_column(String(64))
    read_fingerprint: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str | None] = mapped_column(String(128))
    transport_gate_sha256: Mapped[str | None] = mapped_column(String(64))
    budget_json: Mapped[dict | None] = mapped_column(JSONB)
    case_json: Mapped[dict] = mapped_column(JSONB)
    registered_evidence_json: Mapped[list] = mapped_column(JSONB)
    option_population_json: Mapped[dict] = mapped_column(JSONB)
    option_population_sha256: Mapped[str] = mapped_column(String(64))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationShadowExecution(Base):
    """Immutable association of one frozen case with its later terminal run."""

    __tablename__ = "evidence_investigation_shadow_executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shadow_case_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_shadow_cases.id"), unique=True
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_runs.id"), unique=True
    )
    execution_status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceInvestigationReviewObservation(Base):
    """Server-observed review boundary, separate from runtime/waiting time."""

    __tablename__ = "evidence_investigation_review_observations"
    __table_args__ = (
        UniqueConstraint(
            "shadow_case_id", "boundary", name="uq_shadow_review_boundary"
        ),
        CheckConstraint(
            "boundary in ('start', 'end')", name="ck_shadow_review_boundary"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shadow_case_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_shadow_cases.id"), index=True
    )
    boundary: Mapped[str] = mapped_column(String(16))
    principal: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationCandidateReviewStart(Base):
    """First ordinary coordinator review observed before any shadow freeze."""

    __tablename__ = "evidence_investigation_candidate_review_starts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id"), unique=True, index=True
    )
    principal: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationShadowOutcome(Base):
    """Later independent human label associated without touching the run."""

    __tablename__ = "evidence_investigation_shadow_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shadow_case_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_shadow_cases.id"), unique=True
    )
    human_outcome_identity: Mapped[str] = mapped_column(String(64), unique=True)
    candidate_disposition: Mapped[str | None] = mapped_column(String(32))
    scope_mode: Mapped[str | None] = mapped_column(String(32))
    selected_dependency_ids_json: Mapped[list] = mapped_column(JSONB)
    correction: Mapped[bool] = mapped_column(Boolean)
    undo: Mapped[bool] = mapped_column(Boolean)
    unresolved: Mapped[bool] = mapped_column(Boolean)
    outcome_identities_json: Mapped[dict] = mapped_column(JSONB)
    strata_json: Mapped[list] = mapped_column(JSONB)
    review_seconds: Mapped[float | None] = mapped_column(Float)
    outcome_sha256: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationEvaluationReceipt(Base):
    """Versioned, immutable deterministic shadow evaluation receipt."""

    __tablename__ = "evidence_investigation_evaluation_receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    evaluation_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    selected_run_ids_json: Mapped[list] = mapped_column(JSONB)
    identity_json: Mapped[dict] = mapped_column(JSONB)
    metrics_json: Mapped[dict] = mapped_column(JSONB)
    strata_json: Mapped[dict] = mapped_column(JSONB)
    human_scores_json: Mapped[dict] = mapped_column(JSONB)
    gates_json: Mapped[dict] = mapped_column(JSONB)
    limitations_json: Mapped[list] = mapped_column(JSONB)
    summary_markdown: Mapped[str] = mapped_column(Text)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandidateDisposition(Base):
    """One human disposition of an Unplaced Statement Candidate.

    Candidate state is the current-work projection.  This append-only record
    preserves why a coordinator accepted a statement or marked it Not Relevant
    without treating either as a mutation of the extractor's Candidate.
    """

    __tablename__ = "candidate_dispositions"
    __table_args__ = (
        CheckConstraint(
            "disposition in ('accepted', 'not_relevant')",
            name="ck_candidate_dispositions_kind",
        ),
        CheckConstraint(
            "(disposition = 'accepted' and reason is null) or "
            "(disposition = 'not_relevant' and reason is not null)",
            name="ck_candidate_dispositions_reason",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_candidate_dispositions_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    disposition: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementCoordinationReceipt(Base):
    """The immutable grouping identity for one guided statement Save.

    The rows named here remain independent statement, scope, Work Decision,
    Evidence, and audit facts.  This receipt only states which exact rows the
    coordinator saved together and which predecessors the screen had read.
    """

    __tablename__ = "statement_coordination_receipts"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(expected_predecessors_json) = 'object'",
            name="ck_statement_coordination_receipt_predecessors_object",
        ),
        CheckConstraint(
            "jsonb_typeof(accepted_facts_json) = 'object'",
            name="ck_statement_coordination_receipt_facts_object",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_statement_coordination_receipt_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    candidate_disposition_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_dispositions.id"), unique=True
    )
    commitment_lineage_id: Mapped[int] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    dependency_event_id: Mapped[int] = mapped_column(
        ForeignKey("dependency_events.id"), unique=True
    )
    scope_decision_id: Mapped[int] = mapped_column(
        ForeignKey("dependency_event_scope_decisions.id"), unique=True
    )
    internal_owner_roster_entry_id: Mapped[int] = mapped_column(
        ForeignKey("project_roster_entries.id")
    )
    internal_owner_decision_id: Mapped[int] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    next_action_decision_id: Mapped[int] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    milestone_impact_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    expected_predecessors_json: Mapped[dict] = mapped_column(JSONB)
    accepted_facts_json: Mapped[dict] = mapped_column(JSONB)
    candidate_payload_sha256: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementCoordinationReversal(Base):
    """The attributable compensating act for one guided result.

    A reversal names either one grouped Save or one Not Relevant disposition.
    It changes only current projections; the source rows and their original
    receipts remain immutable history.
    """

    __tablename__ = "statement_coordination_reversals"
    __table_args__ = (
        CheckConstraint(
            "(receipt_id is not null and candidate_disposition_id is null) or "
            "(receipt_id is null and candidate_disposition_id is not null)",
            name="ck_statement_coordination_reversals_one_source",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_statement_coordination_reversals_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int | None] = mapped_column(
        ForeignKey("statement_coordination_receipts.id"), unique=True
    )
    candidate_disposition_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_dispositions.id"), unique=True
    )
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementCoordinationReversalEffect(Base):
    """One exact result made noncurrent by a compensating command."""

    __tablename__ = "statement_coordination_reversal_effects"
    __table_args__ = (
        CheckConstraint(
            "effect_kind in ("
            "'statement', 'scope_decision', 'work_decision', 'milestone_link', "
            "'candidate_disposition', 'candidate_projection', 'lineage_projection', "
            "'audit_pointer', 'grouping_receipt'"
            ")",
            name="ck_statement_coordination_reversal_effects_kind",
        ),
        UniqueConstraint(
            "reversal_id",
            "effect_kind",
            "target_id",
            name="uq_statement_coordination_reversal_effect",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reversal_id: Mapped[int] = mapped_column(
        ForeignKey("statement_coordination_reversals.id")
    )
    effect_kind: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReconfirmationReceipt(Base):
    """Immutable binding behind one human Reconfirmation audit entry.

    Audit JSON remains the readable history. This sealed copy prevents a
    malformed or edited JSON pointer from making the already-used successor
    Candidate writable again or from inventing a different transferred scope.
    """

    __tablename__ = "reconfirmation_receipts"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(before_json) = 'object'",
            name="ck_reconfirmation_receipt_before_object",
        ),
        CheckConstraint(
            "jsonb_typeof(after_json) = 'object'",
            name="ck_reconfirmation_receipt_after_object",
        ),
    )

    audit_log_id: Mapped[int] = mapped_column(
        ForeignKey("audit_log.id", ondelete="CASCADE"), primary_key=True
    )
    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), index=True
    )
    successor_candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    before_json: Mapped[dict] = mapped_column(JSONB)
    after_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AutomaticCarryForwardReceipt(Base):
    """Immutable machine-transfer identity bound to its readable audit row."""

    __tablename__ = "automatic_carry_forward_receipts"
    __table_args__ = (
        UniqueConstraint(
            "dependency_id",
            "successor_candidate_id",
            name="uq_automatic_carry_forward_dependency_successor",
        ),
        UniqueConstraint(
            "new_evidence_link_id",
            name="uq_automatic_carry_forward_new_evidence",
        ),
        CheckConstraint(
            "jsonb_typeof(before_json) = 'object'",
            name="ck_automatic_carry_forward_receipt_before_object",
        ),
        CheckConstraint(
            "jsonb_typeof(after_json) = 'object'",
            name="ck_automatic_carry_forward_receipt_after_object",
        ),
        CheckConstraint(
            "family = 'automatic-carry-forward'",
            name="ck_automatic_carry_forward_receipt_family",
        ),
        ForeignKeyConstraint(
            ["project_id", "family", "policy_approval_id"],
            [
                "policy_approvals.project_id",
                "policy_approvals.family",
                "policy_approvals.id",
            ],
            name="fk_automatic_carry_forward_receipt_policy_project",
        ),
        ForeignKeyConstraint(
            ["dependency_id", "new_evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_automatic_carry_forward_receipt_dependency_evidence",
        ),
    )

    audit_log_id: Mapped[int] = mapped_column(
        ForeignKey("audit_log.id"), primary_key=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    family: Mapped[str] = mapped_column(
        String(32), server_default="automatic-carry-forward"
    )
    policy_approval_id: Mapped[int | None] = mapped_column(BigInteger)
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), index=True
    )
    comparison_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    finding_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_findings.id")
    )
    predecessor_candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    successor_candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    new_evidence_link_id: Mapped[int] = mapped_column(BigInteger)
    origin_admission_audit_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"))
    predecessor_support_transfer_audit_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_log.id")
    )
    before_json: Mapped[dict] = mapped_column(JSONB)
    after_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AutomaticCarryForwardOutcome(Base):
    """One immutable row outcome within a Carry-Forward batch receipt."""

    __tablename__ = "automatic_carry_forward_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "id",
            name="uq_automatic_carry_forward_outcome_project_id",
        ),
        UniqueConstraint(
            "receipt_audit_log_id",
            name="uq_automatic_carry_forward_outcome_receipt_audit",
        ),
        CheckConstraint(
            "outcome in ('carried', 'abstained')",
            name="ck_automatic_carry_forward_outcome_value",
        ),
        CheckConstraint(
            "("
            "outcome = 'carried' and reason is null and reason_version is null "
            "and receipt_audit_log_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and reason_version is not null and receipt_audit_log_id is null"
            ")",
            name="ck_automatic_carry_forward_outcome_kind",
        ),
        CheckConstraint(
            "family = 'automatic-carry-forward'",
            name="ck_automatic_carry_forward_outcome_family",
        ),
        ForeignKeyConstraint(
            ["project_id", "family", "run_id"],
            [
                "policy_runs.project_id",
                "policy_runs.family",
                "policy_runs.id",
            ],
            name="fk_automatic_carry_forward_outcome_run_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "family", "policy_approval_id"],
            [
                "policy_approvals.project_id",
                "policy_approvals.family",
                "policy_approvals.id",
            ],
            name="fk_automatic_carry_forward_outcome_policy_project",
        ),
        Index(
            "uq_automatic_carry_forward_outcome_abstained_identity",
            "project_id",
            "policy_approval_id",
            "dependency_id",
            text("coalesce(comparison_id, -1)"),
            text("coalesce(finding_id, -1)"),
            text("coalesce(predecessor_candidate_id, -1)"),
            text("coalesce(successor_candidate_id, -1)"),
            "reason",
            "reason_version",
            unique=True,
            postgresql_where=text("outcome = 'abstained'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(
        String(32), server_default="automatic-carry-forward"
    )
    policy_approval_id: Mapped[int | None] = mapped_column(BigInteger)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    outcome: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(128))
    reason_version: Mapped[str | None] = mapped_column(String(64))
    receipt_audit_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("automatic_carry_forward_receipts.audit_log_id")
    )
    comparison_id: Mapped[int | None] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("revision_comparison_findings.id")
    )
    predecessor_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id")
    )
    successor_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DependencyDismissal(Base):
    """One human decision that a record is junk, with the reason.

    Append-only, like every other decision here. Dismissing does not
    delete: the row, its Evidence, its Assertions and its history all
    stay exactly where they are, and `dependencies.dismissed_at` is the
    projection readers filter on — the shape the Committed Date
    projection already takes (ADR-0032). Anyone asking why a conflict
    left the list gets an answer with a name and a date on it.
    """

    __tablename__ = "dependency_dismissals"
    __table_args__ = (
        CheckConstraint(
            "reason in ('duplicate', 'not-a-conflict', 'wrong')",
            name="ck_dependency_dismissals_reason",
        ),
        CheckConstraint(
            "length(trim(dismissed_by)) > 0",
            name="ck_dependency_dismissals_attributable",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    reason: Mapped[str] = mapped_column(String(32))
    dismissed_by: Mapped[str] = mapped_column(Text)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DisputeSettlement(Base):
    """One human decision about what a disputed field concludes.

    A Dispute is a query, not a flag: two revisions asserting different
    verified values for one field. Assertions are append-only, so a
    settlement cannot erase the losing claim and does not try — it
    records what the record concludes and how far its judgment reaches
    (ADR-0031).

    ``covers_assertion_id`` is the newest Assertion for the field at the
    moment of settling. A later revision's claim carries a higher id, so
    it postdates the judgment and the Dispute reopens on its own: a
    reviewer settled the disagreement in front of them, never every
    disagreement that field will ever have.
    """

    __tablename__ = "dispute_settlements"
    __table_args__ = (
        CheckConstraint(
            "length(trim(settled_by)) > 0",
            name="ck_dispute_settlements_attributable",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    field_name: Mapped[str] = mapped_column(String(64))
    # What the record concludes. Null is a legitimate conclusion: a
    # reviewer may settle that the field says nothing.
    settled_value: Mapped[str | None] = mapped_column(Text)
    settled_by: Mapped[str] = mapped_column(Text)
    covers_assertion_id: Mapped[int] = mapped_column(BigInteger)
    settled_at: Mapped[datetime] = mapped_column(
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
