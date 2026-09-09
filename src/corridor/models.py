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

from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
from typing import Any
import sqlalchemy as _legacy_sa
from sqlalchemy.dialects import postgresql as _legacy_pg

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
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

from corridor.fact_types import (
    EFFECTIVE_SINGLE_VALUE_FACT_TYPES,
    PDF_MARKED_RESOLUTION_TRANSFORMATION,
    PDF_TEXT_TRANSFORMATION,
    SINGLE_VALUED_FACT_TYPES,
    STRUCTURED_DATE_FACT_TYPES,
    STRUCTURED_SATELLITE_FACT_TYPES,
    STRUCTURED_TEXT_FACT_TYPES,
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

CANDIDATE_KINDS = ("dependency", "event", "evidence")
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


class CustomerEnvironmentBinding(Base):
    """Immutable local identity checked before customer content is reachable."""

    __tablename__ = "customer_environment_binding"
    __table_args__ = (CheckConstraint("singleton", name="ck_customer_environment_singleton"),)

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    customer_id: Mapped[str] = mapped_column(String(128))
    environment_id: Mapped[str] = mapped_column(String(128))
    deployment_id: Mapped[str] = mapped_column(String(128))


class ClassBRetentionMixin:
    """Explicit TTL state shared only by intermediary assistant receipts."""

    retention_class: Mapped[str] = mapped_column(
        String(16), default="class_b", server_default="class_b"
    )
    retention_content_sha256: Mapped[str | None] = mapped_column(String(64))
    retention_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


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


class IntakeProjectIdentifier(Base):
    """One exact registered routing identifier for the shared intake address.

    A project name is deliberately absent: names are presentation text, never a
    routing key.  A caller records the agency-issued identifier (for example a
    CSJ or contract number) and the router only compares its normalized exact
    value.
    """

    __tablename__ = "intake_project_identifiers"
    __table_args__ = (
        UniqueConstraint("kind", "value_normalized", "project_id"),
        CheckConstraint("length(trim(kind)) > 0", name="ck_intake_identifier_kind"),
        CheckConstraint(
            "length(trim(value_normalized)) > 0", name="ck_intake_identifier_value"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    value_normalized: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PushIntakeCredential(Base):
    """One inbound alias or webhook credential bound to one customer and project.

    ADR-0059 gave the deployment one address and read the project out of the
    message; ADR-0078 replaced that with a binding declared before the bytes
    arrive, and this row is that declaration.  A presented credential resolves
    here first, so nothing in a payload can name the customer or project it
    belongs to.

    Only a one-way digest of the credential material is kept: connector
    credentials live in the control plane (ADR-0079 as amended by ADR-0083),
    and recognizing a presented credential needs nothing more than a digest.
    The database lets a runtime capability insert a credential and revoke one,
    and nothing else, so an alias cannot be re-pointed at another project.
    """

    __tablename__ = "push_intake_credentials"
    __table_args__ = (
        UniqueConstraint(
            "credential_sha256", name="uq_push_intake_credential_digest"
        ),
        CheckConstraint(
            "length(btrim(customer)) > 0", name="ck_push_intake_credential_customer"
        ),
        CheckConstraint(
            "channel in ('project_alias', 'shared_mailbox', 'webhook')",
            name="ck_push_intake_credential_channel",
        ),
        CheckConstraint(
            "state in ('active', 'revoked')", name="ck_push_intake_credential_state"
        ),
        CheckConstraint(
            "credential_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_push_intake_credential_digest",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    customer: Mapped[str] = mapped_column(Text)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    credential_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceDelivery(Base):
    """One delivery, persisted once, whatever transport carried it (ADR-0089).

    ADR-0083 made the ``SourceEnvelope`` common to pull and push, and #511
    persisted only the push half; the pull half had no record at all, so "did
    we take delivery of this external version" was a database read on one
    transport and a re-listing of the customer's own system on the other.
    ADR-0089 calls that an accidental implementation asymmetry and puts both
    transports in this one family, under one identity rule.

    A row is one *outcome* of one delivery, not one attempt at it.  The
    identity ``(project_id, delivery_identity, content_sha256, disposition)``
    is unique and every column of it is ``not null``, so the record answers
    what arrived, what became of it, and why, and a replay converges on the
    row it already wrote.  ``delivery_identity`` and ``idempotency_key`` are
    re-derived from the row's own columns by a trigger, so a writer that
    derives them wrongly is refused rather than silently trusted (#457).

    What was considered and rejected: one row per *attempt*, discriminated by
    the run that made it.  It reads as the more literal ledger, but the run
    identity is fresh on every pass, so the delivery identity would never be
    unique and the relation would be a log rather than a record.  Attempts are
    already recorded — the Due Work receipt for a pull pass, the transport's
    own delivery for a push — and what is missing is the delivery.
    """

    __tablename__ = "source_deliveries"
    __table_args__ = (
        # The envelope's structural key, so dedup no longer rests on the
        # derived digest having been derived correctly (#457), extended by
        # ADR-0089 with the disposition: one delivery may be stored, and later
        # found duplicate, and it is the same delivery each time.
        UniqueConstraint(
            "project_id",
            "delivery_identity",
            "content_sha256",
            "disposition",
            name="uq_source_deliveries_observation",
        ),
        # The project-scoped identity a Document's delivery link points at, so
        # one customer's document can never name another customer's delivery
        # (#675).
        UniqueConstraint("id", "project_id", name="uq_source_deliveries_row"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_delivery_content_sha256",
        ),
        CheckConstraint(
            "delivery_identity ~ '^[0-9a-f]{64}$'", name="ck_source_delivery_identity"
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'", name="ck_source_delivery_idempotency"
        ),
        CheckConstraint(
            "transport in ('pull', 'push')", name="ck_source_delivery_transport"
        ),
        CheckConstraint(
            "disposition in ('stored', 'duplicate', 'quarantined', "
            "'terminally_refused', 'transient_failure')",
            name="ck_source_delivery_disposition",
        ),
        # A pushed delivery is bound by its credential and a pulled one is not;
        # neither may borrow the other's binding.
        CheckConstraint(
            "(transport = 'push') = (credential_id is not null)",
            name="ck_source_delivery_push_credential",
        ),
        CheckConstraint(
            "length(btrim(configuration_identity)) > 0",
            name="ck_source_delivery_configuration",
        ),
        CheckConstraint(
            "length(btrim(service_identity)) > 0 and length(btrim(run_identity)) > 0",
            name="ck_source_delivery_run",
        ),
        # The refusal evidence the checkpoint rule turns on: a delivery that
        # was refused, held, or failed says why, and one that was taken has
        # nothing to say (ADR-0089).
        CheckConstraint(
            "(disposition in ('stored', 'duplicate')) = (refusal_reason is null)",
            name="ck_source_delivery_refusal_reason",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("push_intake_credentials.id")
    )
    customer: Mapped[str] = mapped_column(Text)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    transport: Mapped[str] = mapped_column(String(8))
    channel: Mapped[str] = mapped_column(String(32))
    # The connector or channel configuration this delivery arrived under, and
    # its version: the server-owned connector identity for a pull, the bound
    # credential for a push.
    configuration_identity: Mapped[str] = mapped_column(Text)
    configuration_version: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    external_identity: Mapped[str] = mapped_column(Text)
    external_version: Mapped[str] = mapped_column(Text)
    original_timestamps_json: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    # The object-store key for bytes that were taken, the quarantine reference
    # for bytes that are held, and empty for a delivery whose bytes Corridor
    # never kept.
    bytes_reference: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    delivery_identity: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    service_identity: Mapped[str] = mapped_column(Text)
    run_identity: Mapped[str] = mapped_column(Text)
    disposition: Mapped[str] = mapped_column(String(24))
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConnectorCheckpointAdvance(Base):
    """One durable advance of one connector configuration's external cursor.

    #488 kept the token on the completed Due Work receipt of the pass that
    reached it, and said so in its own docstring: a checkpoint table was
    rejected because the migration window was closed.  ADR-0089 reverses that.
    Retention is not identity — a receipt sweep, a retention-policy change, or
    an ordinary cleanup would have reset a live connector's external cursor, or
    landed it on a stale token — so the cursor lives with the *configuration*,
    which nothing sweeps, and the current checkpoint is derived from the
    newest advance rather than stored anywhere.

    Ordered by identity, not by ``advanced_at``: the identifier is monotonic in
    insertion order whatever any clock said, and "current" here must mean the
    last one recorded.
    """

    __tablename__ = "connector_checkpoint_advances"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "run_identity",
            "checkpoint_token",
            name="uq_connector_checkpoint_advance",
        ),
        CheckConstraint(
            "length(btrim(checkpoint_token)) > 0",
            name="ck_connector_checkpoint_token",
        ),
        CheckConstraint(
            "length(btrim(service_identity)) > 0 and length(btrim(run_identity)) > 0",
            name="ck_connector_checkpoint_run",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_schedules.id"), index=True
    )
    configuration_identity: Mapped[str] = mapped_column(Text)
    configuration_version: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    channel: Mapped[str] = mapped_column(String(32))
    checkpoint_token: Mapped[str] = mapped_column(Text)
    service_identity: Mapped[str] = mapped_column(Text)
    run_identity: Mapped[str] = mapped_column(Text)
    advanced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConnectorCheckpointAdvanceDelivery(Base):
    """One delivery an advance covered, and the reason the advance was safe.

    ADR-0089's checkpoint rule is a property of what an advance covers, so it
    is enforced where the coverage is written: a trigger refuses a
    ``transient_failure``, because a scanner that timed out or a provider that
    returned a 500 has said nothing about the delivery, and refuses a refused
    or quarantined delivery whose evidence is not durable.
    """

    __tablename__ = "connector_checkpoint_advance_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "advance_id",
            "delivery_id",
            name="uq_connector_checkpoint_advance_delivery",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    advance_id: Mapped[int] = mapped_column(
        ForeignKey("connector_checkpoint_advances.id"), index=True
    )
    delivery_id: Mapped[int] = mapped_column(ForeignKey("source_deliveries.id"))


class InboundThread(Base):
    """One header-connected conversation, never reconstructed by content."""

    __tablename__ = "inbound_threads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Null is the honest unresolved state.  A triage answer fills this once;
    # reply inheritance reads it but never guesses it.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    # Binding to a Constraint is reserved for the exact matcher.  Email intake
    # records the durable provenance seam without inferring a relationship.
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    bound_by_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_messages.id", use_alter=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InboundMessage(Base):
    """An immutable raw inbound email and the deterministic route it received."""

    __tablename__ = "inbound_messages"
    # Message identity is scoped to the project boundary (#511, ADR-0078).
    # The same bytes, or the same Message-ID, delivered to two projects are two
    # deliveries: a cross-project constraint would hand one customer's stored
    # message back to another customer's alias, or let a guessed Message-ID
    # refuse a delivery in a project the sender cannot see.
    __table_args__ = (
        UniqueConstraint(
            "project_id", "raw_sha256", name="uq_inbound_message_project_bytes"
        ),
        UniqueConstraint(
            "project_id", "message_id", name="uq_inbound_message_project_message_id"
        ),
        UniqueConstraint(
            "push_delivery_id", name="uq_inbound_message_push_delivery"
        ),
        CheckConstraint(
            "raw_sha256 ~ '^[0-9a-f]{64}$'", name="ck_inbound_message_sha256"
        ),
        CheckConstraint(
            "route_status in ('routed', 'triage')", name="ck_inbound_message_route"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    headers_json: Mapped[dict] = mapped_column(JSONB)
    body_text: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[int] = mapped_column(ForeignKey("inbound_threads.id"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    route_status: Mapped[str] = mapped_column(String(16))
    route_evidence_json: Mapped[dict] = mapped_column(JSONB)
    # Filled when the routed message registers as a prose source Document.
    # A triaged message stays unregistered until its thread routes; the raw
    # bytes at storage_path are the crash-safe original either way.
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    # Per-attachment registration receipts: filename, sha256, and either the
    # registered document id or the exact shared-intake refusal reason.
    attachments_json: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    # The one push delivery this message arrived on (#511). Null for a message
    # the frozen global-address path received.
    push_delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_deliveries.id")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InboundThreadReading(Base):
    """The one durable outcome of reading a bound thread's conversation arc.

    ADR-0062: a concluded conversation proposes exactly one claim (a pending
    Candidate under ordinary admission, cited to the closing turn); an
    unresolved conversation proposes zero claims and one open question in the
    thread's own words.  One row per thread reading attempt over a fixed last
    turn, so re-reading an unchanged thread is idempotent and a longer thread
    reads again.
    """

    __tablename__ = "inbound_thread_readings"
    __table_args__ = (
        UniqueConstraint("thread_id", "closing_message_id"),
        UniqueConstraint("project_id", "id", name="uq_inbound_thread_readings_project_id"),
        CheckConstraint(
            "resolution in ('concluded', 'unresolved')",
            name="ck_inbound_thread_reading_resolution",
        ),
        CheckConstraint(
            "(resolution = 'concluded') = (candidate_id is not null or source_fact_id is not null)",
            name="ck_inbound_thread_reading_claim",
        ),
        CheckConstraint(
            "(resolution = 'unresolved') = (open_question is not null)",
            name="ck_inbound_thread_reading_question",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    thread_id: Mapped[int] = mapped_column(
        ForeignKey("inbound_threads.id"), index=True
    )
    closing_message_id: Mapped[int] = mapped_column(ForeignKey("inbound_messages.id"))
    resolution: Mapped[str] = mapped_column(String(16))
    # The one proposed claim of a concluded conversation — a pending Candidate
    # that enters the record only through ordinary admission.
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"))
    source_fact_id: Mapped[int | None] = mapped_column(ForeignKey("facts.id"))
    proposed_delta_id: Mapped[int | None] = mapped_column(ForeignKey("proposed_deltas.id"))
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    question_segment_id: Mapped[int | None] = mapped_column(ForeignKey("source_segments.id"))
    # The one open question of an unresolved conversation, in the thread's own
    # words, on the standing owner's list for the bound row.
    open_question: Mapped[str | None] = mapped_column(Text)
    turn_context_json: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InboundRouteTriage(Base):
    """The one human routing residue for an ambiguous or blank thread."""

    __tablename__ = "inbound_route_triage"
    __table_args__ = (
        UniqueConstraint("thread_id"),
        CheckConstraint(
            "state in ('pending', 'resolved')", name="ck_inbound_route_triage_state"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("inbound_threads.id"))
    candidate_project_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default="{}"
    )
    state: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    resolved_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


DELTA_CHANGE_TYPES = ("add", "modify", "apparent_removal")
DELTA_TARGET_TYPES = ("existing_subject", "proposed_subject")
DELTA_DISPOSITIONS = ("accept", "edit", "reject")
# The typed effect one resolved delta has on the accepted record (#519,
# ADR-0076 as amended by ADR-0083 and ADR-0084).
DELTA_EFFECT_KINDS = (
    "new_subject",
    "changed_field",
    "timing",
    "organization",
    "apparent_removal",
    "contradiction",
    "schedule_key_date",
    "closure",
)
DELTA_ORGANIZATION_CHANGE_KINDS = ("correction", "changed_ownership")
_DELTA_EFFECT_KINDS_SQL = ", ".join(f"'{value}'" for value in DELTA_EFFECT_KINDS)


class DeltaGroup(Base):
    """One atomic source change binding proposed deltas for source lineage (#518)."""

    __tablename__ = "delta_groups"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_delta_groups_project_id"),
        # One atomic source change per source version (#457): replaying a
        # version, or taking it in more than one batch, joins the group that
        # version already opened instead of leaving another behind. The
        # document and the statement are part of the identity and either may
        # be absent, so two absences are the same absence.
        UniqueConstraint(
            "project_id",
            "source_family",
            "source_revision",
            "document_id",
            "statement_id",
            name="uq_delta_groups_source_change",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_family: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[str] = mapped_column(String(128))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    statement_id: Mapped[int | None] = mapped_column(ForeignKey("dependency_events.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProposedDelta(Base):
    """One immutable occurrence of a proposed delta against accepted record (#518)."""

    __tablename__ = "proposed_deltas"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_proposed_deltas_project_id"),
        UniqueConstraint("content_sha256", name="uq_proposed_deltas_content"),
        ForeignKeyConstraint(
            ["project_id", "group_id"],
            ["delta_groups.project_id", "delta_groups.id"],
            name="fk_proposed_deltas_group",
        ),
        CheckConstraint(
            "change_type in ('add', 'modify', 'apparent_removal')",
            name="ck_proposed_deltas_change_type",
        ),
        CheckConstraint(
            "target_type in ('existing_subject', 'proposed_subject')",
            name="ck_proposed_deltas_target_type",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_proposed_deltas_content_sha256",
        ),
        Index("ix_proposed_deltas_target", "project_id", "target_subject_identity"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    change_type: Mapped[str] = mapped_column(String(32))
    target_type: Mapped[str] = mapped_column(String(32))
    target_subject_identity: Mapped[str] = mapped_column(String(128))
    target_field: Mapped[str | None] = mapped_column(String(64))
    accepted_value: Mapped[Any | None] = mapped_column(JSONB)
    proposed_value: Mapped[Any | None] = mapped_column(JSONB)
    source_family: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[str] = mapped_column(String(128))
    comparison_rule_version: Mapped[str] = mapped_column(String(64))
    accepted_baseline_revision: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProposedDeltaImpactDerivation(Base):
    """Immutable versioned consequences, never Source Facts or accepted values."""

    __tablename__ = "proposed_delta_impact_derivations"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"]),
        UniqueConstraint("project_id", "delta_id", "rule", "rule_version", "input_sha256"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    delta_id: Mapped[int] = mapped_column(BigInteger)
    rule: Mapped[str] = mapped_column(Text)
    rule_version: Mapped[str] = mapped_column(Text)
    accepted_revision_id: Mapped[int | None] = mapped_column(ForeignKey("project_record_revisions.id"))
    inputs: Mapped[dict] = mapped_column(JSONB)
    input_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    affected_constraint_ids: Mapped[list] = mapped_column(JSONB)
    affected_key_dates: Mapped[list] = mapped_column(JSONB)
    derivation_sha256: Mapped[str] = mapped_column(String(64))


class DeltaDisposition(Base):
    """Semantic resolution (accept, edit, reject) of a proposed delta (#518)."""

    __tablename__ = "delta_dispositions"
    __table_args__ = (
        UniqueConstraint("delta_id", name="uq_delta_dispositions_delta"),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_dispositions_delta",
        ),
        CheckConstraint(
            "disposition in ('accept', 'edit', 'reject')",
            name="ck_delta_dispositions_disposition",
        ),
        CheckConstraint(
            "(decided_by_principal is null) <> (decided_by_policy is null)",
            name="ck_delta_dispositions_authority_xor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    disposition: Mapped[str] = mapped_column(String(32))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by_principal: Mapped[str | None] = mapped_column(String(128))
    decided_by_policy: Mapped[str | None] = mapped_column(String(128))
    rationale: Mapped[str | None] = mapped_column(Text)
    effective_value: Mapped[Any | None] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaSupersession(Base):
    """Lineage link when a newer source revision supersedes a prior delta (#518)."""

    __tablename__ = "delta_supersessions"
    __table_args__ = (
        UniqueConstraint("prior_delta_id", name="uq_delta_supersessions_prior"),
        ForeignKeyConstraint(
            ["project_id", "source_reading_id"],
            ["inbound_thread_readings.project_id", "inbound_thread_readings.id"],
            use_alter=True, name="fk_delta_supersessions_reading_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "prior_delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_supersessions_prior",
        ),
        ForeignKeyConstraint(
            ["project_id", "superseding_delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_supersessions_superseding",
        ),
        CheckConstraint(
            "prior_delta_id <> superseding_delta_id",
            name="ck_delta_supersessions_not_self",
        ),
        ForeignKeyConstraint(["project_id", "minutes_capture_id"], ["minutes_captures.project_id", "minutes_captures.id"],
                             use_alter=True, name="fk_delta_supersession_minutes"),
        CheckConstraint("superseding_delta_id is not null or source_reading_id is not null or minutes_capture_id is not null",
                        name="ck_delta_supersessions_successor"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    prior_delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    superseding_delta_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    source_reading_id: Mapped[int | None] = mapped_column(BigInteger)
    minutes_capture_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(64))
    superseded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaDeferral(Base):
    """Attributable Work List scheduling leaving delta open (#518, ADR-0035)."""

    __tablename__ = "delta_deferrals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_deferrals_delta",
        ),
        # Scheduling writes no Project Record revision (ADR-0084), so the act
        # carries no idempotency key; the delta, the instant it was scheduled
        # at, and the person who scheduled it are its identity, and a retried
        # Defer returns the receipt already written (#457).
        UniqueConstraint(
            "delta_id",
            "deferred_at",
            "scheduled_by_principal",
            name="uq_delta_deferrals_occurrence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    deferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deferred_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wake_condition: Mapped[str | None] = mapped_column(String(128))
    scheduled_by_principal: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaRecordDecision(Base):
    """The authority binding of one resolved Proposed Delta (#519).

    ``DeltaDisposition`` says the delta resolved; this row says by whose
    authority, in which Project Record revision, with which typed effect, and
    — for an edit — on which constrained basis the edited value is still
    source-backed.  Written only by ``resolve_proposed_delta_decision``; a
    wrong decision is corrected by a later attributable decision, never by an
    update (ADR-0076, ADR-0084).
    """

    __tablename__ = "delta_record_decisions"
    __table_args__ = (
        UniqueConstraint(
            "disposition_id", name="uq_delta_record_decisions_disposition"
        ),
        UniqueConstraint("delta_id", name="uq_delta_record_decisions_delta"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_delta_record_decisions_key"
        ),
        UniqueConstraint(
            "project_id", "id", name="uq_delta_record_decisions_project_id"
        ),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_record_decisions_delta",
        ),
        CheckConstraint(
            "disposition in ('accept', 'edit', 'reject')",
            name="ck_delta_record_decisions_disposition",
        ),
        CheckConstraint(
            f"effect_kind in ({_DELTA_EFFECT_KINDS_SQL})",
            name="ck_delta_record_decisions_effect_kind",
        ),
        CheckConstraint(
            "(effect_kind = 'organization' and organization_change_kind in "
            "('correction', 'changed_ownership')) or "
            "(effect_kind <> 'organization' and organization_change_kind is null)",
            name="ck_delta_record_decisions_organization",
        ),
        CheckConstraint(
            "(disposition = 'edit') = (edit_basis is not null)",
            name="ck_delta_record_decisions_edit_basis",
        ),
        CheckConstraint(
            "length(btrim(decided_by_principal)) > 0",
            name="ck_delta_record_decisions_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    delta_id: Mapped[int] = mapped_column(BigInteger)
    disposition_id: Mapped[int] = mapped_column(
        ForeignKey("delta_dispositions.id")
    )
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("project_record_revisions.id"), index=True
    )
    disposition: Mapped[str] = mapped_column(String(32))
    effect_kind: Mapped[str] = mapped_column(String(32))
    organization_change_kind: Mapped[str | None] = mapped_column(String(32))
    decided_by_principal: Mapped[str] = mapped_column(String(128))
    observed_accepted_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    edit_basis: Mapped[Any | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaDecisionSupport(Base):
    """One effective Support Assessment a delta decision relied on (#519, #530).

    The decision names its support; a passed Source Passage Check is never
    read here and can never stand in for it (ADR-0082).
    """

    __tablename__ = "delta_decision_supports"
    __table_args__ = (
        UniqueConstraint(
            "decision_id",
            "support_assessment_id",
            name="uq_delta_decision_supports_member",
        ),
        UniqueConstraint(
            "decision_id", "ordinal", name="uq_delta_decision_supports_ordinal"
        ),
        ForeignKeyConstraint(
            ["project_id", "decision_id"],
            [
                "delta_record_decisions.project_id",
                "delta_record_decisions.id",
            ],
            name="fk_delta_decision_supports_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "support_assessment_id"],
            ["support_assessments.project_id", "support_assessments.id"],
            name="fk_delta_decision_supports_assessment",
        ),
        CheckConstraint("ordinal > 0", name="ck_delta_decision_supports_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    decision_id: Mapped[int] = mapped_column(BigInteger)
    support_assessment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


# The five outcomes one Review Packet child may carry (#526, ADR-0085).  The
# first four are ADR-0085's primary decisions; the fifth is its secondary
# dated Defer, which is Work List scheduling and not a semantic disposition
# (ADR-0084).
PACKET_CHILD_OUTCOMES = (
    "apply",
    "keep_current",
    "edit_and_apply",
    "needs_coordination",
    "defer",
)
PACKET_SEMANTIC_OUTCOMES = ("apply", "keep_current", "edit_and_apply")
# How a packet was keyed, so the receipt preserves the grouping basis a later
# reader would otherwise have to guess at (ADR-0085).
PACKET_GROUPING_KEY_KINDS = (
    "source_revision",
    "coordination_question",
    "shared_commitment",
)
_PACKET_CHILD_OUTCOMES_SQL = ", ".join(f"'{value}'" for value in PACKET_CHILD_OUTCOMES)
_PACKET_GROUPING_KEY_KINDS_SQL = ", ".join(
    f"'{value}'" for value in PACKET_GROUPING_KEY_KINDS
)


class DeltaFollowUpPlan(Base):
    """The Follow-up Plan decision a Needs coordination outcome records (#526).

    ADR-0085 keeps Needs coordination among the four primary packet decisions
    and ADR-0084 forbids settling an external fact with free text, so the
    coordinator's answer to "I cannot settle this yet" has to be a recorded
    decision rather than a dropped selection.  The row is a separately
    identified decision inside the packet's one Project Record revision: it
    names the exact question, the person or organization who owes the answer,
    the date the question returns, the scope it affects, and the evidence that
    raised it.  It leaves the proposed value unaccepted and the Proposed Delta
    open, which is why it writes no ``delta_dispositions`` row.

    This is the spine's Follow-up Plan.  ``follow_up_plan_receipts`` is the
    frozen legacy grouping receipt over ``work_decisions`` (ADR-0081) and is
    not extended for adopted-baseline projects (ADR-0084 §3).
    """

    __tablename__ = "delta_follow_up_plans"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_delta_follow_up_plans_project_id"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_delta_follow_up_plans_key"
        ),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_follow_up_plans_delta",
        ),
        CheckConstraint(
            "length(btrim(open_question)) > 0",
            name="ck_delta_follow_up_plans_question",
        ),
        CheckConstraint(
            "responsible_principal is not null "
            "or responsible_organization is not null",
            name="ck_delta_follow_up_plans_responsible",
        ),
        CheckConstraint(
            "jsonb_typeof(affected_scope) = 'object'",
            name="ck_delta_follow_up_plans_scope_object",
        ),
        CheckConstraint(
            "length(btrim(recorded_by_principal)) > 0",
            name="ck_delta_follow_up_plans_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("project_record_revisions.id"), index=True
    )
    open_question: Mapped[str] = mapped_column(Text)
    responsible_principal: Mapped[str | None] = mapped_column(String(128))
    responsible_organization: Mapped[str | None] = mapped_column(String(255))
    return_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    affected_scope: Mapped[Any] = mapped_column(JSONB)
    recorded_by_principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaFollowUpPlanEvidence(Base):
    """One effective Support Assessment a Follow-up Plan cited (#526, #530)."""

    __tablename__ = "delta_follow_up_plan_evidence"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "support_assessment_id",
            name="uq_delta_follow_up_plan_evidence_member",
        ),
        UniqueConstraint(
            "plan_id", "ordinal", name="uq_delta_follow_up_plan_evidence_ordinal"
        ),
        ForeignKeyConstraint(
            ["project_id", "plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_delta_follow_up_plan_evidence_plan",
        ),
        ForeignKeyConstraint(
            ["project_id", "support_assessment_id"],
            ["support_assessments.project_id", "support_assessments.id"],
            name="fk_delta_follow_up_plan_evidence_assessment",
        ),
        CheckConstraint("ordinal > 0", name="ck_delta_follow_up_plan_evidence_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    plan_id: Mapped[int] = mapped_column(BigInteger)
    support_assessment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


class OutgoingRequest(Base):
    """A retained outgoing request, and the response boundary it declared (#652).

    The chase list's ``unanswered_request`` band is the one place Corridor may
    say a specific thing has gone unanswered, and ADR-0090 retired the legacy
    ``STALE`` alert precisely because silence is not evidence: nobody sending a
    document does not mean anybody failed to answer.  A no-response fact
    therefore needs a *retained request* behind it, and until this table existed
    ``follow_up_bundles.read_retained_outgoing_requests`` truthfully returned
    nothing.  This is that record: what was asked, of which External
    Organization, covering which Utility Conflicts, its exact sent bytes or a
    digest of them, the declared expected-response boundary (silence before it
    is not a finding), the attributable sender and day, and the Follow-up Plan
    the request advances.

    It is Corridor-originated correspondence, not source-derived evidence and
    not an accepted-record decision, so it does not join the spine's append
    matrix.  It is written only through ``append_outgoing_request`` under the
    record-decision role, and it is append-only: a guard trigger refuses every
    update, delete, and truncate, and refuses an insert that does not arrive as
    that role, so not even the schema owner can write one raw (#492 idiom).

    ``sent_bytes`` is the exact request when Corridor kept it and null when it
    did not — whoever sent it, by whatever means, may not have retained the
    bytes — so ``content_sha256`` is the digest that stands on either footing,
    the same choice ``SourceDelivery`` makes for a delivery whose bytes were
    never kept.  ``expected_response_by`` is the resolved boundary the band
    reads; ``boundary_rule_version`` and ``boundary_interval_days`` record how
    it was derived when an interval and a rule produced it rather than a date
    stated outright.
    """

    __tablename__ = "outgoing_requests"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_outgoing_requests_project_id"),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_outgoing_requests_key"
        ),
        ForeignKeyConstraint(
            ["project_id", "follow_up_plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_outgoing_requests_plan",
        ),
        CheckConstraint(
            "length(btrim(external_organization)) > 0",
            name="ck_outgoing_requests_organization",
        ),
        CheckConstraint(
            "length(btrim(question)) > 0", name="ck_outgoing_requests_question"
        ),
        CheckConstraint(
            "length(btrim(sent_by_principal)) > 0",
            name="ck_outgoing_requests_principal",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_outgoing_requests_digest"
        ),
        CheckConstraint(
            "expected_response_by >= sent_on", name="ck_outgoing_requests_boundary"
        ),
        CheckConstraint(
            "jsonb_typeof(covered_subject_keys) = 'array'",
            name="ck_outgoing_requests_subjects",
        ),
        CheckConstraint(
            "sent_bytes is null or octet_length(sent_bytes) > 0",
            name="ck_outgoing_requests_bytes",
        ),
        CheckConstraint(
            "boundary_interval_days is null or boundary_interval_days >= 0",
            name="ck_outgoing_requests_interval",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    follow_up_plan_id: Mapped[int] = mapped_column(BigInteger, index=True)
    external_organization: Mapped[str] = mapped_column(String(255))
    responsible_role: Mapped[str | None] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text)
    covered_subject_keys: Mapped[Any] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    sent_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    sent_on: Mapped[date] = mapped_column(Date)
    sent_by_principal: Mapped[str] = mapped_column(String(128))
    expected_response_by: Mapped[date] = mapped_column(Date)
    boundary_rule_version: Mapped[str | None] = mapped_column(String(64))
    boundary_interval_days: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def digest_is_valid(self) -> bool:
        """Whether retained bytes still match the digest; true when none were kept."""
        if self.sent_bytes is None:
            return True
        return sha256(self.sent_bytes).hexdigest() == self.content_sha256


class OutgoingRequestResponse(Base):
    """A received response that stops one retained request's silence clock (#652).

    Recording that a reply arrived, at least enough to stop the no-response
    clock: the band fires only for a retained request whose boundary has passed
    *and* which has no recorded response as of the reading's cutoff.  Full
    receipt and delivery tracking is deliberately out of scope (#652); this row
    exists to make "they answered" a fact the chase list can read.  It is
    append-only and written only through ``append_outgoing_request_response``
    under the record-decision role, held by the same guard trigger as the
    request it answers.  One response per request stops the clock; the
    command converges a replay on the row it already wrote.
    """

    __tablename__ = "outgoing_request_responses"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "request_id", name="uq_outgoing_request_responses_request"
        ),
        ForeignKeyConstraint(
            ["project_id", "request_id"],
            ["outgoing_requests.project_id", "outgoing_requests.id"],
            name="fk_outgoing_request_responses_request",
        ),
        CheckConstraint(
            "length(btrim(recorded_by_principal)) > 0",
            name="ck_outgoing_request_responses_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    request_id: Mapped[int] = mapped_column(BigInteger, index=True)
    received_on: Mapped[date] = mapped_column(Date)
    recorded_by_principal: Mapped[str] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaReviewPacketReceipt(Base):
    """The one receipt for one guided Review Packet act (#526, ADR-0085).

    A Review Packet is a derived presentation and never an authoritative
    record (ADR-0085), so nothing here stores a packet's membership as state
    a later reading must reconcile.  What this row preserves is the *act*: the
    grouping rule and key the coordinator was shown, the human principal, the
    accepted revision they had read, the optional one Project Record revision
    the act produced, and — through ``delta_review_packet_children`` — the
    exact ordered child set with one outcome and one decision, plan, or
    deferral identity each.

    ``revision_id`` is null exactly when every child was a dated Defer: a
    scheduling-only act writes no Project Record revision (ADR-0084,
    ADR-0085).
    """

    __tablename__ = "delta_review_packet_receipts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "id", name="uq_delta_review_packet_receipts_project_id"
        ),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_delta_review_packet_receipts_key"
        ),
        CheckConstraint(
            f"grouping_key_kind in ({_PACKET_GROUPING_KEY_KINDS_SQL})",
            name="ck_delta_review_packet_receipts_key_kind",
        ),
        CheckConstraint(
            "length(btrim(grouping_rule_version)) > 0",
            name="ck_delta_review_packet_receipts_rule_version",
        ),
        CheckConstraint(
            "length(btrim(decided_by_principal)) > 0",
            name="ck_delta_review_packet_receipts_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id"), index=True
    )
    grouping_rule_version: Mapped[str] = mapped_column(String(64))
    grouping_key_kind: Mapped[str] = mapped_column(String(32))
    grouping_key: Mapped[str] = mapped_column(String(255))
    decided_by_principal: Mapped[str] = mapped_column(String(128))
    observed_accepted_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DeltaReviewPacketChild(Base):
    """One child delta of one packet act, with its own retained identity (#526).

    ADR-0035 forbids one Save collapsing the identity of the distinct domain
    acts inside it, so the receipt does not summarize its children: each row
    names the exact Proposed Delta, the position it was shown in, the outcome
    the coordinator chose, and the one identity that outcome produced.
    """

    __tablename__ = "delta_review_packet_children"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id", "ordinal", name="uq_delta_review_packet_children_ordinal"
        ),
        UniqueConstraint(
            "receipt_id", "delta_id", name="uq_delta_review_packet_children_delta"
        ),
        ForeignKeyConstraint(
            ["project_id", "receipt_id"],
            [
                "delta_review_packet_receipts.project_id",
                "delta_review_packet_receipts.id",
            ],
            name="fk_delta_review_packet_children_receipt",
        ),
        ForeignKeyConstraint(
            ["project_id", "delta_id"],
            ["proposed_deltas.project_id", "proposed_deltas.id"],
            name="fk_delta_review_packet_children_delta",
        ),
        ForeignKeyConstraint(
            ["project_id", "decision_id"],
            ["delta_record_decisions.project_id", "delta_record_decisions.id"],
            name="fk_delta_review_packet_children_decision",
        ),
        ForeignKeyConstraint(
            ["project_id", "follow_up_plan_id"],
            ["delta_follow_up_plans.project_id", "delta_follow_up_plans.id"],
            name="fk_delta_review_packet_children_plan",
        ),
        CheckConstraint(
            f"outcome in ({_PACKET_CHILD_OUTCOMES_SQL})",
            name="ck_delta_review_packet_children_outcome",
        ),
        CheckConstraint("ordinal > 0", name="ck_delta_review_packet_children_ordinal"),
        # One outcome, one identity: a semantic child names its Human Record
        # Decision, a Needs coordination child its Follow-up Plan, and a dated
        # Defer its scheduling receipt. No child names two, and none names none.
        CheckConstraint(
            "(case when decision_id is null then 0 else 1 end) "
            "+ (case when follow_up_plan_id is null then 0 else 1 end) "
            "+ (case when deferral_id is null then 0 else 1 end) = 1",
            name="ck_delta_review_packet_children_one_identity",
        ),
        CheckConstraint(
            "(outcome in ('apply', 'keep_current', 'edit_and_apply')) "
            "= (decision_id is not null)",
            name="ck_delta_review_packet_children_semantic",
        ),
        CheckConstraint(
            "(outcome = 'needs_coordination') = (follow_up_plan_id is not null)",
            name="ck_delta_review_packet_children_coordination",
        ),
        CheckConstraint(
            "(outcome = 'defer') = (deferral_id is not null)",
            name="ck_delta_review_packet_children_defer",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    delta_id: Mapped[int] = mapped_column(BigInteger, index=True)
    outcome: Mapped[str] = mapped_column(String(32))
    observed_source_revision: Mapped[str] = mapped_column(String(128))
    decision_id: Mapped[int | None] = mapped_column(BigInteger)
    follow_up_plan_id: Mapped[int | None] = mapped_column(BigInteger)
    deferral_id: Mapped[int | None] = mapped_column(
        ForeignKey("delta_deferrals.id"), unique=True
    )


class DeltaReviewPacketSupport(Base):
    """One effective Support Assessment the whole packet act relied on (#526)."""

    __tablename__ = "delta_review_packet_supports"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            "support_assessment_id",
            name="uq_delta_review_packet_supports_member",
        ),
        UniqueConstraint(
            "receipt_id", "ordinal", name="uq_delta_review_packet_supports_ordinal"
        ),
        ForeignKeyConstraint(
            ["project_id", "receipt_id"],
            [
                "delta_review_packet_receipts.project_id",
                "delta_review_packet_receipts.id",
            ],
            name="fk_delta_review_packet_supports_receipt",
        ),
        ForeignKeyConstraint(
            ["project_id", "support_assessment_id"],
            ["support_assessments.project_id", "support_assessments.id"],
            name="fk_delta_review_packet_supports_assessment",
        ),
        CheckConstraint("ordinal > 0", name="ck_delta_review_packet_supports_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger)
    support_assessment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


class DeltaReviewPacketReversal(Base):
    """The compensating act for one guided packet Save (#526, ADR-0035).

    Undo never deletes and never cascades into later work.  It appends one
    compensating Project Record revision that restores each predecessor
    accepted decision the packet superseded, and names the receipt it
    compensates so the original act, its children, and its history all stay
    exactly as they were recorded.  A packet whose every child was a dated
    Defer has no revision to compensate; reversing it only releases those
    scheduling receipts, so ``revision_id`` is null.
    """

    __tablename__ = "delta_review_packet_reversals"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id", name="uq_delta_review_packet_reversals_receipt"
        ),
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_delta_review_packet_reversals_key"
        ),
        ForeignKeyConstraint(
            ["project_id", "receipt_id"],
            [
                "delta_review_packet_receipts.project_id",
                "delta_review_packet_receipts.id",
            ],
            name="fk_delta_review_packet_reversals_receipt",
        ),
        CheckConstraint(
            "length(btrim(reversed_by_principal)) > 0",
            name="ck_delta_review_packet_reversals_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger)
    revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_record_revisions.id")
    )
    reversed_by_principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    reversed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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


# ADR-0091's configured members. The mandatory updated UCM is deliberately not
# among them: it is a column on ``IssueProfile``, so a profile can neither omit
# it nor carry it twice.
CONFIGURED_ARTIFACT_TYPES = (
    "accepted_change_summary",
    "chase_list",
    "weekly_coordination_report",
    "provenance_sidecar",
)

_CONFIGURED_ARTIFACT_TYPES_SQL = ", ".join(
    f"'{name}'" for name in CONFIGURED_ARTIFACT_TYPES
)


class IssueProfile(Base):
    """One version of what a project externally issues (#640, ADR-0091).

    ADR-0091 made the issued set per-project configuration with only the
    updated UCM mandatory, and recorded that the configuration was not
    modelled. This row is it, and every version of it is kept: a profile change
    never rewrites what an earlier reporting cutoff was configured to issue.

    The chain is the timing rule. Each version names its predecessor through a
    composite foreign key carrying that predecessor's project, identity,
    version and effective instant, and ``ck_project_issue_profiles_succession``
    requires the version to be exactly one higher and the effective instant to
    be strictly later — so monotonic, non-backdatable, single-lineage,
    append-only history is a database invariant rather than a Python
    comparison. ``uq_project_issue_profiles_successor`` refuses a fork and the
    partial unique index refuses a second lineage in one project.

    The updated UCM is a column rather than an artifact row because its
    participation is not configurable; ``IssueProfileArtifact`` holds exactly
    what ADR-0091 made a per-project choice.
    """

    __tablename__ = "project_issue_profiles"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_project_issue_profiles_key"
        ),
        UniqueConstraint(
            "project_id",
            "profile_identity",
            "profile_version",
            name="uq_project_issue_profiles_version",
        ),
        UniqueConstraint("id", "project_id", name="uq_project_issue_profiles_row"),
        UniqueConstraint(
            "id",
            "project_id",
            "profile_identity",
            "profile_version",
            "effective_from",
            name="uq_project_issue_profiles_chain",
        ),
        # The key a release candidate binds itself to (#529): the row, its
        # project, its identity and its version, so a candidate cannot name
        # one profile row while recording another profile's version.
        UniqueConstraint(
            "id",
            "project_id",
            "profile_identity",
            "profile_version",
            name="uq_project_issue_profiles_binding",
        ),
        UniqueConstraint(
            "supersedes_id", name="uq_project_issue_profiles_successor"
        ),
        ForeignKeyConstraint(
            [
                "supersedes_id",
                "project_id",
                "profile_identity",
                "supersedes_version",
                "supersedes_effective_from",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
                "project_issue_profiles.effective_from",
            ],
            name="fk_project_issue_profiles_supersedes",
        ),
        ForeignKeyConstraint(
            ["output_template_format_id", "project_id", "output_template_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_project_issue_profiles_template",
        ),
        ForeignKeyConstraint(
            ["field_mapping_format_id", "project_id", "field_mapping_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_project_issue_profiles_mapping",
        ),
        CheckConstraint(
            "length(btrim(profile_identity)) > 0",
            name="ck_project_issue_profiles_identity",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_project_issue_profiles_key",
        ),
        CheckConstraint(
            "length(btrim(registered_by_principal)) > 0",
            name="ck_project_issue_profiles_principal",
        ),
        CheckConstraint(
            "length(btrim(declaration_schema_version)) > 0",
            name="ck_project_issue_profiles_schema",
        ),
        CheckConstraint(
            "profile_version >= 1", name="ck_project_issue_profiles_version"
        ),
        CheckConstraint(
            "length(btrim(ucm_renderer_identity)) > 0 "
            "and length(btrim(ucm_renderer_version)) > 0",
            name="ck_project_issue_profiles_ucm",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(declaration, 'utf8')), 'hex') "
            "= content_sha256",
            name="ck_project_issue_profiles_digest",
        ),
        CheckConstraint(
            "(supersedes_id is null and profile_version = 1 "
            "and supersedes_version is null "
            "and supersedes_effective_from is null) "
            "or (supersedes_id is not null and supersedes_version is not null "
            "and supersedes_effective_from is not null "
            "and profile_version = supersedes_version + 1 "
            "and effective_from > supersedes_effective_from)",
            name="ck_project_issue_profiles_succession",
        ),
        Index(
            "uq_project_issue_profiles_root",
            "project_id",
            unique=True,
            postgresql_where=text("supersedes_id is null"),
        ),
        Index(
            "ix_project_issue_profiles_effective", "project_id", "effective_from"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    profile_identity: Mapped[str] = mapped_column(String(160))
    # An integer, so a blank version is not a value this column can hold.
    profile_version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    # The exact canonical bytes the digest is taken over, not a re-encoding of
    # them: `jsonb` would normalize key order, whitespace and numbers, and the
    # digest could no longer be checked against what came back.
    declaration: Mapped[str] = mapped_column(Text)
    declaration_schema_version: Mapped[str] = mapped_column(String(64))
    # The business instant the caller supplied, never a clock reading.
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ucm_renderer_identity: Mapped[str] = mapped_column(String(160))
    ucm_renderer_version: Mapped[str] = mapped_column(String(64))
    output_template_format_id: Mapped[int] = mapped_column(BigInteger)
    # A stored constant, so the composite key above can require the named
    # registration to be a template and not a mapping.
    output_template_kind: Mapped[str] = mapped_column(
        String(32), Computed("'output_template'", persisted=True)
    )
    field_mapping_format_id: Mapped[int] = mapped_column(BigInteger)
    field_mapping_kind: Mapped[str] = mapped_column(
        String(32), Computed("'field_mapping'", persisted=True)
    )
    supersedes_id: Mapped[int | None] = mapped_column(BigInteger)
    supersedes_version: Mapped[int | None] = mapped_column(Integer)
    supersedes_effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    registered_by_principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IssueProfileArtifact(Base):
    """One configured member of a project's issued set (#640, ADR-0091).

    Only what ADR-0091 made a per-project choice lives here. ``updated_ucm`` is
    not an admitted type: the mandatory member is ``IssueProfile``'s own
    column, so it can be neither dropped from a profile nor entered twice.
    """

    __tablename__ = "project_issue_profile_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "artifact_type",
            name="uq_project_issue_profile_artifacts_type",
        ),
        ForeignKeyConstraint(
            ["profile_id", "project_id"],
            ["project_issue_profiles.id", "project_issue_profiles.project_id"],
            name="fk_project_issue_profile_artifacts_profile",
        ),
        CheckConstraint(
            f"artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})",
            name="ck_project_issue_profile_artifacts_type",
        ),
        CheckConstraint(
            "length(btrim(renderer_identity)) > 0 "
            "and length(btrim(renderer_version)) > 0",
            name="ck_project_issue_profile_artifacts_renderer",
        ),
        Index("ix_project_issue_profile_artifacts_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    artifact_type: Mapped[str] = mapped_column(String(48))
    renderer_identity: Mapped[str] = mapped_column(String(160))
    renderer_version: Mapped[str] = mapped_column(String(64))


# --- #675 The confirmed coverage declaration one issue is prepared under ----

# The four states one coverage line may be in, spelled where the database
# check reads them. They are ``issue_rendering``'s own vocabulary, respelled
# nowhere: ``read`` is the only non-exception, and the other three are the
# honest ways a source is not part of what was read for this issue.
COVERAGE_LINE_STATES = ("read", "failed", "excluded", "late")


class IssueCoverageDeclaration(Base):
    """One coordinator's confirmation of the coverage reading Corridor derived.

    ADR-0086 makes "one declared coverage state" a shared input of every
    artifact in an issue, and #529 bound it as an in-memory value the caller
    supplied. That let any caller state a coverage nobody confirmed, so #675
    gives the declaration its own identity: the candidate now names this row
    by foreign key, and the only thing that can be prepared is coverage a
    person put their name to.

    **The machine's half and the human's half are separately digested.**
    ``derived_reading`` holds the exact canonical bytes Corridor derived from
    the effective issue profile, the persisted Source Delivery ledger, the
    processing receipts and the declared cutoff, and ``derived_reading_digest``
    is the SHA-256 of them; ``declaration`` repeats that digest and adds only
    what a person may add -- bounded annotations and permitted exclusions with
    their reasons -- and ``declaration_digest`` is the SHA-256 of *that*. A
    coordinator who confirmed one reading therefore cannot be recorded as
    having confirmed another, and the two questions "what did Corridor say"
    and "what did the person declare" keep two separate answers.

    **The boundary is an append-only watermark, never a clock comparison.**
    ``through_source_delivery_id`` is the highest ``source_deliveries`` row
    this issue includes, and every delivery after it is outside the issue by
    identity. ``cutoff_at`` is the human-readable instant the reading was taken
    at and is frozen beside it, but membership is the watermark: #641 recorded
    that a Proposed Delta has no trustworthy source-arrival instant and that
    ``created_at`` is server-assigned, and this is the record that lets the
    question be asked without one. ``None`` is the honest watermark of a
    project that has taken no delivery at all, and is not "everything".
    """

    __tablename__ = "issue_coverage_declarations"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_issue_coverage_declarations_row"
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_issue_coverage_declarations_key",
        ),
        # One row per confirmed declaration. A repeated confirmation of the
        # same reading with the same annotations converges here rather than
        # appending a second identical declaration.
        UniqueConstraint(
            "project_id",
            "declaration_digest",
            name="uq_issue_coverage_declarations_identity",
        ),
        ForeignKeyConstraint(
            [
                "issue_profile_id",
                "project_id",
                "issue_profile_identity",
                "issue_profile_version",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
            ],
            name="fk_issue_coverage_declarations_profile",
        ),
        ForeignKeyConstraint(
            ["through_source_delivery_id", "project_id"],
            ["source_deliveries.id", "source_deliveries.project_id"],
            name="fk_issue_coverage_declarations_delivery",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(derived_reading, 'utf8')), 'hex') "
            "= derived_reading_digest",
            name="ck_issue_coverage_declarations_reading_digest",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(declaration, 'utf8')), 'hex') "
            "= declaration_digest",
            name="ck_issue_coverage_declarations_declaration_digest",
        ),
        CheckConstraint(
            "length(btrim(coverage_identity)) > 0",
            name="ck_issue_coverage_declarations_identity_text",
        ),
        CheckConstraint(
            "length(btrim(confirmed_by_principal)) > 0",
            name="ck_issue_coverage_declarations_principal",
        ),
        CheckConstraint(
            "issue_profile_version >= 1",
            name="ck_issue_coverage_declarations_version",
        ),
        Index(
            "ix_issue_coverage_declarations_project", "project_id", "cutoff_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    issue_profile_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_identity: Mapped[str] = mapped_column(String(160))
    issue_profile_version: Mapped[int] = mapped_column(Integer)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    through_source_delivery_id: Mapped[int | None] = mapped_column(BigInteger)
    derived_reading: Mapped[str] = mapped_column(Text)
    derived_reading_digest: Mapped[str] = mapped_column(String(64))
    declaration: Mapped[str] = mapped_column(Text)
    declaration_digest: Mapped[str] = mapped_column(String(64))
    coverage_identity: Mapped[str] = mapped_column(String(160))
    confirmed_by_principal: Mapped[str] = mapped_column(String(128))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- #529 One immutable release candidate, from one coherent reading --------

# Every reason preparation may refuse for, as a closed vocabulary. A refusal a
# person has to read a paragraph to classify is a refusal nobody counts.
PREPARATION_REFUSAL_REASONS = (
    "unsupported_issue_configuration",
    "renderer_failed",
    "artifact_missing",
    "storage_failed",
    "digest_mismatch",
    "inputs_changed_while_rendering",
    "mixed_reading",
    "candidate_identity_conflict",
)

_PREPARATION_REFUSAL_REASONS_SQL = ", ".join(
    f"'{reason}'" for reason in PREPARATION_REFUSAL_REASONS
)

# The three outcomes one finished preparation attempt may record (#675).
# There is deliberately no ``running`` or ``queued`` member: an attempt row is
# appended when the attempt *finishes*, so the relation stays append-only and
# the immutability trigger covers it whole. "Preparing" is the derived answer
# for a request no attempt has finished yet, which is exactly what the Issue
# section says while a worker is at it.
PREPARATION_ATTEMPT_OUTCOMES = ("prepared", "refused", "failed")

_PREPARATION_ATTEMPT_OUTCOMES_SQL = ", ".join(
    f"'{outcome}'" for outcome in PREPARATION_ATTEMPT_OUTCOMES
)

# ADR-0086's three derived outcomes for a candidate that exists.
READY = "ready"
READY_WITH_EXCEPTIONS = "ready_with_exceptions"
BLOCKED = "blocked"
RELEASE_READINESS_STATES = (READY, READY_WITH_EXCEPTIONS, BLOCKED)

_RELEASE_READINESS_SQL = ", ".join(
    f"'{state}'" for state in RELEASE_READINESS_STATES
)


class ReleasePackage(Base):
    """One authorized external issue, and the receipt that binds it (#533).

    #529 created this relation empty so a candidate could name a predecessor;
    #533 is what writes it, and #635 is why it carries so many columns. The
    receipt binds the candidate, the accepted revision, the previous authorized
    package or an explicit none, the source cutoff, the coverage identity and
    digest, the issue-profile identity and version, the template and mapping
    registrations, every artifact identity and digest, the releaser and the
    release time (ADR-0086).

    **The candidate binding is composite.** ``(candidate_id, project_id,
    accepted_revision_id)`` references ``release_candidates (id, project_id,
    accepted_revision_id)``, so the receipt states the revision it released
    explicitly *and* proves it is the revision the candidate was prepared from.
    A receipt whose revision disagrees with its candidate's is not a row this
    schema can hold — which is precisely what ``external_report_releases``
    could not say, and why that table is never a predecessor.

    **The predecessor is a chain, never a clock.** ``sequence_number`` is the
    predecessor's own number plus one, bound to the predecessor row by a
    composite key. One root per project and one successor per package: a first
    release has no predecessor and invents none, and a later release has
    exactly one. Nothing orders releases by ``authorized_at``.

    **The identity is the digest of the receipt bytes.** ``package_identity``
    is the SHA-256 of ``receipt_declaration``, checked by the database, so a
    receipt that does not digest to what it claims cannot be stored.
    """

    __tablename__ = "release_packages"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_release_packages_row"),
        UniqueConstraint(
            "project_id", "package_identity", name="uq_release_packages_identity"
        ),
        # One receipt per candidate: a candidate cannot be attached to two
        # divergent releases, and a replay converges on the row that exists.
        UniqueConstraint("candidate_id", name="uq_release_packages_candidate"),
        UniqueConstraint(
            "id", "project_id", "sequence_number", name="uq_release_packages_sequence"
        ),
        # One successor per package, so the history is a chain and not a fork.
        UniqueConstraint(
            "previous_package_id", name="uq_release_packages_successor"
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_release_packages_revision",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "project_id", "accepted_revision_id"],
            [
                "release_candidates.id",
                "release_candidates.project_id",
                "release_candidates.accepted_revision_id",
            ],
            name="fk_release_packages_candidate",
            # The candidate names its predecessor package and the package names
            # its candidate, so the two relations reference each other. The
            # migration creates them in order and this cycle exists only in the
            # metadata graph; `use_alter` tells SQLAlchemy which edge to ignore
            # when it sorts tables, so table-ordered readers (the committed
            # scenario cleanup, the architecture ratchets) keep a usable order
            # instead of silently dropping every foreign key between the two.
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["previous_package_id", "project_id", "previous_sequence_number"],
            [
                "release_packages.id",
                "release_packages.project_id",
                "release_packages.sequence_number",
            ],
            name="fk_release_packages_previous",
        ),
        ForeignKeyConstraint(
            [
                "issue_profile_id",
                "project_id",
                "issue_profile_identity",
                "issue_profile_version",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
            ],
            name="fk_release_packages_profile",
        ),
        ForeignKeyConstraint(
            ["output_template_format_id", "project_id", "output_template_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_release_packages_template",
        ),
        ForeignKeyConstraint(
            ["field_mapping_format_id", "project_id", "field_mapping_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_release_packages_mapping",
        ),
        CheckConstraint(
            "length(btrim(package_identity)) > 0",
            name="ck_release_packages_identity",
        ),
        CheckConstraint(
            "length(btrim(authorized_by_principal)) > 0",
            name="ck_release_packages_principal",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(receipt_declaration, 'utf8')), 'hex') "
            "= package_identity",
            name="ck_release_packages_receipt_digest",
        ),
        CheckConstraint(
            "length(btrim(receipt_schema_version)) > 0",
            name="ck_release_packages_receipt_schema",
        ),
        CheckConstraint(
            "length(btrim(coverage_identity)) > 0",
            name="ck_release_packages_coverage",
        ),
        CheckConstraint(
            "length(btrim(ucm_renderer_identity)) > 0 "
            "and length(btrim(ucm_renderer_version)) > 0 "
            "and length(btrim(ucm_storage_key)) > 0 "
            "and ucm_byte_count > 0",
            name="ck_release_packages_ucm",
        ),
        CheckConstraint(
            "issue_profile_version >= 1",
            name="ck_release_packages_profile_version",
        ),
        CheckConstraint(
            "(previous_package_id is null and sequence_number = 1 "
            "and previous_sequence_number is null) "
            "or (previous_package_id is not null "
            "and previous_sequence_number is not null "
            "and sequence_number = previous_sequence_number + 1)",
            name="ck_release_packages_succession",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    package_identity: Mapped[str] = mapped_column(String(160))
    accepted_revision_id: Mapped[int] = mapped_column(BigInteger)
    authorized_by_principal: Mapped[str] = mapped_column(String(128))
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    candidate_id: Mapped[int] = mapped_column(BigInteger)
    candidate_identity: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    previous_package_id: Mapped[int | None] = mapped_column(BigInteger)
    previous_sequence_number: Mapped[int | None] = mapped_column(Integer)
    sequence_number: Mapped[int] = mapped_column(Integer)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage_identity: Mapped[str] = mapped_column(String(160))
    coverage_sha256: Mapped[str] = mapped_column(String(64))
    issue_profile_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_identity: Mapped[str] = mapped_column(String(160))
    issue_profile_version: Mapped[int] = mapped_column(Integer)
    issue_profile_sha256: Mapped[str] = mapped_column(String(64))
    output_template_format_id: Mapped[int] = mapped_column(BigInteger)
    output_template_kind: Mapped[str] = mapped_column(
        String(32), Computed("'output_template'", persisted=True)
    )
    field_mapping_format_id: Mapped[int] = mapped_column(BigInteger)
    field_mapping_kind: Mapped[str] = mapped_column(
        String(32), Computed("'field_mapping'", persisted=True)
    )
    ucm_renderer_identity: Mapped[str] = mapped_column(String(160))
    ucm_renderer_version: Mapped[str] = mapped_column(String(64))
    ucm_content_sha256: Mapped[str] = mapped_column(String(64))
    ucm_storage_key: Mapped[str] = mapped_column(String(160))
    ucm_byte_count: Mapped[int] = mapped_column(BigInteger)
    receipt_declaration: Mapped[str] = mapped_column(Text)
    receipt_schema_version: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleasePackageArtifact(Base):
    """One configured member of an authorized package's artifact set (#533).

    Copied from the candidate's own rows by the authorization command, never
    supplied by a caller, so the receipt's enumeration cannot disagree with the
    candidate it seals. ``updated_ucm`` is not an admitted type here for #529's
    reason: the mandatory member is ``ReleasePackage``'s own columns, so a
    package with no UCM and a package with two are both unrepresentable.
    """

    __tablename__ = "release_package_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "package_id", "artifact_type", name="uq_release_package_artifacts_type"
        ),
        UniqueConstraint(
            "package_id", "position", name="uq_release_package_artifacts_position"
        ),
        ForeignKeyConstraint(
            ["package_id", "project_id"],
            ["release_packages.id", "release_packages.project_id"],
            name="fk_release_package_artifacts_package",
        ),
        CheckConstraint(
            f"artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})",
            name="ck_release_package_artifacts_type",
        ),
        CheckConstraint(
            "length(btrim(renderer_identity)) > 0 "
            "and length(btrim(renderer_version)) > 0",
            name="ck_release_package_artifacts_renderer",
        ),
        CheckConstraint(
            "length(btrim(storage_key)) > 0 and byte_count > 0",
            name="ck_release_package_artifacts_bytes",
        ),
        CheckConstraint(
            "position >= 1", name="ck_release_package_artifacts_position"
        ),
        Index("ix_release_package_artifacts_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    package_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    artifact_type: Mapped[str] = mapped_column(String(48))
    renderer_identity: Mapped[str] = mapped_column(String(160))
    renderer_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(160))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer)


class ReleaseCandidate(Base):
    """One prepared, immutable issue for review (#529, ADR-0086, ADR-0091).

    ``candidate_identity`` is the SHA-256 of ``input_declaration``, which holds
    every bound input: the project, the accepted revision, the previous
    authorized package or an explicit none, the source cutoff, the coverage
    identity and digest, the issue-profile row, identity, version and digest,
    the template and mapping registrations and digests, the configured artifact
    types, the renderer identities and versions, the product and code revision,
    and the enabled feature flags. ``content_sha256`` digests a declaration
    that repeats all of that and adds the ordered artifact identities and their
    own digests, so a candidate identity answers "were these the same inputs"
    and a content digest answers "is this the same issue".

    The updated UCM is a set of columns rather than an artifact row, carrying
    #640's structural decision forward: a candidate with no UCM and a candidate
    with two are both unrepresentable.
    """

    __tablename__ = "release_candidates"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_release_candidates_row"),
        UniqueConstraint(
            "project_id",
            "candidate_identity",
            name="uq_release_candidates_identity",
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_release_candidates_revision",
        ),
        ForeignKeyConstraint(
            ["previous_package_id", "project_id"],
            ["release_packages.id", "release_packages.project_id"],
            name="fk_release_candidates_previous",
        ),
        # The confirmed declaration this candidate was prepared under (#675),
        # by identity rather than by the coverage identity and digest alone:
        # those two say what the coverage was, and this says whose confirmation
        # of which derived reading authorised preparing under it.
        ForeignKeyConstraint(
            ["coverage_declaration_id", "project_id"],
            [
                "issue_coverage_declarations.id",
                "issue_coverage_declarations.project_id",
            ],
            name="fk_release_candidates_coverage",
        ),
        ForeignKeyConstraint(
            [
                "issue_profile_id",
                "project_id",
                "issue_profile_identity",
                "issue_profile_version",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
            ],
            name="fk_release_candidates_profile",
        ),
        ForeignKeyConstraint(
            ["output_template_format_id", "project_id", "output_template_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_release_candidates_template",
        ),
        ForeignKeyConstraint(
            ["field_mapping_format_id", "project_id", "field_mapping_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_release_candidates_mapping",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(input_declaration, 'utf8')), 'hex') "
            "= candidate_identity",
            name="ck_release_candidates_identity_digest",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(content_declaration, 'utf8')), 'hex') "
            "= content_sha256",
            name="ck_release_candidates_content_digest",
        ),
        CheckConstraint(
            "length(btrim(input_schema_version)) > 0",
            name="ck_release_candidates_schema",
        ),
        CheckConstraint(
            "length(btrim(coverage_identity)) > 0",
            name="ck_release_candidates_coverage",
        ),
        CheckConstraint(
            "length(btrim(ucm_renderer_identity)) > 0 "
            "and length(btrim(ucm_renderer_version)) > 0 "
            "and length(btrim(ucm_storage_key)) > 0 "
            "and ucm_byte_count > 0",
            name="ck_release_candidates_ucm",
        ),
        CheckConstraint(
            f"readiness in ({_RELEASE_READINESS_SQL})",
            name="ck_release_candidates_readiness",
        ),
        CheckConstraint(
            "length(btrim(prepared_by_principal)) > 0",
            name="ck_release_candidates_principal",
        ),
        CheckConstraint(
            "issue_profile_version >= 1",
            name="ck_release_candidates_profile_version",
        ),
        Index(
            "ix_release_candidates_project_prepared", "project_id", "prepared_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    candidate_identity: Mapped[str] = mapped_column(String(64))
    input_declaration: Mapped[str] = mapped_column(Text)
    input_schema_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    content_declaration: Mapped[str] = mapped_column(Text)
    accepted_revision_id: Mapped[int] = mapped_column(BigInteger)
    previous_package_id: Mapped[int | None] = mapped_column(BigInteger)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage_declaration_id: Mapped[int] = mapped_column(BigInteger)
    coverage_identity: Mapped[str] = mapped_column(String(160))
    coverage_sha256: Mapped[str] = mapped_column(String(64))
    issue_profile_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_identity: Mapped[str] = mapped_column(String(160))
    issue_profile_version: Mapped[int] = mapped_column(Integer)
    issue_profile_sha256: Mapped[str] = mapped_column(String(64))
    output_template_format_id: Mapped[int] = mapped_column(BigInteger)
    output_template_kind: Mapped[str] = mapped_column(
        String(32), Computed("'output_template'", persisted=True)
    )
    field_mapping_format_id: Mapped[int] = mapped_column(BigInteger)
    field_mapping_kind: Mapped[str] = mapped_column(
        String(32), Computed("'field_mapping'", persisted=True)
    )
    code_revision: Mapped[str] = mapped_column(String(160))
    product_revision: Mapped[str] = mapped_column(String(64))
    ucm_renderer_identity: Mapped[str] = mapped_column(String(160))
    ucm_renderer_version: Mapped[str] = mapped_column(String(64))
    ucm_content_sha256: Mapped[str] = mapped_column(String(64))
    ucm_storage_key: Mapped[str] = mapped_column(String(160))
    ucm_byte_count: Mapped[int] = mapped_column(BigInteger)
    readiness: Mapped[str] = mapped_column(String(32))
    prepared_by_principal: Mapped[str] = mapped_column(String(128))
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleaseCandidateArtifact(Base):
    """One configured member of a prepared candidate's artifact set (#529).

    Only what ADR-0091 made a per-project choice lives here. ``updated_ucm`` is
    not an admitted type: the mandatory member is ``ReleaseCandidate``'s own
    columns, so it can be neither dropped from a candidate nor entered twice.
    The composite foreign key carries ``project_id``, so an artifact of one
    project cannot be attached to another project's candidate.
    """

    __tablename__ = "release_candidate_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "artifact_type",
            name="uq_release_candidate_artifacts_type",
        ),
        UniqueConstraint(
            "candidate_id",
            "position",
            name="uq_release_candidate_artifacts_position",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "project_id"],
            ["release_candidates.id", "release_candidates.project_id"],
            name="fk_release_candidate_artifacts_candidate",
        ),
        CheckConstraint(
            f"artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})",
            name="ck_release_candidate_artifacts_type",
        ),
        CheckConstraint(
            "length(btrim(renderer_identity)) > 0 "
            "and length(btrim(renderer_version)) > 0",
            name="ck_release_candidate_artifacts_renderer",
        ),
        CheckConstraint(
            "length(btrim(storage_key)) > 0 and byte_count > 0",
            name="ck_release_candidate_artifacts_bytes",
        ),
        CheckConstraint(
            "position >= 1", name="ck_release_candidate_artifacts_position"
        ),
        Index("ix_release_candidate_artifacts_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    artifact_type: Mapped[str] = mapped_column(String(48))
    renderer_identity: Mapped[str] = mapped_column(String(160))
    renderer_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(160))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer)


class ReleasePreparationRefusal(Base):
    """The receipt a refused preparation leaves, and the only thing it leaves.

    A failed preparation writes no candidate and no artifact row, so this is
    where the reason lives. ``reason_code`` is one of a closed vocabulary and
    ``reason`` is a sentence a person reads, never a captured traceback.
    """

    __tablename__ = "release_preparation_refusals"
    __table_args__ = (
        CheckConstraint(
            f"reason_code in ({_PREPARATION_REFUSAL_REASONS_SQL})",
            name="ck_release_preparation_refusals_code",
        ),
        CheckConstraint(
            "length(btrim(reason)) > 0 and length(reason) <= 2000",
            name="ck_release_preparation_refusals_reason",
        ),
        CheckConstraint(
            "length(btrim(refused_by_principal)) > 0",
            name="ck_release_preparation_refusals_principal",
        ),
        Index(
            "ix_release_preparation_refusals_project", "project_id", "refused_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    candidate_identity: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(48))
    reason: Mapped[str] = mapped_column(Text)
    refused_by_principal: Mapped[str] = mapped_column(String(128))
    refused_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )




# --- #675 What one preparation was asked for, and what each attempt did -----

class ReleasePreparationRequest(Base):
    """One idempotent request that a worker prepare this project's next issue.

    #536 promised Review -> Follow-up -> Issue as one executable path and had
    no way to make a candidate, because #529 renders outside its own short
    transactions and needs a session *factory*: running it inside the HTTP
    request would be long, fragile, and ambiguous to retry. This row is what
    the request leaves behind instead. It records what the coordinator was
    looking at when they asked -- the accepted revision, the issue profile
    version, the source cutoff, and the confirmed coverage declaration -- so
    the worker prepares the issue that was confirmed rather than whatever the
    project happens to hold when it gets round to it.

    There is deliberately no status column and no mutable "weekly close"
    object. Status is derived from this relation and
    ``release_preparation_attempts``, which is the same discipline ADR-0085
    used to refuse a stored packet lifecycle and #537 used to refuse a stored
    cross-project queue: a second authority beside the append-only records
    would be stale the moment one of them moved, and somebody would have to
    tick it.
    """

    __tablename__ = "release_preparation_requests"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_release_preparation_requests_row"
        ),
        # The idempotency the ticket asks for: a coordinator who submits twice,
        # or a retried POST, converges on the request already recorded rather
        # than queueing a second preparation of the same issue.
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_release_preparation_requests_key",
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_release_preparation_requests_revision",
        ),
        ForeignKeyConstraint(
            [
                "issue_profile_id",
                "project_id",
                "issue_profile_identity",
                "issue_profile_version",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
            ],
            name="fk_release_preparation_requests_profile",
        ),
        ForeignKeyConstraint(
            ["coverage_declaration_id", "project_id"],
            [
                "issue_coverage_declarations.id",
                "issue_coverage_declarations.project_id",
            ],
            name="fk_release_preparation_requests_coverage",
        ),
        CheckConstraint(
            "length(btrim(requested_by_principal)) > 0",
            name="ck_release_preparation_requests_principal",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_release_preparation_requests_key",
        ),
        CheckConstraint(
            "issue_profile_version >= 1",
            name="ck_release_preparation_requests_version",
        ),
        Index(
            "ix_release_preparation_requests_project", "project_id", "id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    accepted_revision_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_identity: Mapped[str] = mapped_column(String(160))
    issue_profile_version: Mapped[int] = mapped_column(Integer)
    coverage_declaration_id: Mapped[int] = mapped_column(BigInteger)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_by_principal: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleasePreparationAttempt(Base):
    """What one finished attempt at one preparation request produced (#675).

    Exactly one of the three outcomes, and the check constraint makes the other
    two unrepresentable in the same row: a ``prepared`` attempt names its
    candidate and no reason, and a ``refused`` or ``failed`` attempt names a
    bounded reason and no candidate. That is #529's own three-outcome rule
    carried into the record a coordinator's screen is derived from, so
    "preparation left a partial candidate" is not a state this relation can
    describe.

    ``started_at`` and ``finished_at`` are both declared by the worker and both
    recorded on the one row, which is why a row is appended when an attempt
    finishes rather than claimed when it begins. A relation that were claimed
    first and completed later would need an UPDATE, and every release relation
    beside it refuses one.
    """

    __tablename__ = "release_preparation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_release_preparation_attempts_row"
        ),
        ForeignKeyConstraint(
            ["request_id", "project_id"],
            [
                "release_preparation_requests.id",
                "release_preparation_requests.project_id",
            ],
            name="fk_release_preparation_attempts_request",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "project_id"],
            ["release_candidates.id", "release_candidates.project_id"],
            name="fk_release_preparation_attempts_candidate",
        ),
        CheckConstraint(
            f"outcome in ({_PREPARATION_ATTEMPT_OUTCOMES_SQL})",
            name="ck_release_preparation_attempts_outcome",
        ),
        CheckConstraint(
            "(outcome = 'prepared' and candidate_id is not null "
            "and refusal_code is null and reason is null) "
            "or (outcome = 'refused' and candidate_id is null "
            "and refusal_code is not null and reason is not null) "
            "or (outcome = 'failed' and candidate_id is null "
            "and refusal_code is null and reason is not null)",
            name="ck_release_preparation_attempts_result",
        ),
        CheckConstraint(
            f"refusal_code is null or refusal_code in "
            f"({_PREPARATION_REFUSAL_REASONS_SQL})",
            name="ck_release_preparation_attempts_code",
        ),
        CheckConstraint(
            "reason is null or (length(btrim(reason)) > 0 "
            "and length(reason) <= 2000)",
            name="ck_release_preparation_attempts_reason",
        ),
        CheckConstraint(
            "finished_at >= started_at",
            name="ck_release_preparation_attempts_span",
        ),
        Index(
            "ix_release_preparation_attempts_request", "request_id", "id"
        ),
        Index(
            "ix_release_preparation_attempts_project", "project_id", "id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(String(32))
    candidate_id: Mapped[int | None] = mapped_column(BigInteger)
    refusal_code: Mapped[str | None] = mapped_column(String(48))
    reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleasePreparationReading(Base):
    """The one report-preparation reading one request is prepared under (#690).

    Not "the latest completed receipt for this project", and the difference is
    the whole point. The weekly pass keeps completing receipts, so a request
    that resolved the newest one whenever a worker got round to it would
    prepare a different window from the one the coordinator confirmed, and a
    preparation that failed could advance the next reading's floor. The
    binding is written once, is unique per request, and every retry of that
    request reuses it.

    The window is a watermark pair and never a pair of timestamps (#488). The
    **ceilings** are frozen here at the moment of binding; the **floors** come
    from the reading bound to the previous *authorized* package, or zero for a
    first issue, because ADR-0086 is explicit that only an authorized package
    advances the external comparison baseline. A candidate that was merely
    prepared, blocked or refused moves nothing.

    The result itself stays owned by ``DueWorkReceipt.handler_result_json``.
    This row says *which* retained reading was used and what its bytes
    digested to; copying the reading here would copy state another row already
    owns, which is the defect #598's ratchet refuses.
    """

    __tablename__ = "release_preparation_readings"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_release_preparation_readings_row"
        ),
        UniqueConstraint(
            "request_id", name="uq_release_preparation_readings_request"
        ),
        ForeignKeyConstraint(
            ["request_id", "project_id"],
            [
                "release_preparation_requests.id",
                "release_preparation_requests.project_id",
            ],
            name="fk_release_preparation_readings_request",
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_release_preparation_readings_revision",
        ),
        ForeignKeyConstraint(
            ["previous_package_id", "project_id"],
            ["release_packages.id", "release_packages.project_id"],
            name="fk_release_preparation_readings_previous",
        ),
        CheckConstraint(
            "result_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_release_preparation_readings_digest",
        ),
        CheckConstraint(
            "handler_key = 'report_preparation'",
            name="ck_release_preparation_readings_handler",
        ),
        CheckConstraint(
            "length(btrim(result_schema_version)) > 0",
            name="ck_release_preparation_readings_schema",
        ),
        CheckConstraint(
            "prior_delta_floor >= 0 and prior_disposition_floor >= 0",
            name="ck_release_preparation_readings_floors",
        ),
        CheckConstraint(
            "through_delta_id >= prior_delta_floor "
            "and through_disposition_id >= prior_disposition_floor",
            name="ck_release_preparation_readings_window",
        ),
        Index(
            "ix_release_preparation_readings_project", "project_id", "id"
        ),
        Index("ix_release_preparation_readings_receipt", "receipt_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    request_id: Mapped[int] = mapped_column(BigInteger)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("due_work_receipts.id"))
    handler_key: Mapped[str] = mapped_column(String(64))
    result_schema_version: Mapped[str] = mapped_column(String(64))
    result_sha256: Mapped[str] = mapped_column(String(64))
    accepted_revision_id: Mapped[int] = mapped_column(BigInteger)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_package_id: Mapped[int | None] = mapped_column(BigInteger)
    prior_delta_floor: Mapped[int] = mapped_column(BigInteger)
    prior_disposition_floor: Mapped[int] = mapped_column(BigInteger)
    through_delta_id: Mapped[int] = mapped_column(BigInteger)
    through_disposition_id: Mapped[int] = mapped_column(BigInteger)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleasePreparationPublication(Base):
    """One Due Work occurrence, one preparation request (#690).

    Occurrences are otherwise coalesced from a cadence slot, and a slot cannot
    say which request it is for. One opaque occurrence processing every pending
    request would also give them one shared lease, one shared retry budget and
    one shared receipt, so a single unlucky request would burn the lot. Unique
    both ways: a reclaimed occurrence resumes the request it was published for
    and no other.
    """

    __tablename__ = "release_preparation_publications"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_release_preparation_publications_row"
        ),
        UniqueConstraint(
            "request_id", name="uq_release_preparation_publications_request"
        ),
        UniqueConstraint(
            "occurrence_id",
            name="uq_release_preparation_publications_occurrence",
        ),
        ForeignKeyConstraint(
            ["request_id", "project_id"],
            [
                "release_preparation_requests.id",
                "release_preparation_requests.project_id",
            ],
            name="fk_release_preparation_publications_request",
        ),
        Index(
            "ix_release_preparation_publications_project", "project_id", "id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    request_id: Mapped[int] = mapped_column(BigInteger)
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_occurrences.id")
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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


class PipelineQualificationPolicy(Base):
    """Native metric contracts and rules frozen before their observations."""

    __tablename__ = "pipeline_qualification_policies"
    policy_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_sha256: Mapped[str] = mapped_column(String(64))
    policy_text: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class PipelineConfiguration(Base):
    """One immutable full-chain configuration; registration selects nothing."""

    __tablename__ = "pipeline_configurations"
    __table_args__ = (CheckConstraint(
        "configuration_sha256 = encode(sha256(convert_to(configuration_text, 'UTF8')), 'hex')",
        name="ck_pipeline_configurations_digest",
    ),)
    configuration_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    configuration_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineReceiptMixin:
    """Permanent exact bytes, separately indexed by their scope/configuration."""

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    configuration_sha256: Mapped[str] = mapped_column(ForeignKey("pipeline_configurations.configuration_sha256"))
    scope_sha256: Mapped[str] = mapped_column(String(64))
    receipt_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    receipt_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineObservation(PipelineReceiptMixin, Base):
    """A complete, refused or failed shadow attempt, never an active run declaration."""

    __tablename__ = "pipeline_observations"
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    extraction_run_id: Mapped[int | None] = mapped_column(ForeignKey("extraction_runs.id"))


class PipelineComparison(PipelineReceiptMixin, Base):
    """Repeatability and quality are distinct immutable observations."""

    __tablename__ = "pipeline_comparisons"
    kind: Mapped[str] = mapped_column(String(24))


class PipelineQualification(PipelineReceiptMixin, Base):
    """A numeric, chain-bound gate result; incomplete evidence remains incomplete."""

    __tablename__ = "pipeline_qualifications"
    status: Mapped[str] = mapped_column(String(24))


class PipelineAcceptance(PipelineReceiptMixin, Base):
    """ADR-0095's recorded maintainer acceptance, a selection basis of its own.

    It is never a gate result and carries no status: an incomplete or failed
    qualification stays exactly that in its own receipt. Only the maintainer's
    own principal may append here, and no runtime login holds an insert grant.
    """

    __tablename__ = "pipeline_acceptances"
    implementation_revision: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(Text)


class PipelineSelection(PipelineReceiptMixin, Base):
    """One maintainer's append-only routing selection, with a CAS predecessor.

    Its basis is exactly one of a passing qualification or a recorded
    acceptance (ADR-0095); the two never read alike. This relation does not
    declare an Active Extraction Run, reconcile an old cohort or write accepted
    values. Restoring an older configuration appends another selection; its
    original observations remain intact.
    """

    __tablename__ = "pipeline_selections"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "deployment", "previous_selection_id",
            name="uq_pipeline_selections_successor", postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "(qualification_id is null) <> (acceptance_id is null)",
            name="ck_pipeline_selections_one_basis",
        ),
    )
    deployment: Mapped[str] = mapped_column(Text)
    qualification_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_qualifications.id"))
    acceptance_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_acceptances.id"))
    previous_selection_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_selections.id"))
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean)


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


# The lifecycle of one reference observed at a connected location (#350). A
# reference is `proposed` when discovery has only observed it; a human moves it
# to `authorized` by declaring the kind the bytes cannot state (ADR-0007); the
# fetch pass moves it to `registered` once its exact bytes become a Document.
# Discovery is never registration authority, so `proposed` is not actionable
# and no guessed filename, type, or date is ever an accepted fact.
DISCOVERED_REFERENCE_STATES = ("proposed", "authorized", "registered")

# Every fetch attempt against an authorized reference retains its own honest
# outcome (#350). `registered` created a new Document; `unchanged` re-observed
# identical bytes and created nothing; `drift` saw different bytes under an
# existing registry identity and is retained as operations work rather than an
# overwrite or an inferred Supersession (ADR-0015); `failed` is a rejected
# response, boundary, or member that never became a source document; and
# `budget_exhausted` stopped a bounded pass with resumable or held state.
SOURCE_FETCH_OUTCOMES = (
    "registered",
    "unchanged",
    "drift",
    "failed",
    "budget_exhausted",
)


class DiscoveredReference(Base):
    """One coalesced reference observed at a connected location (#350).

    Discovery of a new document reference or newly published archive URL records
    exactly one row per reference identity, keyed by where it was observed rather
    than by any value read from it. Repeated discovery of the same reference
    updates ``last_observed_at`` and ``observed_count`` in place — one proposed
    intake identity, its origin and available source metadata preserved — never a
    second row. The observed title and type hint are the location's own words and
    stay advisory: they are never promoted to an accepted document fact, and a
    reference becomes a Document only through an attributable authorization plus a
    validated fetch. Members of an already-declared dated snapshot are recognized
    here (their archive URL was observed before) so a refetch is not miscounted as
    a discovery.
    """

    __tablename__ = "discovered_references"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "reference_key", name="uq_discovered_reference_identity"
        ),
        CheckConstraint(
            "observed_count >= 1", name="ck_discovered_reference_observed_count"
        ),
        CheckConstraint(
            "authorized_doc_type is null or authorized_doc_type in "
            "('matrix','minutes','agreement','email','plan','schedule','spec',"
            "'status_report','other')",
            name="ck_discovered_reference_authorized_doc_type",
        ),
        CheckConstraint(
            "(state = 'proposed' and authorized_at is null "
            "and registered_document_id is null) or "
            "(state = 'authorized' and authorized_at is not null "
            "and authorized_doc_type is not null) or "
            "(state = 'registered' and authorized_at is not null "
            "and registered_document_id is not null)",
            name="ck_discovered_reference_state",
        ),
        Index("ix_discovered_references_project_state", "project_id", "state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    # The server-owned location identity this reference was observed at. Scope is
    # bound to one declared location; a reference never floats free of one.
    location_id: Mapped[str] = mapped_column(String(128))
    # A deterministic identity over (location, url, member) — where the reference
    # was observed, not what it contains. Two runs of discovery coalesce onto it.
    reference_key: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(Text)
    # Set when the reference is a member of an archive; ``archive_url`` is the
    # dated snapshot and ``member`` names the file inside it.
    archive_url: Mapped[str | None] = mapped_column(Text)
    member: Mapped[str | None] = mapped_column(Text)
    # The location's own advisory words. Never an accepted fact (ADR-0007).
    observed_title: Mapped[str | None] = mapped_column(Text)
    observed_type_hint: Mapped[str | None] = mapped_column(String(64))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_count: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    state: Mapped[str] = mapped_column(
        _enum(*DISCOVERED_REFERENCE_STATES, name="discovered_reference_state"),
        default="proposed",
        server_default="proposed",
    )
    # Declared by a human at authorization — the kind the bytes cannot state — and
    # an optional stable registry identity for a curated corpus document.
    authorized_doc_type: Mapped[str | None] = mapped_column(String(32))
    authorized_registry_id: Mapped[str | None] = mapped_column(String(128))
    authorized_by: Mapped[str | None] = mapped_column(String(128))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_document_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceFetchAttempt(Base):
    """One retained fetch attempt against an authorized reference (#350).

    The append-only counterpart to the corpus lockfile's per-source record, held
    in the database so a crash between fetching bytes and registering a Document
    cannot advertise partial content as a completed retrieval: a row is written in
    the same committed transaction that registers (or refuses) the bytes. A failed
    attempt is retained beside any prior success, so a source that returns an error
    page, a missing member, a truncated body, or an unauthorized redirect shows the
    new attempt failed rather than silently overwriting the last good retrieval.
    """

    __tablename__ = "source_fetch_attempts"
    __table_args__ = (
        CheckConstraint(
            "sha256 is null or sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_fetch_attempt_sha256",
        ),
        CheckConstraint(
            "prior_sha256 is null or prior_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_fetch_attempt_prior_sha256",
        ),
        Index(
            "ix_source_fetch_attempts_project_outcome", "project_id", "outcome"
        ),
        Index(
            "ix_source_fetch_attempts_reference", "project_id", "reference_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    # The reference this attempt was for. Null only for a pass-level budget
    # exhaustion that stopped before a specific reference was chosen.
    reference_key: Mapped[str | None] = mapped_column(String(64))
    location_id: Mapped[str] = mapped_column(String(128))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(
        _enum(*SOURCE_FETCH_OUTCOMES, name="source_fetch_outcome")
    )
    # The exact URL whose bytes were read, after any authorized redirect — the
    # retrieval provenance a citation ultimately bottoms out at.
    resolved_url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(Text)
    byte_count: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    # For a drift outcome, the sha256 of the document already registered under the
    # same registry identity; both byte identities are retained on disk.
    prior_sha256: Mapped[str | None] = mapped_column(String(64))
    # The registered or existing Document this attempt resolved to, when any.
    document_id: Mapped[int | None] = mapped_column(BigInteger)
    # A stable machine reason plus a human sentence for a failed, drift, or
    # budget outcome; null for an ordinary success.
    reason: Mapped[str | None] = mapped_column(Text)
    # Whether a stopped pass may simply run again to finish the remaining work.
    resumable: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
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


class DueWorkSchedule(Base):
    """One validated gate-7 declaration for a server-owned handler."""

    __tablename__ = "due_work_schedules"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "handler_key",
            "configuration_version",
            "input_identity_sha256",
            name="uq_due_work_schedule_identity",
        ),
        CheckConstraint(
            "configuration_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_schedule_configuration_sha256",
        ),
        CheckConstraint(
            "input_identity_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_schedule_input_identity_sha256",
        ),
        CheckConstraint(
            "retention_days > 0 and max_attempts > 0 and backoff_seconds >= 0 "
            "and claim_ttl_seconds > 0 and deadline_seconds > 0 "
            "and concurrency_limit > 0 and model_token_budget >= 0 "
            "and notification_budget >= 0",
            name="ck_due_work_schedule_budgets",
        ),
        CheckConstraint(
            "disabled_at is null or disabled_at >= enabled_at",
            name="ck_due_work_schedule_disable_order",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    handler_key: Mapped[str] = mapped_column(String(64))
    configuration_version: Mapped[str] = mapped_column(String(64))
    scope_json: Mapped[dict] = mapped_column(JSONB)
    configuration_json: Mapped[dict] = mapped_column(JSONB)
    configuration_sha256: Mapped[str] = mapped_column(String(64))
    input_identity_sha256: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cadence: Mapped[str] = mapped_column(String(32))
    timezone_name: Mapped[str] = mapped_column(String(64))
    missed_run_policy: Mapped[str] = mapped_column(String(32))
    retention_days: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    backoff_seconds: Mapped[int] = mapped_column(Integer)
    claim_ttl_seconds: Mapped[int] = mapped_column(Integer)
    deadline_seconds: Mapped[int] = mapped_column(Integer)
    concurrency_limit: Mapped[int] = mapped_column(Integer)
    model_token_budget: Mapped[int] = mapped_column(Integer)
    notification_budget: Mapped[int] = mapped_column(Integer)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DueWorkOccurrence(Base):
    """Mutable claim state for one stable scheduled occurrence."""

    __tablename__ = "due_work_occurrences"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_due_work_occurrence_key"),
        CheckConstraint(
            "state in ('pending', 'claimed', 'retry_due', 'completed', 'failed')",
            name="ck_due_work_occurrence_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_due_work_occurrence_attempt_count",
        ),
        CheckConstraint(
            "(state = 'claimed' and owner is not null and claim_token is not null "
            "and lease_expires_at is not null and claimed_at is not null "
            "and deadline_at is not null) or "
            "(state <> 'claimed' and owner is null and claim_token is null "
            "and lease_expires_at is null and claimed_at is null "
            "and deadline_at is null)",
            name="ck_due_work_occurrence_claim_shape",
        ),
        CheckConstraint(
            "(state = 'retry_due' and next_attempt_at is not null) or "
            "(state <> 'retry_due' and next_attempt_at is null)",
            name="ck_due_work_occurrence_retry_shape",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    scheduled_job_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_schedules.id"), index=True
    )
    occurrence_key: Mapped[str] = mapped_column(String(64))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(16))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DueWorkReceipt(Base):
    """Append-only result of one bounded due-work attempt."""

    __tablename__ = "due_work_receipts"
    __table_args__ = (
        UniqueConstraint(
            "occurrence_id", "attempt_number", name="uq_due_work_receipt_attempt"
        ),
        CheckConstraint(
            "execution_outcome in ('completed', 'retry_due', 'failed')",
            name="ck_due_work_receipt_outcome",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_receipt_content_sha256",
        ),
        CheckConstraint(
            "attempt_number > 0 and finished_at >= started_at",
            name="ck_due_work_receipt_attempt",
        ),
        CheckConstraint(
            "(execution_outcome = 'completed' and handler_result_json is not null "
            "and error_code is null) or "
            "(execution_outcome in ('retry_due', 'failed') "
            "and handler_result_json is null and error_code is not null)",
            name="ck_due_work_receipt_result_shape",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_occurrences.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    handler_key: Mapped[str] = mapped_column(String(64))
    attempt_number: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[str] = mapped_column(String(64), unique=True)
    runtime_owner: Mapped[str] = mapped_column(String(128))
    execution_outcome: Mapped[str] = mapped_column(String(16))
    handler_result_json: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    safe_next_step: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RecordInclusionRequest(Base):
    """One durable, coalescing watermark of a project's pending Record Inclusion.

    A completed Extraction Run (and, later, an approved identity/fact change)
    leaves the record needing reconciliation, but ``load_project`` appends a
    PolicyRun on every call — so calling it on every idle tick would grow the
    receipt log without bound. This row is the handoff that makes reconciliation
    conditional and recoverable: a producer bumps ``dirty_seq`` inside its own
    transaction, so a rolled-back producer leaves no work and a committed one
    survives process exit. Reconciliation is pending exactly while
    ``dirty_seq > reconciled_seq``; many bumps between reconciliations coalesce
    into one pending pass. Unlike the append-only receipt tables, this is
    mutable operational state (like a due-work occurrence), so it carries no
    immutability trigger.
    """

    __tablename__ = "record_inclusion_requests"
    __table_args__ = (
        CheckConstraint(
            "dirty_seq >= 0 and reconciled_seq >= 0",
            name="ck_record_inclusion_requests_non_negative",
        ),
        CheckConstraint(
            "reconciled_seq <= dirty_seq",
            name="ck_record_inclusion_requests_watermark_order",
        ),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), primary_key=True
    )
    dirty_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reconciled_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    last_reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RevisionReconciliationRequest(Base):
    """One durable, coalescing watermark of a project's pending revision work.

    A committed structural change — a registered Supersession edge or a changed
    Current Production Run — makes a project's Document Revision Processing and
    Automatic Support Update possibly stale, but re-running that pass on every
    idle scheduled tick would create a Revision Comparison nobody asked for and
    append a Carry-Forward PolicyRun without bound. This row is the handoff that
    makes the pass conditional and recoverable, exactly like
    ``record_inclusion_requests``: a producer bumps ``dirty_seq`` inside its own
    transaction, so a rolled-back producer leaves no revision work and a
    committed one survives process exit. Reconciliation is pending exactly while
    ``dirty_seq > reconciled_seq``; many bumps between reconciliations coalesce
    into one pending pass. Like the Record Inclusion watermark this is mutable
    operational state, not an append-only receipt, so it carries no immutability
    trigger.
    """

    __tablename__ = "revision_reconciliation_requests"
    __table_args__ = (
        CheckConstraint(
            "dirty_seq >= 0 and reconciled_seq >= 0",
            name="ck_revision_reconciliation_requests_non_negative",
        ),
        CheckConstraint(
            "reconciled_seq <= dirty_seq",
            name="ck_revision_reconciliation_requests_watermark_order",
        ),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), primary_key=True
    )
    dirty_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reconciled_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    last_reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class ProjectContactImport(Base):
    """One immutable, bound contact import and complete row accounting (#562)."""

    __tablename__ = "project_contact_imports"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_contact_import_scope"),
        UniqueConstraint("project_id", "idempotency_key", name="uq_contact_import_key"),
        UniqueConstraint("project_id", "source_family", "source_revision", name="uq_contact_import_revision"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey("source_deliveries.id"))
    customer: Mapped[str] = mapped_column(Text)
    source_family: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    mapping_json: Mapped[dict] = mapped_column(JSONB)
    accounting_json: Mapped[dict] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class ProjectContact(Base):
    """An imported contact or attributable replacement, never a mutable address book."""

    __tablename__ = "project_contacts"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "import_id"], ["project_contact_imports.project_id", "project_contact_imports.id"]),
        UniqueConstraint("project_id", "id", name="uq_project_contact_scope"),
        ForeignKeyConstraint(["project_id", "corrects_id"], ["project_contacts.project_id", "project_contacts.id"]),
        UniqueConstraint("corrects_id", name="uq_project_contact_correction"),
        UniqueConstraint("project_id", "correction_key", name="uq_project_contact_correction_key"),
        Index("uq_project_contact_import_identity", "import_id", "source_contact_id", unique=True,
              postgresql_where=text("corrects_id is null")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    import_id: Mapped[int] = mapped_column(BigInteger)
    source_contact_id: Mapped[str] = mapped_column(Text)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("external_orgs.id"))
    values_json: Mapped[dict] = mapped_column(JSONB)
    source_locators: Mapped[dict] = mapped_column(JSONB)
    unresolved_reason: Mapped[str | None] = mapped_column(Text)
    corrects_id: Mapped[int | None] = mapped_column(BigInteger)
    correction_key: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[str | None] = mapped_column(Text)
    correction_reason: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


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


class OrganizationIdentityReceipt(Base):
    """One append-only resolution of source wording to a registered party.

    The ``external_orgs`` row remains the registry's current projection.  This
    receipt is the authority and provenance for an alias, a human selection, or
    a deterministic whole-row resolution: it preserves the wording, the
    evidence that was considered, and the responsible person or policy.  A
    local statement resolution is deliberately not represented here; it stays
    source-bound on the statement receipt rather than acquiring registry reach.
    """

    __tablename__ = "organization_identity_receipts"
    __table_args__ = (
        UniqueConstraint("candidate_id", "method", name="uq_organization_identity_receipt_candidate_method"),
        CheckConstraint(
            "method in ('human_confirmation', 'automatic_name_alias', "
            "'automatic_facility_class', 'automatic_contact', "
            "'automatic_revision_lineage', 'automatic_stated_alias', "
            "'human_cited_alias_confirmation', 'alias_correction')",
            name="ck_organization_identity_receipt_method",
        ),
        CheckConstraint(
            "scope = 'registry'", name="ck_organization_identity_receipt_scope"
        ),
        CheckConstraint(
            "length(trim(stated_wording)) > 0",
            name="ck_organization_identity_receipt_wording",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_organization_identity_receipt_actor",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_json) = 'object'",
            name="ck_organization_identity_receipt_evidence",
        ),
        CheckConstraint(
            "jsonb_typeof(facility_classes_json) = 'array'",
            name="ck_organization_identity_receipt_facility_classes",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    external_org_id: Mapped[int] = mapped_column(ForeignKey("external_orgs.id"), index=True)
    method: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32), default="registry", server_default="registry")
    stated_wording: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[dict] = mapped_column(JSONB)
    facility_classes_json: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    recorded_by: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[str | None] = mapped_column(String(64))
    policy_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OrganizationIdentityActivation(Base):
    """Append-only ADR-0050 gate for whole-row automatic identity tiers."""

    __tablename__ = "organization_identity_activations"
    __table_args__ = (
        CheckConstraint(
            "action in ('activate', 'suspend')",
            name="ck_organization_identity_activation_action",
        ),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_organization_identity_activation_sha256",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_organization_identity_activation_reason",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_organization_identity_activation_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    replay_case_count: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(160))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReportRun(Base):
    """A published report, kept so the next one can say what changed.

    `ruleset_version` is stored per run for a specific reason: a figure that
    moved between two weekly reports must be attributable to a *rule* change
    or a *data* change, and those call for opposite responses. Tightening
    STALE from 14 days to 10 looks identical to a project falling behind
    unless the report remembers which ruleset produced each number.

    `revision_id` is the accepted Project Record revision the reading was
    taken against (#602). It is the authority for every value the *record*
    owns. `snapshot_json` beside it is the immutable Report Reading payload:
    what this dated occurrence published, which is a different ownership and
    not a copy of the revision (ADR-0092).
    """

    __tablename__ = "report_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_report_runs_revision",
        ),
        Index("ix_report_runs_revision_id", "revision_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # The accepted revision this reading was taken against.  Nullable only
    # because rows written before #602 are not rewritten, and because a
    # project with no accepted revision at all has no identity to name; a
    # trigger refuses a new row that omits one when the project has any.
    revision_id: Mapped[int | None] = mapped_column(BigInteger)
    retirement_archive_id: Mapped[int | None] = mapped_column(ForeignKey("legacy_ledger_archives.id"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ruleset_version: Mapped[str] = mapped_column(String(32))
    # The immutable Report Reading payload (ADR-0092): the population this
    # report covered, its derived documentation-requirement results and
    # Constraint Alerts, the statement-projected Promised For, and the rules
    # and thresholds that produced them.  None of those is a revision's to
    # answer, so this is the occurrence's own evidence rather than a cache of
    # `revision_id` above; it is retained as long as the run is and expires on
    # no cache TTL.  `report_reading` owns its schema version, content digest
    # and the translation that keeps a version 1 payload readable.
    snapshot_json: Mapped[dict] = mapped_column(JSONB)
    output_path: Mapped[str | None] = mapped_column(Text)
    # Document-only reports are a separate comparison lineage: comparing one
    # against the ordinary report would leak a verbal date through its old
    # snapshot into an otherwise citation-only surface.
    document_only: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )


class ProjectCheckConfiguration(Base):
    """One declared, retained per-project configuration of the check thresholds.

    The exception engine's thresholds — how many days of document silence is
    STALE, how near a Need Date is DUE_SOON, how near a Next Action is
    ACTION_DUE_SOON — were fixed module constants, varied only by a test
    passing a ``Thresholds`` into ``evaluate*``.  A project that runs on a
    different cadence had no supported way to declare its own horizons.

    Each save is a new identity, never an edit: the effective configuration
    is the newest row for the project, and every earlier row — with the
    person who declared it and when — stays readable so a report published
    under it remains explainable.  No row means the supported module
    defaults, unchanged.  The values only parameterize the existing rules;
    this table introduces no new rule, urgency, or model behavior.  The
    append-only guarantee is enforced by a trigger, matching the other
    provenance tables (ADR-0044 keeps the reading derived — nothing here
    rewrites a past Evaluation).
    """

    __tablename__ = "project_check_configurations"
    __table_args__ = (
        CheckConstraint(
            "stale_days between 1 and 3650",
            name="ck_project_check_configurations_stale_days",
        ),
        CheckConstraint(
            "due_soon_days between 1 and 3650",
            name="ck_project_check_configurations_due_soon_days",
        ),
        CheckConstraint(
            "action_due_soon_days between 1 and 3650",
            name="ck_project_check_configurations_action_due_soon_days",
        ),
        CheckConstraint(
            "length(trim(ruleset_version)) > 0",
            name="ck_project_check_configurations_ruleset_version",
        ),
        CheckConstraint(
            "length(trim(created_by)) > 0",
            name="ck_project_check_configurations_created_by",
        ),
        Index("ix_project_check_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # The ruleset the declared thresholds parameterize, recorded so a later
    # reader never reads these day-counts against a different rule meaning.
    ruleset_version: Mapped[str] = mapped_column(String(32))
    stale_days: Mapped[int] = mapped_column(Integer)
    due_soon_days: Mapped[int] = mapped_column(Integer)
    action_due_soon_days: Mapped[int] = mapped_column(Integer)
    # The stable human subject who declared it, from the deployment identity
    # seam — never a form-supplied author (M8; production auth is #331).
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CoordinationSummaryConfiguration(Base):
    """One explicit, server-owned authorization for bounded summary drafting.

    Unlike ordinary report reading, a Coordination Summary can spend model
    budget.  Therefore no supported default exists: an attributable project
    declaration names every input, model, time, retry, retention, and
    observation bound before a request is allowed.  Rows are append-only so a
    retained draft always names the rules under which it was obtained.
    """

    __tablename__ = "coordination_summary_configurations"
    __table_args__ = (
        CheckConstraint("source_scope in ('all_sources', 'documents_only')", name="ck_summary_config_source_scope"),
        CheckConstraint("max_input_tokens between 1 and 200000", name="ck_summary_config_input_budget"),
        CheckConstraint("max_output_tokens between 1 and 20000", name="ck_summary_config_output_budget"),
        CheckConstraint("timeout_seconds between 1 and 600", name="ck_summary_config_timeout"),
        CheckConstraint("max_requests = 1", name="ck_summary_config_one_request"),
        CheckConstraint("retry_policy = 'none'", name="ck_summary_config_no_retry"),
        CheckConstraint("retention_policy = 'class_b_30_days'", name="ck_summary_config_retention"),
        CheckConstraint("length(trim(model)) > 0", name="ck_summary_config_model"),
        CheckConstraint("length(trim(prompt_version)) > 0", name="ck_summary_config_prompt"),
        CheckConstraint("length(trim(observation_context)) > 0", name="ck_summary_config_context"),
        CheckConstraint("length(trim(created_by)) > 0", name="ck_summary_config_actor"),
        Index("ix_coordination_summary_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    source_scope: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    max_input_tokens: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    max_requests: Mapped[int] = mapped_column(Integer)
    retry_policy: Mapped[str] = mapped_column(String(32))
    retention_policy: Mapped[str] = mapped_column(String(64))
    observation_context: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CoordinationSummaryRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative draft attempt."""

    __tablename__ = "coordination_summary_requests"
    __table_args__ = (
        UniqueConstraint("configuration_id", "reading_sha256", name="uq_summary_request_reading"),
        CheckConstraint(
            "status in ('completed', 'empty_input', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused')",
            name="ck_summary_request_status",
        ),
        CheckConstraint("reading_sha256 ~ '^[0-9a-f]{64}$'", name="ck_summary_request_reading_sha"),
        CheckConstraint("length(trim(requested_by)) > 0", name="ck_summary_request_actor"),
        Index("ix_coordination_summary_requests_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    configuration_id: Mapped[int] = mapped_column(ForeignKey("coordination_summary_configurations.id"))
    requested_by: Mapped[str] = mapped_column(String(128))
    reading_sha256: Mapped[str] = mapped_column(String(64))
    project_reading_json: Mapped[dict] = mapped_column(JSONB)
    evaluated_on: Mapped[date] = mapped_column(Date)
    ruleset_version: Mapped[str] = mapped_column(String(32))
    statement_publication_fingerprint: Mapped[str] = mapped_column(String(64))
    provenance_mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    summary_markdown: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductionRunExplanationConfiguration(Base):
    """One explicit, server-owned authorization for a bounded run explanation.

    Explaining competing Current Production Runs can spend model budget, so it
    has no supported default: an attributable technical-operations declaration
    names the model, prompt, and the time, input, output, retry, retention, and
    observation bounds before any explanation request is allowed. Rows are
    append-only so a retained explanation always names the rules it ran under.
    """

    __tablename__ = "production_run_explanation_configurations"
    __table_args__ = (
        CheckConstraint(
            "max_input_tokens between 1 and 200000",
            name="ck_run_explanation_config_input_budget",
        ),
        CheckConstraint(
            "max_output_tokens between 1 and 20000",
            name="ck_run_explanation_config_output_budget",
        ),
        CheckConstraint(
            "timeout_seconds between 1 and 600",
            name="ck_run_explanation_config_timeout",
        ),
        CheckConstraint("max_requests = 1", name="ck_run_explanation_config_one_request"),
        CheckConstraint("retry_policy = 'none'", name="ck_run_explanation_config_no_retry"),
        CheckConstraint(
            "retention_policy = 'class_b_30_days'",
            name="ck_run_explanation_config_retention",
        ),
        CheckConstraint("length(trim(model)) > 0", name="ck_run_explanation_config_model"),
        CheckConstraint(
            "length(trim(prompt_version)) > 0", name="ck_run_explanation_config_prompt"
        ),
        CheckConstraint(
            "length(trim(observation_context)) > 0",
            name="ck_run_explanation_config_context",
        ),
        CheckConstraint(
            "length(trim(created_by)) > 0", name="ck_run_explanation_config_actor"
        ),
        Index("ix_run_explanation_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    max_input_tokens: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    max_requests: Mapped[int] = mapped_column(Integer)
    retry_policy: Mapped[str] = mapped_column(String(32))
    retention_policy: Mapped[str] = mapped_column(String(64))
    observation_context: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProductionRunExplanationRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative run explanation.

    The receipt binds the exact competing run identities it explained, retains
    the frozen immutable snapshots it read (the context a human checks each
    explanation against), and stores only the validated explanation plus a
    redacted execution lineage — never raw source text, a chain of thought, or
    any claim of human decision authorship. It never declares a run.
    """

    __tablename__ = "production_run_explanation_requests"
    __table_args__ = (
        UniqueConstraint(
            "configuration_id",
            "comparison_sha256",
            name="uq_run_explanation_request_comparison",
        ),
        CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_run_explanation_request_status",
        ),
        CheckConstraint(
            "comparison_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_run_explanation_request_comparison_sha",
        ),
        CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_run_explanation_request_state_token",
        ),
        CheckConstraint(
            "length(trim(requested_by)) > 0", name="ck_run_explanation_request_actor"
        ),
        CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_run_explanation_request_adapter"
        ),
        CheckConstraint("non_authoritative", name="ck_run_explanation_request_non_auth"),
        Index("ix_run_explanation_requests_project_id", "project_id"),
        Index("ix_run_explanation_requests_document_id", "document_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("production_run_explanation_configurations.id")
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    comparison_sha256: Mapped[str] = mapped_column(String(64))
    state_token: Mapped[str] = mapped_column(String(64))
    competing_run_ids_json: Mapped[list] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # The frozen immutable run snapshots this explanation read: the checkable
    # context, not a raw source-wide trace.
    comparison_json: Mapped[dict] = mapped_column(JSONB)
    # The validated, non-authoritative explanation, or null when none was kept.
    explanation_json: Mapped[dict | None] = mapped_column(JSONB)
    # Redacted transport metadata (hashes, usage, timing) — no prompt, response,
    # or chain-of-thought text.
    execution_lineage_json: Mapped[dict | None] = mapped_column(JSONB)
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
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


class ExtractionFailureDiagnosisConfiguration(Base):
    """One explicit, server-owned authorization for a bounded failure diagnosis.

    Diagnosing an unreadable, no-matrix, quarantined, or otherwise failed
    Extraction Run can spend model budget, so it has no supported default: an
    attributable technical-operations declaration names the model, prompt, and
    the time, input, output, retry, retention, and observation bounds before any
    diagnosis request is allowed. Rows are append-only so a retained diagnosis
    always names the rules it ran under (ADR-0011, ADR-0034).
    """

    __tablename__ = "extraction_failure_diagnosis_configurations"
    __table_args__ = (
        CheckConstraint(
            "max_input_tokens between 1 and 200000",
            name="ck_failure_diagnosis_config_input_budget",
        ),
        CheckConstraint(
            "max_output_tokens between 1 and 20000",
            name="ck_failure_diagnosis_config_output_budget",
        ),
        CheckConstraint(
            "timeout_seconds between 1 and 600",
            name="ck_failure_diagnosis_config_timeout",
        ),
        CheckConstraint(
            "max_requests = 1", name="ck_failure_diagnosis_config_one_request"
        ),
        CheckConstraint(
            "retry_policy = 'none'", name="ck_failure_diagnosis_config_no_retry"
        ),
        CheckConstraint(
            "retention_policy = 'class_b_30_days'",
            name="ck_failure_diagnosis_config_retention",
        ),
        CheckConstraint(
            "length(trim(model)) > 0", name="ck_failure_diagnosis_config_model"
        ),
        CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_failure_diagnosis_config_prompt",
        ),
        CheckConstraint(
            "length(trim(observation_context)) > 0",
            name="ck_failure_diagnosis_config_context",
        ),
        CheckConstraint(
            "length(trim(created_by)) > 0", name="ck_failure_diagnosis_config_actor"
        ),
        Index("ix_failure_diagnosis_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    max_input_tokens: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    max_requests: Mapped[int] = mapped_column(Integer)
    retry_policy: Mapped[str] = mapped_column(String(32))
    retention_policy: Mapped[str] = mapped_column(String(64))
    observation_context: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExtractionFailureDiagnosisRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative failure diagnosis.

    The receipt binds the exact failed Extraction Run it diagnosed, retains the
    frozen deterministic failure facts and permitted-page metadata it read (the
    context a human checks the diagnosis against), and stores only the validated
    diagnosis plus a redacted execution lineage — never raw source-wide text, a
    chain of thought, or any claim of human decision authorship. It never retries
    extraction, relabels the failure, or removes a quarantine; the original
    Extraction Run outcome, error, and receipt are untouched (ADR-0011).
    """

    __tablename__ = "extraction_failure_diagnosis_requests"
    __table_args__ = (
        UniqueConstraint(
            "configuration_id",
            "input_sha256",
            name="uq_failure_diagnosis_request_input",
        ),
        CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_failure_diagnosis_request_status",
        ),
        CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_failure_diagnosis_request_input_sha",
        ),
        CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_failure_diagnosis_request_state_token",
        ),
        CheckConstraint(
            "length(trim(requested_by)) > 0",
            name="ck_failure_diagnosis_request_actor",
        ),
        CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_failure_diagnosis_request_adapter"
        ),
        CheckConstraint(
            "non_authoritative", name="ck_failure_diagnosis_request_non_auth"
        ),
        Index("ix_failure_diagnosis_requests_project_id", "project_id"),
        Index("ix_failure_diagnosis_requests_document_id", "document_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    extraction_run_id: Mapped[int] = mapped_column(ForeignKey("extraction_runs.id"))
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_failure_diagnosis_configurations.id")
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    input_sha256: Mapped[str] = mapped_column(String(64))
    state_token: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # The frozen deterministic failure facts and permitted-page metadata this
    # diagnosis read: the checkable context, not a raw source-wide trace.
    source_context_json: Mapped[dict] = mapped_column(JSONB)
    # The validated, non-authoritative diagnosis, or null when none was kept.
    diagnosis_json: Mapped[dict | None] = mapped_column(JSONB)
    # Redacted transport metadata (hashes, usage, timing) — no prompt, response,
    # or chain-of-thought text.
    execution_lineage_json: Mapped[dict | None] = mapped_column(JSONB)
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
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


class RevisionChangeExplanationConfiguration(Base):
    """One explicit, server-owned authorization for a bounded revision-change
    explanation (#360).

    Explaining a verified newer-document change can spend model budget, so it
    has no supported default: an attributable technical-operations declaration
    names the model, prompt, and the time, input, output, retry, retention, and
    observation bounds before any explanation request is allowed. Rows are
    append-only so a retained explanation always names the rules it ran under.
    """

    __tablename__ = "revision_change_explanation_configurations"
    __table_args__ = (
        CheckConstraint(
            "max_input_tokens between 1 and 200000",
            name="ck_rev_change_expl_cfg_input_budget",
        ),
        CheckConstraint(
            "max_output_tokens between 1 and 20000",
            name="ck_rev_change_expl_cfg_output_budget",
        ),
        CheckConstraint(
            "timeout_seconds between 1 and 600",
            name="ck_rev_change_expl_cfg_timeout",
        ),
        CheckConstraint("max_requests = 1", name="ck_rev_change_expl_cfg_one_request"),
        CheckConstraint("retry_policy = 'none'", name="ck_rev_change_expl_cfg_no_retry"),
        CheckConstraint(
            "retention_policy = 'class_b_30_days'",
            name="ck_rev_change_expl_cfg_retention",
        ),
        CheckConstraint("length(trim(model)) > 0", name="ck_rev_change_expl_cfg_model"),
        CheckConstraint(
            "length(trim(prompt_version)) > 0", name="ck_rev_change_expl_cfg_prompt"
        ),
        CheckConstraint(
            "length(trim(observation_context)) > 0",
            name="ck_rev_change_expl_cfg_context",
        ),
        CheckConstraint(
            "length(trim(created_by)) > 0", name="ck_rev_change_expl_cfg_actor"
        ),
        Index("ix_rev_change_expl_cfg_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    max_input_tokens: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    max_requests: Mapped[int] = mapped_column(Integer)
    retry_policy: Mapped[str] = mapped_column(String(32))
    retention_policy: Mapped[str] = mapped_column(String(64))
    observation_context: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RevisionChangeExplanationRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative revision-change
    explanation (#360).

    The receipt binds the exact affected Constraint and the integrity-verified
    Revision Comparison finding it explained, retains the frozen sanitized
    comparison it read (the context a coordinator checks the explanation
    against), and stores only the validated explanation plus a redacted
    execution lineage — never raw source text, a chain of thought, or any claim
    of human decision authorship. It never updates support or settles anything.
    """

    __tablename__ = "revision_change_explanation_requests"
    __table_args__ = (
        UniqueConstraint(
            "configuration_id",
            "comparison_sha256",
            name="uq_rev_change_expl_req_comparison",
        ),
        CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_rev_change_expl_req_status",
        ),
        CheckConstraint(
            "comparison_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rev_change_expl_req_comparison_sha",
        ),
        CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_rev_change_expl_req_state_token",
        ),
        CheckConstraint(
            "length(trim(requested_by)) > 0", name="ck_rev_change_expl_req_actor"
        ),
        CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_rev_change_expl_req_adapter"
        ),
        CheckConstraint("non_authoritative", name="ck_rev_change_expl_req_non_auth"),
        Index("ix_rev_change_expl_req_project_id", "project_id"),
        Index("ix_rev_change_expl_req_dependency_id", "dependency_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    comparison_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    finding_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_findings.id")
    )
    finding_state: Mapped[str] = mapped_column(String(32))
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("revision_change_explanation_configurations.id")
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    comparison_sha256: Mapped[str] = mapped_column(String(64))
    state_token: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # The frozen, sanitized comparison finding this explanation read: the
    # checkable context, not a raw source-wide trace.
    comparison_json: Mapped[dict] = mapped_column(JSONB)
    # The validated, non-authoritative explanation, or null when none was kept.
    explanation_json: Mapped[dict | None] = mapped_column(JSONB)
    # Redacted transport metadata (hashes, usage, timing) — no prompt, response,
    # or chain-of-thought text.
    execution_lineage_json: Mapped[dict | None] = mapped_column(JSONB)
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
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


class SourceIntakeDraftConfiguration(Base):
    """One explicit, server-owned authorization for a bounded intake draft (#362).

    Drafting source-bound intake metadata and replacement proposals can spend
    model budget, so it has no supported default: an attributable coordination
    declaration names the model, prompt, and the time, input, output, retry,
    retention, and observation bounds before any draft request is allowed. Rows
    are append-only so a retained draft always names the rules it ran under.
    """

    __tablename__ = "source_intake_draft_configurations"
    __table_args__ = (
        CheckConstraint(
            "max_input_tokens between 1 and 200000",
            name="ck_intake_draft_config_input_budget",
        ),
        CheckConstraint(
            "max_output_tokens between 1 and 20000",
            name="ck_intake_draft_config_output_budget",
        ),
        CheckConstraint(
            "timeout_seconds between 1 and 600",
            name="ck_intake_draft_config_timeout",
        ),
        CheckConstraint("max_requests = 1", name="ck_intake_draft_config_one_request"),
        CheckConstraint("retry_policy = 'none'", name="ck_intake_draft_config_no_retry"),
        CheckConstraint(
            "retention_policy = 'class_b_30_days'",
            name="ck_intake_draft_config_retention",
        ),
        CheckConstraint("length(trim(model)) > 0", name="ck_intake_draft_config_model"),
        CheckConstraint(
            "length(trim(prompt_version)) > 0", name="ck_intake_draft_config_prompt"
        ),
        CheckConstraint(
            "length(trim(observation_context)) > 0",
            name="ck_intake_draft_config_context",
        ),
        CheckConstraint(
            "length(trim(created_by)) > 0", name="ck_intake_draft_config_actor"
        ),
        Index("ix_intake_draft_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    max_input_tokens: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    max_requests: Mapped[int] = mapped_column(Integer)
    retry_policy: Mapped[str] = mapped_column(String(32))
    retention_policy: Mapped[str] = mapped_column(String(64))
    observation_context: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceIntakeDraftRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative intake draft (#362).

    The receipt binds the exact staged bytes it read (by their own hash, never a
    registered Document id — a draft identity stays distinct from a registered
    one), retains the frozen readable surface it was checked against (the
    permitted page text and the registry identities a curator verifies each
    suggestion against), and stores only the validated proposals plus a redacted
    execution lineage — never raw source-wide text, a chain of thought, or any
    claim of human decision authorship. It registers no Document and declares no
    Supersession.
    """

    __tablename__ = "source_intake_draft_requests"
    __table_args__ = (
        UniqueConstraint(
            "configuration_id",
            "source_sha256",
            name="uq_intake_draft_request_source",
        ),
        CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_intake_draft_request_status",
        ),
        CheckConstraint(
            "staged_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_staged_sha",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_source_sha",
        ),
        CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_state_token",
        ),
        CheckConstraint(
            "length(trim(requested_by)) > 0", name="ck_intake_draft_request_actor"
        ),
        CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_intake_draft_request_adapter"
        ),
        CheckConstraint("non_authoritative", name="ck_intake_draft_request_non_auth"),
        Index("ix_intake_draft_requests_project_id", "project_id"),
        Index("ix_intake_draft_requests_staged_sha256", "staged_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # The staged byte identity this draft read — the draft's own identity, kept
    # deliberately separate from any registered Document.
    staged_sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(Text)
    declared_doc_type: Mapped[str] = mapped_column(String(64))
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("source_intake_draft_configurations.id")
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    source_sha256: Mapped[str] = mapped_column(String(64))
    state_token: Mapped[str] = mapped_column(String(64))
    permitted_pages_json: Mapped[list] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # The frozen readable surface this draft read: permitted page text and the
    # registry identities, the context a human checks each proposal against.
    source_json: Mapped[dict] = mapped_column(JSONB)
    # The validated, non-authoritative proposals, or null when none were kept.
    proposals_json: Mapped[dict | None] = mapped_column(JSONB)
    # Redacted transport metadata (hashes, usage, timing) — no prompt, response,
    # or chain-of-thought text.
    execution_lineage_json: Mapped[dict | None] = mapped_column(JSONB)
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
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


class ProcessingArtifact(Base):
    """One classified file-backed intermediary with a digest remainder."""

    __tablename__ = "processing_artifacts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    retention_class: Mapped[str] = mapped_column(String(16), server_default="class_b")
    storage_path: Mapped[str] = mapped_column(Text, unique=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    terminal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionHold(Base):
    """One attributable project hold that suspends every Class B delete path."""

    __tablename__ = "retention_holds"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    placed_by: Mapped[str] = mapped_column(String(128))
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lifted_by: Mapped[str | None] = mapped_column(String(128))
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionReference(Base):
    """A durable or open reference that makes intermediary content unreachable."""

    __tablename__ = "retention_references"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(String(64))
    source_row_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    referenced_by: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionManifest(Base):
    """Immutable dry-run identity for one exact set of eligible Class B values."""

    __tablename__ = "retention_manifests"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionManifestItem(Base):
    """One digest-pinned intermediary value named before deletion."""

    __tablename__ = "retention_manifest_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    manifest_id: Mapped[int] = mapped_column(
        ForeignKey("retention_manifests.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(String(64))
    source_row_id: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))
    terminal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExternalReportArtifact(Base):
    """One immutable, already-rendered External Report PDF.

    Rendering is deliberately separate from human release.  This table owns
    the exact PDF and frozen Report context a project person can later choose;
    a new release receipt references this immutable owner so retention never
    depends on an artifact URL, path, or regenerating ReportRun.  Legacy
    receipts may still retain their historical copies.
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
    """One immutable authorization of one fixed External Report artifact.

    A working ``ReportRun`` lets the next internal report describe change;
    it is deliberately not an artifact authority.  This receipt instead
    references the immutable artifact that owns the PDF and frozen context.
    Legacy receipts may still own their copied bytes and context directly;
    the read properties below preserve that history without copying new
    releases (ADR-0040, ADR-0072).
    """

    __tablename__ = "external_report_releases"
    __table_args__ = (
        CheckConstraint("format = 'pdf'", name="ck_external_report_releases_pdf_only"),
        CheckConstraint(
            "pdf_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_external_report_releases_pdf_sha256",
        ),
        CheckConstraint(
            "pdf_bytes is null or octet_length(pdf_bytes) > 5",
            name="ck_external_report_releases_nonempty_pdf",
        ),
        CheckConstraint(
            "provenance_mode in ('all-supported-sources', 'document-only')",
            name="ck_external_report_releases_provenance_mode",
        ),
        CheckConstraint(
            "record_context_json is null or jsonb_typeof(record_context_json) = 'object'",
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
        CheckConstraint(
            "(content_storage = 'legacy' and pdf_bytes is not null "
            "and record_context_json is not null) or "
            "(content_storage = 'artifact' and artifact_id is not null "
            "and pdf_bytes is null and evaluation_context_json is null "
            "and record_context_json is null)",
            name="ck_external_report_releases_content_owner",
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
    content_storage: Mapped[str] = mapped_column(
        String(16), server_default="artifact"
    )
    _legacy_pdf_bytes: Mapped[bytes | None] = mapped_column("pdf_bytes", LargeBinary)
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_on: Mapped[date] = mapped_column(Date)
    ruleset_version: Mapped[str] = mapped_column(String(64))
    _legacy_evaluation_context_json: Mapped[dict | None] = mapped_column(
        "evaluation_context_json", JSONB
    )
    provenance_mode: Mapped[str] = mapped_column(String(32))
    _legacy_record_context_json: Mapped[dict | None] = mapped_column(
        "record_context_json", JSONB
    )
    released_by: Mapped[str] = mapped_column(String(128))
    released_by_display: Mapped[str | None] = mapped_column(Text)
    released_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    _artifact: Mapped[ExternalReportArtifact | None] = relationship(lazy="joined")

    @property
    def pdf_bytes(self) -> bytes:
        """Read new artifact bytes or the retained bytes of a legacy release."""
        if self._legacy_pdf_bytes is not None:
            return self._legacy_pdf_bytes
        if self._artifact is None:
            raise ValueError("released External Report has no retained artifact")
        return self._artifact.pdf_bytes

    @pdf_bytes.setter
    def pdf_bytes(self, value: bytes | None) -> None:
        """Accept legacy-row construction without making new services copy bytes."""
        self._legacy_pdf_bytes = value

    @property
    def evaluation_context_json(self) -> dict | None:
        """Read frozen Evaluation context from its one durable owner."""
        if self._legacy_evaluation_context_json is not None:
            return deepcopy(self._legacy_evaluation_context_json)
        if self._artifact is None:
            return None
        return deepcopy(self._artifact.evaluation_context_json)

    @evaluation_context_json.setter
    def evaluation_context_json(self, value: dict | None) -> None:
        """Accept legacy-row construction without copying new release context."""
        self._legacy_evaluation_context_json = deepcopy(value)

    @property
    def record_context_json(self) -> dict:
        """Read frozen Project Record context from its one durable owner."""
        if self._legacy_record_context_json is not None:
            return deepcopy(self._legacy_record_context_json)
        if self._artifact is None:
            raise ValueError("released External Report has no retained record context")
        return deepcopy(self._artifact.record_context_json)

    @record_context_json.setter
    def record_context_json(self, value: dict | None) -> None:
        """Accept legacy-row construction without copying new release context."""
        self._legacy_record_context_json = deepcopy(value)

    @property
    def digest_is_valid(self) -> bool:
        """Whether the receipt and its one retained byte owner share a digest."""
        if self._legacy_pdf_bytes is not None:
            return sha256(self._legacy_pdf_bytes).hexdigest() == self.pdf_sha256
        return (
            self._artifact is not None
            and self._artifact.pdf_sha256 == self.pdf_sha256
            and self._artifact.digest_is_valid
        )


class ScheduledReportPublication(Base):
    """One retained weekly reading a scheduled Due Work occurrence produced.

    A scheduled publication supplements the working view; it never becomes a
    ``ReportRun`` and so never advances the internal report's comparison
    baseline (ADR-0053).  Each row binds one occurrence to one coherent project
    reading, the predecessor it compared against — the last Report Approved for
    Release at observation time, ``NULL`` before a project's first release — and,
    when the schedule declared external preparation, the exact prepared PDF
    artifact awaiting a separate human release (ADR-0040).

    The unique occurrence binding is the convergence guarantee: repeated
    triggers, competing workers, abandoned claims, and retries all resolve to
    this one row rather than a second snapshot or artifact.  The row is
    append-only; a refreshed working view or a later release cannot rewrite the
    predecessor or comparison window it recorded.
    """

    __tablename__ = "scheduled_report_publications"
    __table_args__ = (
        UniqueConstraint(
            "occurrence_id", name="uq_scheduled_report_publication_occurrence"
        ),
        CheckConstraint(
            "provenance_mode in ('all-supported-sources', 'document-only')",
            name="ck_scheduled_report_publication_provenance_mode",
        ),
        CheckConstraint(
            "(predecessor_release_id is null) = (window_start is null)",
            name="ck_scheduled_report_publication_predecessor_window",
        ),
        CheckConstraint(
            "(window_start is null) = (comparison_window_days is null)",
            name="ck_scheduled_report_publication_window_days",
        ),
        CheckConstraint(
            "comparison_window_days is null or comparison_window_days >= 0",
            name="ck_scheduled_report_publication_window_nonneg",
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot_json) = 'object'",
            name="ck_scheduled_report_publication_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(thresholds_json) = 'object'",
            name="ck_scheduled_report_publication_thresholds_object",
        ),
        ForeignKeyConstraint(
            ["revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_scheduled_report_publications_revision",
        ),
        Index("ix_scheduled_report_publications_revision_id", "revision_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_occurrences.id")
    )
    schedule_id: Mapped[int] = mapped_column(ForeignKey("due_work_schedules.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    configuration_version: Mapped[str] = mapped_column(String(64))
    provenance_mode: Mapped[str] = mapped_column(String(32))
    # Reference ids into two append-only stores.  They are plain ids, not
    # foreign keys, so this retained row never changes the truncate or deletion
    # semantics of the release and artifact tables it points at; those rows are
    # immutable and never removed, so a dangling reference cannot arise.
    predecessor_release_id: Mapped[int | None] = mapped_column(BigInteger)
    prepared_artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    # The accepted revision this reading was taken against (#602), under the
    # same rule and the same trigger as ``ReportRun.revision_id``.  Unlike the
    # two reference ids above it does carry a foreign key, a composite one to
    # ``(id, project_id)``: what has to be unrepresentable here is not a
    # dangling row but a reading bound to some other project's revision, and
    # only a key can say that.
    revision_id: Mapped[int | None] = mapped_column(BigInteger)
    evaluated_on: Mapped[date] = mapped_column(Date)
    window_start: Mapped[date | None] = mapped_column(Date)
    comparison_window_days: Mapped[int | None] = mapped_column(Integer)
    ruleset_version: Mapped[str] = mapped_column(String(64))
    thresholds_json: Mapped[dict] = mapped_column(JSONB)
    # The immutable Report Reading payload, exactly as on ``ReportRun``: the
    # occurrence's own population, derived outcomes and projected Promised For,
    # retained for as long as this row and its released package are, and never
    # a cache of ``revision_id`` (ADR-0092).
    snapshot_json: Mapped[dict] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
    retirement_report_run_watermark_id: Mapped[int | None] = mapped_column(BigInteger)
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ShadowProject(Base):
    """An isolated shadow environment's project binding, unavailable to the web login."""

    __tablename__ = "shadow_projects"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    environment: Mapped[str] = mapped_column(Text)
    customer: Mapped[str] = mapped_column(Text)
    database_name: Mapped[str] = mapped_column(Text)
    bootstrap_operator: Mapped[str] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShadowRun(Base):
    """Frozen native predictions and their custody, never accepted record authority."""

    __tablename__ = "shadow_runs"
    identity: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("shadow_projects.project_id"))
    payload: Mapped[dict] = mapped_column(JSONB)
    output_sha256: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class ExtractionMeasurementCaseState(Base):
    """One immutable state in a human-ruling measurement case.

    The stable ``case_key`` groups corrections to the same ruling subject.
    Each correction or reversal appends a successor row; no row here changes
    the Project Record or rewrites an earlier human conclusion.
    """

    __tablename__ = "extraction_measurement_case_states"
    __table_args__ = (
        CheckConstraint(
            "kind in ('candidate_correction', 'source_discrepancy_settlement', "
            "'do_not_add', 'statement_fact_correction', "
            "'statement_scope_correction')",
            name="ck_extraction_measurement_case_states_kind",
        ),
        CheckConstraint(
            "state in ('active', 'reversed')",
            name="ck_extraction_measurement_case_states_state",
        ),
        CheckConstraint(
            "length(trim(case_key)) > 0 and length(trim(recorded_by)) > 0 "
            "and ruling_id > 0",
            name="ck_extraction_measurement_case_states_identity",
        ),
        CheckConstraint(
            "jsonb_typeof(source_identity_json) = 'object' and "
            "source_identity_json ?& array["
            "'candidate_id', 'extraction_run_id', 'documents'] and "
            "jsonb_typeof(source_identity_json -> 'documents') = 'array' and "
            "jsonb_array_length(source_identity_json -> 'documents') > 0",
            name="ck_extraction_measurement_case_states_source",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_json) = 'object' and "
            "jsonb_typeof(expected_json -> 'scoring_rule') = 'string' and "
            "length(trim(expected_json ->> 'scoring_rule')) > 0",
            name="ck_extraction_measurement_case_states_expected",
        ),
        UniqueConstraint(
            "ruling_type",
            "ruling_id",
            name="uq_extraction_measurement_case_states_ruling",
        ),
        Index(
            "uq_extraction_measurement_case_states_root",
            "case_key",
            unique=True,
            postgresql_where=text("predecessor_state_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    case_key: Mapped[str] = mapped_column(String(160), index=True)
    predecessor_state_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_measurement_case_states.id"), unique=True
    )
    kind: Mapped[str] = mapped_column(String(48))
    state: Mapped[str] = mapped_column(String(16))
    ruling_type: Mapped[str] = mapped_column(String(64))
    ruling_id: Mapped[int] = mapped_column(BigInteger)
    source_identity_json: Mapped[dict] = mapped_column(JSONB)
    expected_json: Mapped[dict] = mapped_column(JSONB)
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
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


class Dependency(Base):
    __tablename__ = "dependencies"
    __table_args__ = (
        UniqueConstraint("project_id", "ref_code"),
        UniqueConstraint("project_id", "id", name="uq_dependencies_project_id"),
    )

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
    # A source-proven selector, not a judgment.  ADR-0052 uses it with the
    # resolution method to select the standard documentation fields that are
    # required at read time.  ``reimbursable`` is the one currently modeled
    # value; unknown or absent source wording deliberately selects nothing.
    # Deferred like milestone_registration_id: migration rehearsals load
    # Dependency rows on databases pinned before this column existed.
    cost_responsibility: Mapped[str | None] = mapped_column(
        String(64), deferred=True
    )
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
    """One project-team membership, and the authority it carries.

    A Work Decision keeps its human-readable owner projection for existing
    readers, but a guided save must not turn a typed project roster into a
    caller-supplied string.  The grouping receipt binds the exact roster row
    that supplied the rendered name.

    The membership is also the project-scoped access boundary (#331).  An
    ``active`` row makes the person a member who may read the project and be
    assigned work, but membership alone is not authority: each write designation
    is an explicit, independently granted flag (ADR-0034 decisions 28 and 34,
    ADR-0035).  Possessing a signed-in principal, or merely appearing on the
    roster, confers none of them by default — they fail closed.
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
    # The four distinct designations of #331.  None is implied by another or by
    # membership: a coordinator is not a Documentation Reviewer, a reviewer may
    # not release externally, and none of them is a technical operator.
    can_coordinate: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    can_review_documentation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    can_release_externally: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    is_technical_operator: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PersonIdentity(Base):
    """The stable subject a verified email resolves to, and nothing more.

    Sign-in authenticates a person (email -> principal); membership and its
    designations decide what that person may do, per project.  This table is
    deliberately global and authority-free: creating it grants no project access
    and no organization-registry power (#331).  It is never backfilled for
    historical actors — an existing audit or decision principal keeps its own
    identity, and its email basis stays unknown unless a person enrolls (ADR-0035).
    """

    __tablename__ = "person_identities"
    __table_args__ = (
        UniqueConstraint("email_normalized", name="uq_person_identity_email"),
        UniqueConstraint("principal_subject", name="uq_person_identity_principal"),
        CheckConstraint(
            "length(trim(email_normalized)) > 0", name="ck_person_identity_email"
        ),
        CheckConstraint(
            "length(trim(principal_subject)) > 0",
            name="ck_person_identity_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_normalized: Mapped[str] = mapped_column(Text)
    principal_subject: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatedByPerson(Base):
    """One project-scoped individual who may be the person in Stated By.

    This is Project Record identity, not authentication. A registered person
    may be an external speaker with no Corridor account; conversely a signed-in
    PersonIdentity gains no statement attribution merely by existing.
    """

    __tablename__ = "stated_by_people"
    __table_args__ = (
        CheckConstraint(
            "length(trim(display_name)) > 0",
            name="ck_stated_by_people_display_name",
        ),
        CheckConstraint(
            "email_normalized is null or "
            "(length(trim(email_normalized)) > 0 and "
            "email_normalized = lower(trim(email_normalized)))",
            name="ck_stated_by_people_email",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    display_name: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    email_normalized: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubjectResolutionAttempt(Base):
    """One provenance-bound exact lookup of a source reference.

    The row is an observation of what the released exact-alias rule saw.  It
    never doubles as the Human Record Decision that may later register an
    alias, and unresolved outcomes remain durable even after that decision.
    """

    __tablename__ = "subject_resolution_attempts"
    __table_args__ = (
        UniqueConstraint("content_sha256", name="uq_subject_resolution_content"),
        ForeignKeyConstraint(
            ["project_id", "source_document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_subject_resolution_segment_scope",
        ),
        CheckConstraint(
            "reference_kind in ('organization_name', 'email_sender', "
            "'email_domain', 'person_name', 'person_email', "
            "'source_identifier', 'activity_identifier', 'document_identifier')",
            name="ck_subject_resolution_reference_kind",
        ),
        CheckConstraint(
            "expected_subject_type in ('external_org', 'person', 'constraint', 'document')",
            name="ck_subject_resolution_expected_type",
        ),
        CheckConstraint(
            "usage in ('identity', 'statement_speaker', 'affected_subject')",
            name="ck_subject_resolution_usage",
        ),
        CheckConstraint(
            "state in ('resolved', 'unresolved', 'conflict', 'stale', 'actor_boundary')",
            name="ck_subject_resolution_state",
        ),
        CheckConstraint(
            "length(trim(raw_reference)) > 0 and length(trim(normalized_reference)) > 0",
            name="ck_subject_resolution_reference",
        ),
        CheckConstraint(
            "rule_identity = 'exact-registered-alias-v1'",
            name="ck_subject_resolution_rule",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_subject_resolution_sha256",
        ),
        CheckConstraint(
            "(state = 'resolved' and "
            "num_nonnulls(resolved_external_org_id, resolved_stated_by_person_id, "
            "resolved_dependency_id, resolved_document_id) = 1 and "
            "((expected_subject_type = 'external_org' and resolved_external_org_id is not null) or "
            "(expected_subject_type = 'person' and resolved_stated_by_person_id is not null) or "
            "(expected_subject_type = 'constraint' and resolved_dependency_id is not null) or "
            "(expected_subject_type = 'document' and resolved_document_id is not null))) or "
            "(state <> 'resolved' and "
            "num_nonnulls(resolved_external_org_id, resolved_stated_by_person_id, "
            "resolved_dependency_id, resolved_document_id) = 0)",
            name="ck_subject_resolution_target",
        ),
        CheckConstraint(
            "(state = 'resolved' and attention_reason is null) or "
            "(state <> 'resolved' and length(trim(attention_reason)) > 0)",
            name="ck_subject_resolution_attention",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_document_id: Mapped[int] = mapped_column(BigInteger)
    source_segment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reference_kind: Mapped[str] = mapped_column(String(48))
    raw_reference: Mapped[str] = mapped_column(Text)
    normalized_reference: Mapped[str] = mapped_column(Text)
    expected_subject_type: Mapped[str] = mapped_column(String(32))
    usage: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24))
    attention_reason: Mapped[str | None] = mapped_column(String(64))
    rule_identity: Mapped[str] = mapped_column(String(64))
    resolved_external_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_orgs.id")
    )
    resolved_stated_by_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("stated_by_people.id")
    )
    resolved_dependency_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependencies.id")
    )
    resolved_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id")
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubjectResolutionCandidate(Base):
    """One typed candidate retained on an unresolved exact lookup."""

    __tablename__ = "subject_resolution_candidates"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "id", name="uq_subject_resolution_candidate_scope"
        ),
        UniqueConstraint(
            "attempt_id", "subject_key", name="uq_subject_resolution_candidate"
        ),
        CheckConstraint(
            "subject_type in ('external_org', 'person', 'constraint', 'document')",
            name="ck_subject_resolution_candidate_type",
        ),
        CheckConstraint(
            "candidate_state in ('active', 'stale')",
            name="ck_subject_resolution_candidate_state",
        ),
        CheckConstraint(
            "num_nonnulls(external_org_id, stated_by_person_id, dependency_id, document_id) = 1 and "
            "((subject_type = 'external_org' and external_org_id is not null) or "
            "(subject_type = 'person' and stated_by_person_id is not null) or "
            "(subject_type = 'constraint' and dependency_id is not null) or "
            "(subject_type = 'document' and document_id is not null))",
            name="ck_subject_resolution_candidate_target",
        ),
        CheckConstraint(
            "match_source in ('registered_alias', 'human_alias_decision')",
            name="ck_subject_resolution_candidate_match_source",
        ),
        CheckConstraint(
            "length(trim(subject_key)) > 0 and length(trim(display_name)) > 0",
            name="ck_subject_resolution_candidate_text",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("subject_resolution_attempts.id"), index=True
    )
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(String(96))
    external_org_id: Mapped[int | None] = mapped_column(ForeignKey("external_orgs.id"))
    stated_by_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("stated_by_people.id")
    )
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    display_name: Mapped[str] = mapped_column(Text)
    candidate_state: Mapped[str] = mapped_column(String(16))
    match_source: Mapped[str] = mapped_column(String(64))


class SubjectResolutionDecision(Base):
    """One attributable Human Record Decision registering an exact alias."""

    __tablename__ = "subject_resolution_decisions"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_subject_resolution_decision_attempt"),
        UniqueConstraint("revision_id", name="uq_subject_resolution_decision_revision"),
        UniqueConstraint(
            "project_id",
            "reference_kind",
            "normalized_reference",
            name="uq_subject_resolution_registered_alias",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_subject_resolution_decision_segment_scope",
        ),
        CheckConstraint(
            "decision_kind = 'human_alias_registration'",
            name="ck_subject_resolution_decision_kind",
        ),
        CheckConstraint(
            "reference_kind in ('organization_name', 'email_sender', "
            "'email_domain', 'person_name', 'person_email', "
            "'source_identifier', 'activity_identifier', 'document_identifier')",
            name="ck_subject_resolution_decision_reference_kind",
        ),
        CheckConstraint(
            "subject_type in ('external_org', 'person', 'constraint', 'document')",
            name="ck_subject_resolution_decision_type",
        ),
        CheckConstraint(
            "num_nonnulls(external_org_id, stated_by_person_id, dependency_id, document_id) = 1 and "
            "((subject_type = 'external_org' and external_org_id is not null) or "
            "(subject_type = 'person' and stated_by_person_id is not null) or "
            "(subject_type = 'constraint' and dependency_id is not null) or "
            "(subject_type = 'document' and document_id is not null))",
            name="ck_subject_resolution_decision_target",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0 and length(trim(normalized_reference)) > 0",
            name="ck_subject_resolution_decision_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("subject_resolution_attempts.id"), index=True
    )
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("project_record_revisions.id"), index=True
    )
    source_document_id: Mapped[int] = mapped_column(BigInteger)
    source_segment_id: Mapped[int] = mapped_column(BigInteger)
    reference_kind: Mapped[str] = mapped_column(String(48))
    raw_reference: Mapped[str] = mapped_column(Text)
    normalized_reference: Mapped[str] = mapped_column(Text)
    decision_kind: Mapped[str] = mapped_column(String(48))
    subject_type: Mapped[str] = mapped_column(String(32))
    external_org_id: Mapped[int | None] = mapped_column(ForeignKey("external_orgs.id"))
    stated_by_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("stated_by_people.id")
    )
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubjectCandidateSuggestion(Base):
    """One read-only model ranking over already-retained candidates."""

    __tablename__ = "subject_candidate_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "rank", name="uq_subject_candidate_suggestion_rank"
        ),
        UniqueConstraint(
            "attempt_id", "candidate_id", name="uq_subject_candidate_suggestion_candidate"
        ),
        CheckConstraint("rank > 0", name="ck_subject_candidate_suggestion_rank"),
        CheckConstraint(
            "length(trim(model)) > 0 and length(trim(prompt_version)) > 0",
            name="ck_subject_candidate_suggestion_model",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "candidate_id"],
            [
                "subject_resolution_candidates.attempt_id",
                "subject_resolution_candidates.id",
            ],
            name="fk_subject_candidate_suggestion_scope",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("subject_resolution_attempts.id"), index=True
    )
    candidate_id: Mapped[int] = mapped_column(BigInteger)
    rank: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SignInToken(Base):
    """One expiring, single-use magic-link secret, stored only as a hash.

    The raw token lives only in the emailed link; the column is its SHA-256 so a
    database read can never replay a link.  ``consumed_at`` is the single-use
    guard: consumption is an atomic ``UPDATE ... WHERE consumed_at IS NULL AND
    expires_at > now()`` so an expired, reused, tampered, or concurrently
    consumed link can never establish a second session (#331).
    """

    __tablename__ = "sign_in_tokens"
    __table_args__ = (
        UniqueConstraint("token_sha256", name="uq_sign_in_token_hash"),
        CheckConstraint("length(token_sha256) = 64", name="ck_sign_in_token_hash"),
        CheckConstraint("expires_at > created_at", name="ck_sign_in_token_expiry"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_normalized: Mapped[str] = mapped_column(Text)
    token_sha256: Mapped[str] = mapped_column(String(64))
    redirect_path: Mapped[str | None] = mapped_column(Text, server_default=text("null"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("null")
    )


class WebSession(Base):
    """A signed-in browser session: explicit expiry, revocable, hash-stored.

    The cookie carries a random id; this row stores only its SHA-256, so a
    database read cannot resume a session.  ``expires_at`` and ``revoked_at`` are
    both checked on every request, which is why revoking a session (logout) or a
    membership takes effect immediately, with no reliance on a stale roster or an
    earlier page load (#331).  ``csrf_sha256`` is the hash of the per-session
    request-forgery token echoed by authenticated writes.
    """

    __tablename__ = "web_sessions"
    __table_args__ = (
        UniqueConstraint("session_sha256", name="uq_web_session_hash"),
        CheckConstraint("length(session_sha256) = 64", name="ck_web_session_hash"),
        CheckConstraint("length(csrf_sha256) = 64", name="ck_web_session_csrf"),
        CheckConstraint("expires_at > created_at", name="ck_web_session_expiry"),
        CheckConstraint(
            "length(trim(principal_subject)) > 0", name="ck_web_session_principal"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_sha256: Mapped[str] = mapped_column(String(64))
    csrf_sha256: Mapped[str] = mapped_column(String(64))
    principal_subject: Mapped[str] = mapped_column(String(128))
    email_normalized: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("null")
    )


class SignInAttempt(Base):
    """Append-only record of one issuance or consumption attempt, for backoff.

    Counting rows in a recent window bounds how many email or token attempts an
    unauthenticated caller can generate (#331).  The scope is the throttle key —
    a normalized email or a client address — never a claim that the email maps to
    a member, so the counter cannot be used to enumerate membership.
    """

    __tablename__ = "sign_in_attempts"
    __table_args__ = (
        Index(
            "ix_sign_in_attempt_scope",
            "scope_kind",
            "scope_value",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(32))
    scope_value: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
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
    __table_args__ = (
        UniqueConstraint("dependency_id", "id"),
        # The rendition key an EvidenceLinkSource resolves against, so a cited
        # Source Segment cannot come from a different document (#605).
        UniqueConstraint(
            "document_id", "id", name="uq_evidence_links_document_id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Direct record Evidence owns one Dependency. Statement Evidence leaves
    # this null and is owned by DependencyEventEvidence instead; neither
    # relationship is inferred from missing data.
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    page_no: Mapped[int] = mapped_column(Integer)
    # Superseded for new writes by EvidenceLinkSource (ADR-0068, #605): the
    # Source Segment owns its exact text once and this column copies it.
    # Existing rows keep their copy and stay readable through
    # ``corridor.evidence_citations.evidence_quotation``; nothing rewrites
    # them in bulk until a sibling ticket proves the two texts equivalent.
    quote: Mapped[str] = mapped_column(Text)
    # The quote appears on the cited page. Nothing more.
    verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceLinkSource(Base):
    """One Source Segment an Evidence Link cites, in the same rendition.

    The reference ADR-0068 asked for: an Evidence Link names the segments that
    own the words rather than carrying a second copy of them.  The row holds no
    text at all, so the cited wording has exactly one owner and can be replayed
    from the registered source bytes through the segment's typed locator.

    It mirrors ``fact_sources`` and ``support_assessment_sources`` rather than
    inventing a shape: link, segment, ordinal, and composite keys that make a
    citation of another project's or another document's segment
    unrepresentable.  Rows are append-only.
    """

    __tablename__ = "evidence_link_sources"
    __table_args__ = (
        UniqueConstraint(
            "evidence_link_id",
            "source_segment_id",
            name="uq_evidence_link_sources_segment",
        ),
        UniqueConstraint(
            "evidence_link_id",
            "ordinal",
            name="uq_evidence_link_sources_ordinal",
        ),
        ForeignKeyConstraint(
            ["document_id", "evidence_link_id"],
            ["evidence_links.document_id", "evidence_links.id"],
            name="fk_evidence_link_sources_link_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_evidence_link_sources_segment_scope",
        ),
        CheckConstraint("ordinal > 0", name="ck_evidence_link_sources_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(BigInteger, index=True)
    evidence_link_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_segment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
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


class DocumentationFieldConfirmation(Base):
    """One append-only human confirmation of a cited interpretation field.

    Machine checklist fields are predicates over current cited documents and
    therefore have no stored checkmark.  This row exists only for the small
    interpretive residue ADR-0052 retains: a named person confirmed the
    system's cited reading of one exact current document.  A later source or
    supersession does not overwrite the row; it simply stops making the old
    confirmation applicable when the checklist is read.
    """

    __tablename__ = "documentation_field_confirmations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dependency_id", "evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_documentation_confirmation_owned_evidence",
        ),
        CheckConstraint(
            "field_name = 'approval_interpretation'",
            name="ck_documentation_confirmation_known_field",
        ),
        CheckConstraint(
            "classification in ('approved', 'conditional')",
            name="ck_documentation_confirmation_known_classification",
        ),
        CheckConstraint(
            "conclusion = 'approved'",
            name="ck_documentation_confirmation_known_conclusion",
        ),
        CheckConstraint(
            "length(trim(confirmed_by)) > 0",
            name="ck_documentation_confirmation_actor",
        ),
        # ADR-0060's optional override counts a hedge as immaterial.  It is
        # only meaningful on a conditional letter and must durably record the
        # exact hedge it overrode (the deposition answer for why a hedged
        # letter counted as approval), beside who did it and when (#373).
        CheckConstraint(
            "condition_immaterial = false or ("
            "classification = 'conditional' and overridden_condition_text is not null "
            "and length(trim(overridden_condition_text)) > 0)",
            name="ck_documentation_confirmation_override_records_hedge",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    evidence_link_id: Mapped[int] = mapped_column(BigInteger)
    field_name: Mapped[str] = mapped_column(String(64))
    # This preserves the exact machine reading the person was shown; it is
    # deliberately not a grant for the model to write a project conclusion.
    classification: Mapped[str] = mapped_column(String(64))
    conclusion: Mapped[str] = mapped_column(String(64))
    confirmed_by: Mapped[str] = mapped_column(Text)
    # False for the clean-letter confirm; True only when this row is the
    # optional ADR-0060 override that counts the quoted hedge as immaterial.
    condition_immaterial: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # The exact hedge the override treated as immaterial, retained verbatim.
    overridden_condition_text: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConditionResolution(Base):
    """One append-only attributable act that resolves an open condition.

    ADR-0060 makes a condition a field in its own words: the entry itself is
    derived at read time from the conditional letter, never a stored checkmark
    (exactly like every other machine field, ADR-0052).  What *is* stored is
    the small set of acts that move an open condition toward Ready — a person
    clearing it against a cited later passage or a recorded verbal, the
    exact-and-mechanical automatic clear, and a person dismissing a
    misdetection with a reason.  Nothing here can be produced by a
    misdetection alone, and a dismissal only ever removes a spurious blocker;
    neither direction can manufacture a false Ready.

    The composite foreign key to ``evidence_links(dependency_id, id)`` is the
    database-level guarantee that a resolution is about its own Constraint's
    letter, the same bypass boundary the checklist confirmations use.
    """

    __tablename__ = "condition_resolutions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dependency_id", "evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_condition_resolution_owned_evidence",
        ),
        ForeignKeyConstraint(
            ["basis_evidence_link_id"],
            ["evidence_links.id"],
            name="fk_condition_resolution_basis_evidence",
        ),
        ForeignKeyConstraint(
            ["basis_event_id"],
            ["dependency_events.id"],
            name="fk_condition_resolution_basis_event",
        ),
        CheckConstraint(
            "kind in ('cleared', 'dismissed')",
            name="ck_condition_resolution_kind",
        ),
        CheckConstraint(
            "length(trim(condition_text)) > 0",
            name="ck_condition_resolution_text",
        ),
        CheckConstraint(
            "length(trim(resolved_by)) > 0",
            name="ck_condition_resolution_actor",
        ),
        CheckConstraint(
            "kind <> 'cleared' or basis_evidence_link_id is not null "
            "or basis_event_id is not null",
            name="ck_condition_resolution_clear_has_basis",
        ),
        CheckConstraint(
            "kind <> 'dismissed' or (reason is not null and length(trim(reason)) > 0)",
            name="ck_condition_resolution_dismissal_has_reason",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    # The source conditional letter passage this act is about.
    evidence_link_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    # A durable verbatim copy of the exact words resolved — legible after a
    # later supersession, and never anything but stored data.
    condition_text: Mapped[str] = mapped_column(Text)
    # A clear cites its basis: a later verified passage and/or a recorded
    # verbal statement.  A dismissal carries no basis, only a reason.
    basis_evidence_link_id: Mapped[int | None] = mapped_column(BigInteger)
    basis_event_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    # The exact-and-mechanical automatic clear keeps its reproducible receipt
    # here (ADR-0050); a human act leaves it null.
    receipt_json: Mapped[dict | None] = mapped_column(JSONB)
    resolved_by: Mapped[str] = mapped_column(String(128))
    resolved_at: Mapped[datetime] = mapped_column(
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


class KeyDateDraftReceipt(Base):
    """One bounded, source-bound, non-authoritative Key date drafting attempt.

    This receipt deliberately has no relationship to ``Milestone`` or
    ``Dependency``.  A model can leave a draft here, but only the existing
    human import and linking commands can change the Project Record.
    """

    __tablename__ = "key_date_draft_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "source_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_key_date_draft_receipts_source_same_project",
        ),
        CheckConstraint(
            "status in ('drafted', 'abstained', 'failed')",
            name="ck_key_date_draft_receipts_status",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_key_date_draft_receipts_source_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_pages_json) = 'array' and "
            "jsonb_typeof(configuration_json) = 'object' and "
            "jsonb_typeof(budget_json) = 'object' and "
            "jsonb_typeof(usage_json) = 'object' and "
            "jsonb_typeof(unresolved_json) = 'array' and "
            "jsonb_typeof(sequencing_json) = 'array'",
            name="ck_key_date_draft_receipts_json",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_document_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_sha256: Mapped[str] = mapped_column(String(64))
    allowed_pages_json: Mapped[list] = mapped_column(JSONB)
    requested_by: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    configuration_json: Mapped[dict] = mapped_column(JSONB)
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    unresolved_json: Mapped[list] = mapped_column(JSONB)
    sequencing_json: Mapped[list] = mapped_column(JSONB)
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KeyDateDraftRowReceipt(Base):
    """One validated day-precise row retained beside its draft receipt."""

    __tablename__ = "key_date_draft_row_receipts"
    __table_args__ = (
        UniqueConstraint("receipt_id", "ordinal", name="uq_key_date_draft_row_ordinal"),
        CheckConstraint("precision = 'day'", name="ck_key_date_draft_row_precision"),
        CheckConstraint("source_page > 0", name="ck_key_date_draft_row_source_page"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("key_date_draft_receipts.id"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    need_date: Mapped[date] = mapped_column(Date)
    precision: Mapped[str] = mapped_column(String(16))
    source_page: Mapped[int] = mapped_column(Integer)
    source_quote: Mapped[str] = mapped_column(Text)


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

    Extractors write only here. Dependency and event Admission requires either
    human Adjudication or one exact deterministic policy outcome with an
    immutable receipt. Evidence Candidates are technical source-row proposals;
    they remain outside both Admission families until an explicit later linker
    uses them.
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


class StatementSuggestionEligibilityDeclaration(Base):
    """One explicit approval to expose deterministic statement ordering."""

    __tablename__ = "statement_suggestion_eligibility_declarations"
    __table_args__ = (
        CheckConstraint(
            "length(trim(contract_version)) > 0",
            name="ck_statement_suggestion_eligibility_contract",
        ),
        UniqueConstraint(
            "candidate_id", name="uq_statement_suggestion_eligibility_candidate"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    contract_version: Mapped[str] = mapped_column(String(128))
    declared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementSuggestionProtection(Base):
    """One declared cohort window during which statement ordering is withheld."""

    __tablename__ = "statement_suggestion_protections"
    __table_args__ = (
        CheckConstraint(
            "kind in ('shadow_cohort', 'no_agent_baseline')",
            name="ck_statement_suggestion_protection_kind",
        ),
        CheckConstraint(
            "length(trim(observation_contract)) > 0",
            name="ck_statement_suggestion_protection_contract",
        ),
        UniqueConstraint(
            "candidate_id",
            "kind",
            "observation_contract",
            name="uq_statement_suggestion_protection_window",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    observation_contract: Mapped[str] = mapped_column(String(128))
    declared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatementSuggestionProtectionEnd(Base):
    """Append-only conclusion of one declared suggestion-protection window."""

    __tablename__ = "statement_suggestion_protection_ends"
    __table_args__ = (
        UniqueConstraint(
            "protection_id", name="uq_statement_suggestion_protection_end"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    protection_id: Mapped[int] = mapped_column(
        ForeignKey("statement_suggestion_protections.id"), index=True
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


class EvidenceInvestigationCaptureContract(Base):
    """Declared gate-7 contract for delayed cutoff-correct outcome capture.

    One approved observation contract names an exact frozen cohort, the sealed
    configuration identities it may associate, the observation window and its
    intended cutoff, the protection end, and the retained-history coverage the
    reconstruction is allowed to trust.  It is content-addressed and append-only:
    a different cutoff, membership, or identity is a different contract, never a
    rewrite of this one, and an incomplete or unapproved declaration is never
    written at all (the capture stays disabled).
    """

    __tablename__ = "evidence_investigation_capture_contracts"
    __table_args__ = (
        CheckConstraint(
            "missing_label_policy = 'remain_missing'",
            name="ck_capture_contract_missing_label_policy",
        ),
        CheckConstraint(
            "length(trim(declared_by)) > 0",
            name="ck_capture_contract_actor",
        ),
        CheckConstraint(
            "window_start <= cutoff_at",
            name="ck_capture_contract_window_before_cutoff",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    cohort_id: Mapped[str] = mapped_column(String(128))
    contract_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    baseline_identity: Mapped[str] = mapped_column(String(128))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    protection_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history_retained_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    missing_label_policy: Mapped[str] = mapped_column(String(32))
    member_case_public_ids_json: Mapped[list] = mapped_column(JSONB)
    contract_json: Mapped[dict] = mapped_column(JSONB)
    declared_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceInvestigationCaptureResult(Base):
    """One cutoff-correct association of a frozen case's independent outcome.

    The association records the intended cutoff and the actual execution time
    separately, binds the exact project, frozen case, execution run, human
    outcome, and source-receipt identities, and states its completeness.  It
    never rewrites the frozen case, its run, or the immutable one-time capture,
    and a reconstruction that retained history cannot support exactly is kept as
    ``incomplete`` with its reason rather than labelled as cutoff-time truth.
    """

    __tablename__ = "evidence_investigation_capture_results"
    __table_args__ = (
        UniqueConstraint(
            "capture_contract_id",
            "shadow_case_id",
            name="uq_capture_result_case",
        ),
        CheckConstraint(
            "completeness in ('complete', 'incomplete')",
            name="ck_capture_result_completeness",
        ),
        CheckConstraint(
            "(completeness = 'complete' and incomplete_reason is null) or "
            "(completeness = 'incomplete' and incomplete_reason is not null)",
            name="ck_capture_result_incomplete_reason",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    capture_contract_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_capture_contracts.id"), index=True
    )
    shadow_case_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_shadow_cases.id")
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("evidence_investigation_runs.id")
    )
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completeness: Mapped[str] = mapped_column(String(16))
    incomplete_reason: Mapped[str | None] = mapped_column(String(64))
    candidate_disposition: Mapped[str | None] = mapped_column(String(32))
    scope_mode: Mapped[str | None] = mapped_column(String(32))
    selected_dependency_ids_json: Mapped[list] = mapped_column(JSONB)
    correction: Mapped[bool] = mapped_column(Boolean)
    undo: Mapped[bool] = mapped_column(Boolean)
    unresolved: Mapped[bool] = mapped_column(Boolean)
    human_outcome_identity: Mapped[str | None] = mapped_column(String(64))
    outcome_identities_json: Mapped[dict] = mapped_column(JSONB)
    strata_json: Mapped[list] = mapped_column(JSONB)
    review_seconds: Mapped[float | None] = mapped_column(Float)
    association_sha256: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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


class FollowUpPlanReceipt(Base):
    """The immutable grouping identity for one Constraint Follow-up Plan Save.

    The named rows remain independent append-only Work Decisions (ADR-0038);
    this receipt only states which decisions one Save committed together,
    which predecessors the screen had read, and which exact roster row
    supplied the rendered Assigned To name.  No statement is manufactured to
    give a Constraint a grouping receipt (#333).
    """

    __tablename__ = "follow_up_plan_receipts"
    __table_args__ = (
        CheckConstraint(
            "internal_owner_decision_id is not null "
            "or next_action_decision_id is not null",
            name="ck_follow_up_plan_receipt_one_result",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_predecessors_json) = 'object'",
            name="ck_follow_up_plan_receipt_predecessors_object",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_follow_up_plan_receipt_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), index=True
    )
    internal_owner_roster_entry_id: Mapped[int] = mapped_column(
        ForeignKey("project_roster_entries.id")
    )
    internal_owner_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    next_action_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    resumed_deferral_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    expected_predecessors_json: Mapped[dict] = mapped_column(JSONB)
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FollowUpPlanReversal(Base):
    """The attributable compensating act for one grouped plan Save.

    Undo never edits or deletes the original decisions.  It appends one
    reversal Work Decision per grouped chain, restoring each predecessor
    value, and this row names those appended reversals so the grouped act
    stays auditable as one.
    """

    __tablename__ = "follow_up_plan_reversals"
    __table_args__ = (
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_follow_up_plan_reversal_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("follow_up_plan_receipts.id"), unique=True
    )
    internal_owner_reversal_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    next_action_reversal_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    deferral_reversal_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id"), unique=True
    )
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    recorded_by: Mapped[str] = mapped_column(String(128))
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


class DisputeHistoryResolution(Base):
    """One append-only ADR-0061 chronology outcome, never a human verdict.

    A physical source can be shown to be stale from the record's own change
    history.  That is a mechanical conclusion with a different authority from
    ``DisputeSettlement``: it must never look like a person chose a value.  A
    stale executed agreement is retained in this same chronology history, but
    its ``contractual_amendment`` outcome deliberately does *not* settle the
    field; it creates coordination work instead.
    """

    __tablename__ = "dispute_history_resolutions"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('physical_superseded', 'contractual_amendment')",
            name="ck_dispute_history_resolutions_outcome",
        ),
        CheckConstraint(
            "older_assertion_id <> newer_assertion_id",
            name="ck_dispute_history_resolutions_distinct_assertions",
        ),
        CheckConstraint(
            "length(trim(rule_version)) > 0",
            name="ck_dispute_history_resolutions_rule_version",
        ),
        UniqueConstraint(
            "dependency_id",
            "field_name",
            "covers_assertion_id",
            name="uq_dispute_history_resolutions_coverage",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    field_name: Mapped[str] = mapped_column(String(64))
    older_assertion_id: Mapped[int] = mapped_column(ForeignKey("assertions.id"))
    newer_assertion_id: Mapped[int] = mapped_column(ForeignKey("assertions.id"))
    # This is intentionally the exact newest Assertion the rule saw.  A later
    # assertion reopens a physical conclusion by the same coverage rule a
    # human settlement already uses.
    covers_assertion_id: Mapped[int] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(String(32))
    rule_version: Mapped[str] = mapped_column(String(64))
    why: Mapped[str] = mapped_column(Text)
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


class ScheduleGoverningDerivation(Base):
    """One immutable record of which schedule activities govern utility work.

    ADR-0057: governing dates identify themselves. When a schedule imports,
    activities whose codes and names match utility conventions flag themselves
    as the governing set with no human step, and this row records exactly which
    codes and names matched (``matches_json``). Only when the coding is too poor
    to read does a person pick, once — a ``human_pick`` row under their own
    subject. Append-only: a re-derivation that changes nothing writes nothing.
    """

    __tablename__ = "schedule_governing_derivations"
    __table_args__ = (
        CheckConstraint(
            "method in ('coded', 'awaiting_pick', 'human_pick')",
            name="ck_schedule_governing_method",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_schedule_governing_recorded_by",
        ),
        CheckConstraint(
            "jsonb_typeof(matches_json) = 'array'",
            name="ck_schedule_governing_matches",
        ),
        CheckConstraint(
            "source_sha256 is null or source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_schedule_governing_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_name: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(24))
    recorded_by: Mapped[str] = mapped_column(String(128))
    matches_json: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScheduleLinkReceipt(Base):
    """The deciding values behind one Constraint-to-key-date link.

    The binding itself is written by ``milestones.link_dependency`` (unchanged),
    which copies the exact Key Date Version onto the Constraint and audits it.
    This receipt retains *why* that activity was chosen — the Constraint's own
    station text and the governing activity's coverage, verbatim from both
    sources (ADR-0057, ADR-0051's exact-rule discipline). ``basis`` says whether
    an exact rule fired (``exact_station_containment``), a person resolved a tie
    or confirmed a single candidate (``human_choice``), or a schedule revision
    advanced an existing link (``flow_through``). ``audit_log_id`` ties this to
    the exact ``LINK_MILESTONE`` audit entry so the two can never drift.
    """

    __tablename__ = "schedule_link_receipts"
    __table_args__ = (
        UniqueConstraint(
            "audit_log_id", name="uq_schedule_link_receipts_audit"
        ),
        CheckConstraint(
            "basis in ('exact_station_containment', 'human_choice', 'flow_through')",
            name="ck_schedule_link_receipts_basis",
        ),
        CheckConstraint(
            "length(trim(decided_by)) > 0",
            name="ck_schedule_link_receipts_decided_by",
        ),
        CheckConstraint(
            "jsonb_typeof(deciding_values_json) = 'object'",
            name="ck_schedule_link_receipts_values",
        ),
        CheckConstraint(
            "policy_sha256 is null or policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_schedule_link_receipts_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), index=True
    )
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"))
    milestone_registration_id: Mapped[int] = mapped_column(
        ForeignKey("milestone_registrations.id")
    )
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"))
    basis: Mapped[str] = mapped_column(String(32))
    decided_by: Mapped[str] = mapped_column(String(128))
    # Null for a human choice: only the automatic exact rule stands on a
    # fingerprinted, replay-gated policy version.
    policy_version: Mapped[str | None] = mapped_column(String(64))
    policy_sha256: Mapped[str | None] = mapped_column(String(64))
    deciding_values_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScheduleLinkActivation(Base):
    """Append-only activation or suspension of the automatic location link rule.

    The exact location-link rule is a new automatic matching class, so ADR-0050
    governs it: it may not auto-write until a regression replay of the project's
    own recorded human link decisions passes with at least one real case and no
    contradiction. A passing replay writes an ``activate`` row (system actor); a
    deliberate human ``suspend`` beats any passing test, and only a human act
    lifts it (an ``activate`` under their own subject). A brand-new rule with no
    history has no passing replay and so never auto-links until a person has
    linked by hand — exactly ADR-0050's rule.
    """

    __tablename__ = "schedule_link_activations"
    __table_args__ = (
        CheckConstraint(
            "action in ('activate', 'suspend')",
            name="ck_schedule_link_activation_action",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_schedule_link_activation_reason",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_schedule_link_activation_actor",
        ),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_schedule_link_activation_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    # How many recorded human decisions the replay compared against. Null for a
    # suspension, which needs no proof.
    replay_case_count: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(160))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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


class UnreadableCellAdmissionActivation(Base):
    """Append-only activation/suspension of corroborated cross-document admission.

    Admitting a corroborated cell value across documents expands automatic Record
    Inclusion behavior, so ADR-0050 governs it: the class may not auto-admit until
    a regression replay of the project's own recorded human cell-value decisions
    passes with at least one real case and no contradiction. A passing replay
    writes an ``activate`` row under the system actor; a deliberate human
    ``suspend`` beats any passing test; only a human act lifts it. Shipped
    inactive — a project with no passing replay never auto-admits.
    """

    __tablename__ = "unreadable_cell_admission_activations"
    __table_args__ = (
        CheckConstraint(
            "action in ('activate', 'suspend')",
            name="ck_unreadable_cell_admission_action",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_unreadable_cell_admission_reason",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_unreadable_cell_admission_actor",
        ),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_unreadable_cell_admission_sha",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_sha256: Mapped[str] = mapped_column(String(64))
    replay_case_count: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(160))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- New-assignment notifications (#351) ---------------------------------
#
# A committed new roster-backed assignment produces exactly one immutable
# notification occurrence bound to its exact subject, assignment decision, and
# selected roster identity.  The occurrence is registered in the same
# transaction as the assignment, so a rolled-back save leaves no notification
# and a crash after commit cannot lose it (ADR-0032).  Delivery rides the one
# supervised Due Work runtime (#332) through a server-owned handler; the
# occurrence and its dispatch are the durable domain record, never a second
# scheduler.  Only the ``new_assignment`` interruption category exists here;
# reminders, escalation, and change notices are separately scoped successors.

# The one category this slice emits.  Kept as a check-constrained value rather
# than a free string so a later category cannot silently ride this table.
NEW_ASSIGNMENT_NOTIFICATION_CATEGORY = "new_assignment"
ASSIGNMENT_NOTIFICATION_SUBJECT_KINDS = ("constraint", "statement")
ASSIGNMENT_DELIVERY_STATES = (
    "queued",
    "completed",
    "retry_due",
    "failed",
    "uncertain",
)


class AssignmentNotification(Base):
    """One immutable new-assignment notification occurrence (#351, ADR-0032).

    Bound to the exact Coordination Subject, the exact assignment Work Decision
    that made the person accountable, and the selected roster identity.  The
    ``occurrence_key`` fingerprint makes repeated triggers and competing writers
    converge on one row; the occurrence never gates ownership, which takes
    effect on the assignment's own commit regardless of any delivery outcome.
    """

    __tablename__ = "assignment_notifications"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_assignment_notification_key"),
        CheckConstraint(
            "category = 'new_assignment'",
            name="ck_assignment_notification_category",
        ),
        CheckConstraint(
            "subject_kind in ('constraint', 'statement')",
            name="ck_assignment_notification_subject_kind",
        ),
        CheckConstraint(
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)",
            name="ck_assignment_notification_subject_shape",
        ),
        CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_assignment_notification_key_hex",
        ),
        CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_assignment_notification_actor",
        ),
        CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_assignment_notification_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    subject_kind: Mapped[str] = mapped_column(String(16))
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    assignment_decision_id: Mapped[int] = mapped_column(
        ForeignKey("work_decisions.id"), index=True
    )
    recipient_roster_entry_id: Mapped[int] = mapped_column(
        ForeignKey("project_roster_entries.id")
    )
    recipient_principal_subject: Mapped[str] = mapped_column(String(128))
    occurrence_key: Mapped[str] = mapped_column(String(64))
    registered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AssignmentNotificationDispatch(Base):
    """Mutable delivery standing for one notification occurrence (#351).

    The occurrence is immutable; this row carries what the shared runtime and
    the recipient inbox read: the queued / completed / retry-due / failed /
    uncertain state, the resolved verified contact (or a visible delivery
    limitation when the roster identity has no typed contact), retained provider
    result and idempotency evidence, and bounded retry state.  Its identity is
    immutable; only the delivery standing changes.
    """

    __tablename__ = "assignment_notification_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", name="uq_assignment_dispatch_notification"
        ),
        CheckConstraint("channel = 'email'", name="ck_assignment_dispatch_channel"),
        CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_assignment_dispatch_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_assignment_dispatch_attempt_count"
        ),
        CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_assignment_dispatch_retry_shape",
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_assignment_dispatch_idempotency_hex",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("assignment_notifications.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    delivery_state: Mapped[str] = mapped_column(String(16))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AssignmentNotificationAttempt(Base):
    """Append-only record of one notification delivery attempt (#351, ADR-0032).

    Every sweep of a dispatch appends one row: what was attempted, the retained
    provider result and idempotency evidence, and the explicit outcome —
    including an ``uncertain`` outcome when an acknowledgment is unavailable and
    a ``skipped`` outcome when a re-checked assignment is no longer current or a
    typed contact could not be resolved.  Nothing here is ever mutated.
    """

    __tablename__ = "assignment_notification_attempts"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_assignment_attempt_number"
        ),
        CheckConstraint(
            "outcome in "
            "('completed', 'retry_due', 'failed', 'uncertain', 'skipped')",
            name="ck_assignment_attempt_outcome",
        ),
        CheckConstraint(
            "attempt_number > 0", name="ck_assignment_attempt_positive"
        ),
        CheckConstraint(
            "length(trim(runtime_owner)) > 0",
            name="ck_assignment_attempt_owner",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("assignment_notification_dispatches.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    runtime_owner: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AssignmentNotificationFeedback(Base):
    """Append-only attributable feedback that an assignment looks incorrect (#351).

    The assigned person can flag a notification's assignment through this path.
    It preserves the existing assignment and its Work Decision history until an
    authorized person changes it — flagging records a signed marker, never a
    mutation of the assignment (ADR-0035).
    """

    __tablename__ = "assignment_notification_feedback"
    __table_args__ = (
        UniqueConstraint(
            "notification_id",
            "flagged_by",
            name="uq_assignment_feedback_person",
        ),
        CheckConstraint(
            "feedback_kind = 'incorrect_assignment'",
            name="ck_assignment_feedback_kind",
        ),
        CheckConstraint(
            "length(trim(flagged_by)) > 0",
            name="ck_assignment_feedback_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("assignment_notifications.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    flagged_by: Mapped[str] = mapped_column(String(128))
    feedback_kind: Mapped[str] = mapped_column(String(24))
    note: Mapped[str | None] = mapped_column(Text)
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DueActionNotification(Base):
    """One immutable due-action notification occurrence (#352, ADR-0032/0038).

    This extends the #351 new-assignment occurrence to the three derived
    categories of ADR-0034 decisions 39/48: a soon-due or past-due Next Action
    reminder (``next_action_due``), an urgent-overdue escalation
    (``next_action_escalation``), and a non-interrupting per-recipient daily
    summary (``daily_summary``).  Unlike a new assignment, these conditions are
    *derived* on each supervised tick from the subject's current authoritative
    plan and the applicable existing check semantics, so the occurrence binds the
    exact subject, current Next Action Work Decision identity, applicable
    check/configuration identity, observation window, urgency band, and typed
    recipient role.  The ``occurrence_key`` fingerprint deliberately excludes the
    poll time, so an unchanged condition converges on one row rather than
    becoming a new event on every poll; a genuinely new condition (a new plan
    decision, a soon->overdue crossing, a new check configuration, or a new
    summary window) is a new occurrence.  A daily summary carries no subject.
    """

    __tablename__ = "due_action_notifications"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_due_action_notification_key"),
        CheckConstraint(
            "category in "
            "('next_action_due', 'next_action_escalation', 'daily_summary')",
            name="ck_due_action_notification_category",
        ),
        CheckConstraint(
            "recipient_role in ('assignee', 'escalation', 'summary')",
            name="ck_due_action_notification_role",
        ),
        CheckConstraint(
            "urgency is null or urgency in ('soon', 'overdue', 'urgent_overdue')",
            name="ck_due_action_notification_urgency",
        ),
        # The one shape rule that couples category to its bound fields.  A daily
        # summary names a recipient and a window but no subject, plan, urgency or
        # due date; a reminder or escalation names exactly one subject, its
        # current Next Action decision, and an urgency band.
        CheckConstraint(
            "("
            "category = 'daily_summary' and subject_kind is null "
            "and dependency_id is null and commitment_lineage_id is null "
            "and plan_decision_id is null and urgency is null "
            "and action_due_date is null and recipient_role = 'summary' "
            "and observation_start is not null and observation_end is not null"
            ") or ("
            "category in ('next_action_due', 'next_action_escalation') "
            "and subject_kind in ('constraint', 'statement') "
            "and plan_decision_id is not null and urgency is not null "
            "and recipient_role in ('assignee', 'escalation') "
            "and ("
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)"
            ")"
            ")",
            name="ck_due_action_notification_shape",
        ),
        CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_due_action_notification_key_hex",
        ),
        CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_due_action_notification_actor",
        ),
        CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_due_action_notification_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    subject_kind: Mapped[str | None] = mapped_column(String(16))
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    # The current Next Action Work Decision the finding was derived against — the
    # plan identity that makes an unchanged condition converge and a changed plan
    # a new occurrence (ADR-0038's independent Next Action chain tail).
    plan_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_decisions.id")
    )
    urgency: Mapped[str | None] = mapped_column(String(16))
    action_due_date: Mapped[date | None] = mapped_column(Date)
    # The applicable check/configuration identity (ruleset version and the
    # effective threshold configuration) the finding was derived under.
    check_identity: Mapped[str | None] = mapped_column(String(128))
    # The observation window the occurrence covers.  For a reminder or escalation
    # it is the single observation date; for a daily summary it is the exposed
    # window the digest rolls up.
    observation_start: Mapped[date | None] = mapped_column(Date)
    observation_end: Mapped[date | None] = mapped_column(Date)
    # The frozen, non-interrupting digest a daily summary exposes: its window and
    # the eligible bounded counts for the recipient, never a replay of history.
    # Null for a subject-bound reminder or escalation.
    summary_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    recipient_role: Mapped[str] = mapped_column(String(16))
    recipient_roster_entry_id: Mapped[int] = mapped_column(
        ForeignKey("project_roster_entries.id")
    )
    recipient_principal_subject: Mapped[str] = mapped_column(String(128))
    configuration_version: Mapped[str] = mapped_column(String(64))
    occurrence_key: Mapped[str] = mapped_column(String(64))
    registered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DueActionNotificationDispatch(Base):
    """Mutable delivery standing for one due-action occurrence (#352).

    Identical delivery machinery to the #351 assignment dispatch: the occurrence
    is immutable and this row carries the queued / completed / retry-due / failed
    / uncertain state, the resolved verified contact (or a visible delivery
    limitation), retained provider evidence, and bounded retry state.  Its
    identity is immutable; only the delivery standing changes.
    """

    __tablename__ = "due_action_notification_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", name="uq_due_action_dispatch_notification"
        ),
        CheckConstraint("channel = 'email'", name="ck_due_action_dispatch_channel"),
        CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_due_action_dispatch_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_due_action_dispatch_attempt_count"
        ),
        CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_due_action_dispatch_retry_shape",
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_due_action_dispatch_idempotency_hex",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("due_action_notifications.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    delivery_state: Mapped[str] = mapped_column(String(16))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DueActionNotificationAttempt(Base):
    """Append-only record of one due-action delivery attempt (#352, ADR-0032).

    Every sweep of a dispatch appends one row: what was attempted, the retained
    provider result and idempotency evidence, and the explicit outcome —
    including an ``uncertain`` outcome when an acknowledgment is unavailable and a
    ``skipped`` outcome when the re-derived condition is no longer current (the
    action completed, was cancelled or deferred, the plan changed, membership was
    revoked, or a typed contact could not be resolved).  Nothing here is mutated.
    """

    __tablename__ = "due_action_notification_attempts"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_due_action_attempt_number"
        ),
        CheckConstraint(
            "outcome in "
            "('completed', 'retry_due', 'failed', 'uncertain', 'skipped')",
            name="ck_due_action_attempt_outcome",
        ),
        CheckConstraint(
            "attempt_number > 0", name="ck_due_action_attempt_positive"
        ),
        CheckConstraint(
            "length(trim(runtime_owner)) > 0",
            name="ck_due_action_attempt_owner",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("due_action_notification_dispatches.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    runtime_owner: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentNotification(Base):
    """One immutable document-related interruption occurrence (#353, ADR-0037).

    This carries the two remaining #196 immediate-notification categories
    (ADR-0034 decision 39): a previously affirmative Documentation Review whose
    applicable current support lapsed (``documentation_loss``), and an authentic
    registered source transition that affects a current Commitment or a
    relocation/removal/abandonment Constraint (``document_change``).  It shares
    the delivery adapter seam and the single supervised runtime with the
    new-assignment occurrence (#351) but never reuses its assignment-shaped row:
    a loss preserves the earlier review *and its author*, and one of its
    recipients (the original reviewer) may hold no current roster entry.

    The occurrence is bound to exact identities — the affirmative review, the
    reviewed supporting citation, and the proven source transition — so a
    persistent condition or a repeated processing pass converges on one row and
    never resends the same event.  Registration is a derived system act: Corridor
    stops showing the requirement as met and records visible high-priority work
    (ADR-0037); the earlier human judgment is preserved, never reversed here.
    """

    __tablename__ = "document_notifications"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_document_notification_key"),
        CheckConstraint(
            "category in ('documentation_loss', 'document_change')",
            name="ck_document_notification_category",
        ),
        CheckConstraint(
            "subject_kind in ('constraint', 'statement')",
            name="ck_document_notification_subject_kind",
        ),
        CheckConstraint(
            "(subject_kind = 'constraint' and dependency_id is not null "
            "and commitment_lineage_id is null) or "
            "(subject_kind = 'statement' and commitment_lineage_id is not null "
            "and dependency_id is null)",
            name="ck_document_notification_subject_shape",
        ),
        CheckConstraint(
            "recipient_role in "
            "('current_assignee', 'original_reviewer', "
            "'current_assignee_and_original_reviewer')",
            name="ck_document_notification_recipient_role",
        ),
        # A loss names the affirmative review it preserves; a change names an
        # authentic registered source transition (a proven revision comparison
        # or a superseding statement event).  Neither is inferred from a
        # filename, a date, or an unproven replacement.
        CheckConstraint(
            "(category = 'documentation_loss' and review_confirmation_id is not null) "
            "or (category = 'document_change' and "
            "(comparison_id is not null or statement_event_id is not null))",
            name="ck_document_notification_authentic_source",
        ),
        CheckConstraint(
            "occurrence_key ~ '^[0-9a-f]{64}$'",
            name="ck_document_notification_key_hex",
        ),
        CheckConstraint(
            "length(trim(registered_by)) > 0",
            name="ck_document_notification_actor",
        ),
        CheckConstraint(
            "length(trim(recipient_principal_subject)) > 0",
            name="ck_document_notification_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    subject_kind: Mapped[str] = mapped_column(String(16))
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id")
    )
    recipient_principal_subject: Mapped[str] = mapped_column(String(128))
    recipient_role: Mapped[str] = mapped_column(String(48))
    # Category A (documentation_loss): the earlier affirmative Documentation
    # Review, its stated requirement, and the exact reviewed supporting citation.
    review_confirmation_id: Mapped[int | None] = mapped_column(
        ForeignKey("documentation_field_confirmations.id")
    )
    requirement_field: Mapped[str | None] = mapped_column(String(64))
    reviewed_evidence_link_id: Mapped[int | None] = mapped_column(BigInteger)
    original_reviewer_subject: Mapped[str | None] = mapped_column(String(128))
    # The proven source transition (both categories where one applies): the
    # superseded and superseding documents, the immutable Revision Comparison
    # and finding, or the superseding statement event.
    predecessor_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id")
    )
    successor_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id")
    )
    comparison_id: Mapped[int | None] = mapped_column(BigInteger)
    finding_id: Mapped[int | None] = mapped_column(BigInteger)
    statement_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_events.id")
    )
    # A stable code for the current supported reason a review lost support, or
    # the change class a document transition produced; plain project language is
    # rendered from it, never shown as a raw code.
    reason_code: Mapped[str] = mapped_column(String(64))
    # An ambiguous or otherwise uncertain correspondence is retained honestly:
    # the message preserves the uncertainty and never presents it as proved.
    change_uncertain: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Exact source context the message links (requirement label, reviewed
    # citation, before/after, document names, page references).  Display only;
    # it never becomes a project decision and never leaves the project.
    source_context_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    occurrence_key: Mapped[str] = mapped_column(String(64))
    registered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentNotificationDispatch(Base):
    """Mutable delivery standing for one document-notification occurrence (#353).

    The occurrence is immutable; this row carries the queued / completed /
    retry-due / failed / uncertain state, the resolved verified contact (or a
    visible delivery limitation when the recipient has no current membership or
    typed contact, or the underlying condition resolved before dispatch),
    retained provider result and idempotency evidence, and bounded retry state.
    Its identity is immutable; only the delivery standing changes.
    """

    __tablename__ = "document_notification_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", name="uq_document_dispatch_notification"
        ),
        CheckConstraint("channel = 'email'", name="ck_document_dispatch_channel"),
        CheckConstraint(
            "delivery_state in "
            "('queued', 'completed', 'retry_due', 'failed', 'uncertain')",
            name="ck_document_dispatch_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_document_dispatch_attempt_count"
        ),
        CheckConstraint(
            "(delivery_state = 'retry_due' and next_attempt_at is not null) or "
            "(delivery_state <> 'retry_due' and next_attempt_at is null)",
            name="ck_document_dispatch_retry_shape",
        ),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_document_dispatch_idempotency_hex",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("document_notifications.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    delivery_state: Mapped[str] = mapped_column(String(16))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DocumentNotificationAttempt(Base):
    """Append-only record of one document-notification delivery attempt (#353).

    Every sweep of a dispatch appends one row: what was attempted, the retained
    provider result and idempotency evidence, and the explicit outcome —
    including an ``uncertain`` outcome when an acknowledgment is unavailable and
    a ``skipped`` outcome when a re-checked membership, contact, or underlying
    condition made the interruption no longer current.  Nothing here is mutated.
    """

    __tablename__ = "document_notification_attempts"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_id", "attempt_number", name="uq_document_attempt_number"
        ),
        CheckConstraint(
            "outcome in "
            "('completed', 'retry_due', 'failed', 'uncertain', 'skipped')",
            name="ck_document_attempt_outcome",
        ),
        CheckConstraint(
            "attempt_number > 0", name="ck_document_attempt_positive"
        ),
        CheckConstraint(
            "length(trim(runtime_owner)) > 0",
            name="ck_document_attempt_owner",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("document_notification_dispatches.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))
    recipient_contact: Mapped[str | None] = mapped_column(Text)
    delivery_limitation: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    provider_result_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    runtime_owner: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# Integrate at the end of models.py. These are storage-only receipt mappings;
# native services deliberately consume typed readers rather than ORM writers.
# Must follow the existing Base declaration. No migration helper is imported.
_legacy_history_batches = _legacy_sa.Table(
    "legacy_history_batches", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("run_key", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("executor", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("code_revision", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("inventory_version", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("content_sha256", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("payload", _legacy_pg.JSONB, nullable=False),
    _legacy_sa.Column("counts", _legacy_pg.JSONB, nullable=False),
    _legacy_sa.Column("captured_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("clock_timestamp()")),
    _legacy_sa.UniqueConstraint("project_id", "run_key"),
    _legacy_sa.CheckConstraint("length(btrim(run_key))>0"),
    _legacy_sa.CheckConstraint("length(btrim(executor))>0"),
    _legacy_sa.CheckConstraint("length(btrim(code_revision))>0"),
    _legacy_sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'"),
    _legacy_sa.CheckConstraint("jsonb_typeof(payload)='object'"),
    _legacy_sa.CheckConstraint("jsonb_typeof(counts)='object'"),
)

_legacy_history_reversals = _legacy_sa.Table(
    "legacy_history_reversals", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id"), nullable=False, unique=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("reversed_by", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("reason", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("reversed_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("transaction_timestamp()")),
    _legacy_sa.CheckConstraint("length(btrim(reversed_by))>0"),
    _legacy_sa.CheckConstraint("length(btrim(reason))>0"),
)

_legacy_history_evidence_migrations = _legacy_sa.Table(
    "legacy_history_evidence_migrations", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id"), nullable=False),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("legacy_evidence_link_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.Column("evidence_link_source_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("evidence_link_sources.id")),
    _legacy_sa.Column("source_segment_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("source_segments.id")),
    _legacy_sa.Column("original_quote_sha256", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("outcome", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("reason", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("created_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("transaction_timestamp()")),
    _legacy_sa.UniqueConstraint("batch_id", "legacy_evidence_link_id"),
    _legacy_sa.CheckConstraint("original_quote_sha256 ~ '^[0-9a-f]{64}$'"),
    _legacy_sa.CheckConstraint("outcome in ('segment_reference','already_native','retained_quote')"),
)

_coordination_record_subjects = _legacy_sa.Table(
    "coordination_record_subjects", Base.metadata,
    _legacy_sa.Column("id", _legacy_pg.UUID(as_uuid=True), primary_key=True, server_default=_legacy_sa.text("gen_random_uuid()")),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_kind", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("created_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("clock_timestamp()")),
    _legacy_sa.UniqueConstraint("project_id", "id"),
    _legacy_sa.CheckConstraint("subject_kind in ('constraint','commitment')"),
)

_coordination_subject_lineage = _legacy_sa.Table(
    "coordination_subject_lineage", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_id", _legacy_pg.UUID(as_uuid=True), _legacy_sa.ForeignKey("coordination_record_subjects.id"), nullable=False, unique=True),
    _legacy_sa.Column("legacy_dependency_id", _legacy_sa.BigInteger, unique=True),
    _legacy_sa.Column("legacy_commitment_lineage_id", _legacy_sa.BigInteger, unique=True),
    _legacy_sa.Column("history_batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id"), nullable=False),
    _legacy_sa.CheckConstraint("num_nonnulls(legacy_dependency_id,legacy_commitment_lineage_id)=1"),
)

_coordination_history_activations = _legacy_sa.Table(
    "coordination_history_activations", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_id", _legacy_pg.UUID(as_uuid=True), _legacy_sa.ForeignKey("coordination_record_subjects.id"), nullable=False),
    _legacy_sa.Column("history_batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id"), nullable=False),
    _legacy_sa.Column("recorded_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("clock_timestamp()")),
    _legacy_sa.UniqueConstraint("subject_id", "history_batch_id"),
)

_coordination_record_decisions = _legacy_sa.Table(
    "coordination_record_decisions", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_id", _legacy_pg.UUID(as_uuid=True), _legacy_sa.ForeignKey("coordination_record_subjects.id"), nullable=False),
    _legacy_sa.Column("revision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("project_record_revisions.id"), nullable=False),
    _legacy_sa.Column("field", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("decision_type", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("value_text", _legacy_sa.Text),
    _legacy_sa.Column("action_due_date", _legacy_sa.Date),
    _legacy_sa.Column("action_due_date_reason", _legacy_sa.Text),
    _legacy_sa.Column("milestone_ids", _legacy_pg.ARRAY(_legacy_sa.BigInteger), nullable=False, server_default=_legacy_sa.text("'{}'")),
    _legacy_sa.Column("deferral_reason", _legacy_sa.Text),
    _legacy_sa.Column("deferral_return_date", _legacy_sa.Date),
    _legacy_sa.Column("no_follow_up_reason", _legacy_sa.Text),
    _legacy_sa.Column("cancellation_reason", _legacy_sa.Text),
    _legacy_sa.Column("note", _legacy_sa.Text),
    _legacy_sa.Column("recorded_by", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("recorded_at", _legacy_sa.DateTime(timezone=True), nullable=False),
    _legacy_sa.Column("predecessor_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("coordination_record_decisions.id"), unique=True),
    _legacy_sa.UniqueConstraint("project_id", "id"),
    _legacy_sa.ForeignKeyConstraint(["project_id", "subject_id"], ["coordination_record_subjects.project_id", "coordination_record_subjects.id"]),
    _legacy_sa.CheckConstraint("field in ('internal_owner','next_action','milestone_impact','deferral')"),
    _legacy_sa.CheckConstraint("decision_type in ('assign_internal_owner','set_next_action','complete_next_action','cancel_next_action','set_milestone_impact','defer_work','resume_work','undo_follow_up_plan')"),
    _legacy_sa.CheckConstraint("valid_coordination_history_actor(recorded_by)"),
)
_legacy_sa.Index("uq_coordination_record_root", _coordination_record_decisions.c.subject_id,
                 _coordination_record_decisions.c.field, unique=True,
                 postgresql_where=_legacy_sa.text("predecessor_id is null"))

_coordination_decision_lineage = _legacy_sa.Table(
    "coordination_decision_lineage", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("decision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("coordination_record_decisions.id"), nullable=False, unique=True),
    _legacy_sa.Column("legacy_work_decision_id", _legacy_sa.BigInteger, nullable=False, unique=True),
    _legacy_sa.Column("history_batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id")),
    _legacy_sa.Column("original_content_sha256", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("observation_lineage", _legacy_pg.JSONB, nullable=False, server_default=_legacy_sa.text("'{}'")),
    _legacy_sa.CheckConstraint("original_content_sha256 ~ '^[0-9a-f]{64}$'"),
)

_coordination_record_reversals = _legacy_sa.Table(
    "coordination_record_reversals", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("decision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("coordination_record_decisions.id"), nullable=False, unique=True),
    _legacy_sa.Column("revision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("project_record_revisions.id"), nullable=False),
    _legacy_sa.Column("recorded_by", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("recorded_at", _legacy_sa.DateTime(timezone=True), nullable=False),
    _legacy_sa.CheckConstraint("valid_coordination_history_actor(recorded_by)"),
)

_coordination_reversal_lineage = _legacy_sa.Table(
    "coordination_reversal_lineage", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("reversal_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("coordination_record_reversals.id"), nullable=False, unique=True),
    _legacy_sa.Column("legacy_statement_reversal_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.UniqueConstraint("legacy_statement_reversal_id", "reversal_id"),
)


_support_scope_lineage = _legacy_sa.Table(
    "support_scope_lineage", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_id", _legacy_pg.UUID(as_uuid=True), _legacy_sa.ForeignKey("coordination_record_subjects.id"), nullable=False),
    _legacy_sa.Column("legacy_dependency_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.Column("field_name", _legacy_sa.Text),
    _legacy_sa.Column("fact_subject_key", _legacy_sa.Text, nullable=False),
    _legacy_sa.UniqueConstraint("project_id", "legacy_dependency_id", "field_name", postgresql_nulls_not_distinct=True),
    _legacy_sa.UniqueConstraint("project_id", "fact_subject_key"),
)

_support_history_receipts = _legacy_sa.Table(
    "support_history_receipts", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id")),
    _legacy_sa.Column("scope_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("support_scope_lineage.id"), nullable=False),
    _legacy_sa.Column("legacy_support_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.Column("legacy_evidence_link_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.Column("original_scope_sha256", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("fact_decision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("fact_decisions.id")),
    _legacy_sa.Column("source_segment_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("source_segments.id")),
    _legacy_sa.Column("outcome", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("reason", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("original_actor", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("original_time", _legacy_sa.DateTime(timezone=True), nullable=False),
    _legacy_sa.Column("policy_run_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("policy_runs.id")),
    _legacy_sa.Column("policy_approval_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("policy_approvals.id")),
    _legacy_sa.Column("recorded_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("clock_timestamp()")),
    _legacy_sa.Column("predecessor_receipt_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("support_history_receipts.id")),
    _legacy_sa.UniqueConstraint("scope_id", "batch_id", "original_scope_sha256", "predecessor_receipt_id", "outcome", postgresql_nulls_not_distinct=True),
    _legacy_sa.CheckConstraint("original_scope_sha256 ~ '^[0-9a-f]{64}$'"),
    _legacy_sa.CheckConstraint("outcome in ('native','retained_compatibility')"),
)
