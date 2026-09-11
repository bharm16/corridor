"""Ingress: what arrived, from which connector, and what it was routed to.

One normalized delivery record per arrival (ADR-0075's SourceEnvelope), the
inbound mail thread it came in, the connector checkpoint that advanced past it,
and the references a delivery pointed at that were fetched later. Earlier
designs recorded a document directly at intake, with no record of the delivery;
that lost the arrival when the document turned out to be unreadable, so the
delivery and the document are separate rows and the document is derived from
the delivery.
"""

from datetime import datetime
from hashlib import sha256

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
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
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base, _enum


__all__ = [
    "ConnectorCheckpointAdvance",
    "ConnectorCheckpointAdvanceDelivery",
    "DISCOVERED_REFERENCE_STATES",
    "DiscoveredReference",
    "InboundMessage",
    "InboundRouteTriage",
    "InboundThread",
    "InboundThreadReading",
    "PushIntakeCredential",
    "SOURCE_FETCH_OUTCOMES",
    "SourceDelivery",
    "SourceDeliveryConfirmation",
    "SourceFetchAttempt",
    "SourceRevisionDeclaration",
]


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

    A push is not only a machine's (#823).  Somebody handing Corridor a file
    through the product is handing it bytes it never asked for, which is what
    push means, and the person's authenticated session is what admitted it.
    The row therefore records *how* the transport authenticated — a credential
    or a principal — rather than assuming a credential exists.
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
        # How the transport authenticated, stated once and checked here (#823).
        # A pushed delivery is admitted by exactly one of the two things that
        # can authenticate a push: a machine credential, or the signed-in
        # person who handed the bytes over.  A pull authenticates neither way,
        # because the connector configuration is the whole binding.  The
        # predecessor required a credential outright, which is why a product
        # upload could not be recorded here at all without either minting a
        # live push secret for a person or calling their upload a pull.
        CheckConstraint(
            "case transport"
            " when 'pull' then credential_id is null"
            "                and delivered_by_principal is null"
            " when 'push' then (credential_id is not null)"
            "                <> (delivered_by_principal is not null)"
            " end",
            name="ck_source_delivery_authentication",
        ),
        CheckConstraint(
            "delivered_by_principal is null "
            "or length(btrim(delivered_by_principal)) > 0",
            name="ck_source_delivery_principal",
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
    # The person whose authenticated session carried a pushed delivery, where
    # one did (#823).  A machine push leaves it null and names its credential
    # instead; the two are the transport's two authentication modes and the
    # check constraint above admits exactly one of them.
    delivered_by_principal: Mapped[str | None] = mapped_column(Text)
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


class SourceDeliveryConfirmation(Base):
    """One person's admission of one delivery to processing (#823).

    Storing the bytes and admitting them are two different acts by two
    different parties, and the ledger row above records only the first: a
    delivery is ``stored`` the moment Corridor holds the exact bytes, whether
    or not anybody has decided they should be read.  For a product upload the
    gap between the two is the whole staged-upload screen, and before this
    relation existed the only trace of the second act was an audit entry about
    the *Document* it produced — so an upload somebody staged and walked away
    from was indistinguishable from one that failed to register.

    One row per delivery, so replaying a confirmation converges on the act
    already recorded rather than attributing the same admission twice.  It is
    append-only for the same reason every other receipt here is: a later
    correction is a separate act against the Document, never an edit of who
    admitted what and when.
    """

    __tablename__ = "source_delivery_confirmations"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id", name="uq_source_delivery_confirmation_delivery"
        ),
        # The delivery is named with its project, so a confirmation recorded in
        # one project can never name another customer's delivery (#675).
        ForeignKeyConstraint(
            ["delivery_id", "project_id"],
            ["source_deliveries.id", "source_deliveries.project_id"],
            name="fk_source_delivery_confirmation_delivery",
        ),
        CheckConstraint(
            "length(btrim(confirmed_by_principal)) > 0",
            name="ck_source_delivery_confirmation_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    confirmed_by_principal: Mapped[str] = mapped_column(Text)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceRevisionDeclaration(Base):
    """What a coordinator declared about one delivery that its bytes cannot say (#825).

    A workbook does not state which registered source family it belongs to,
    which Document Revision it is, whether it enumerates the customer's whole
    population or a filtered slice of it, whether it replaces, supplements or
    is another rendition of a revision already delivered, or whether it was
    produced under the field mapping this project registered.  Those five
    facts decided whether an absent row may be proposed as an apparent removal
    (ADR-0076) and whether a reading is a second logical revision at all
    (ADR-0069), and until this relation existed they were two Boolean keyword
    arguments a developer set at a call site.

    One row per delivery, because the declaration is about *what arrived*, not
    about the Document a confirmation went on to register: a delivery may be
    refused registration and still have been declared, and the same bytes may
    arrive twice under two different declared revisions.  Append-only for the
    reason every other receipt here is: a coordinator correcting a declaration
    is making a new attributable act against the delivery that carries the
    correction, never editing what was declared before.

    ``answer_sources_json`` is how each answer got here -- ``registration`` for
    one read back from what the project registered, ``source_metadata`` for one
    the transport's own external version supplied, ``declared`` for one the
    person answered.  It is retained rather than derived because the rule that
    produced a default can change, and a receipt that cannot say where an
    answer came from cannot show that completeness was never inferred.
    """

    __tablename__ = "source_revision_declarations"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id", name="uq_source_revision_declaration_delivery"
        ),
        # The delivery is named with its project, so a declaration recorded in
        # one project can never name another customer's delivery (#675).
        ForeignKeyConstraint(
            ["delivery_id", "project_id"],
            ["source_deliveries.id", "source_deliveries.project_id"],
            name="fk_source_revision_declaration_delivery",
        ),
        CheckConstraint(
            "length(btrim(declared_by_principal)) > 0",
            name="ck_source_revision_declaration_principal",
        ),
        CheckConstraint(
            "length(btrim(source_family)) > 0",
            name="ck_source_revision_declaration_family",
        ),
        CheckConstraint(
            "length(btrim(revision_identity)) > 0",
            name="ck_source_revision_declaration_revision",
        ),
        CheckConstraint(
            "completeness in ('complete_enumeration', 'partial_export')",
            name="ck_source_revision_declaration_completeness",
        ),
        CheckConstraint(
            "revision_relationship in "
            "('replaces', 'supplements', 'additional_rendition')",
            name="ck_source_revision_declaration_relationship",
        ),
        # A rendition is a rendition *of* a Document Revision (ADR-0069), so a
        # declaration that says "another rendition" and names no revision says
        # nothing at all.
        CheckConstraint(
            "revision_relationship <> 'additional_rendition' "
            "or length(btrim(coalesce(related_revision_identity, ''))) > 0",
            name="ck_source_revision_declaration_rendition_names_its_revision",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_family: Mapped[str] = mapped_column(Text)
    revision_identity: Mapped[str] = mapped_column(Text)
    completeness: Mapped[str] = mapped_column(String(32))
    revision_relationship: Mapped[str] = mapped_column(String(32))
    related_revision_identity: Mapped[str | None] = mapped_column(Text)
    uses_registered_mapping: Mapped[bool] = mapped_column(Boolean)
    answer_sources_json: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    declared_by_principal: Mapped[str] = mapped_column(Text)
    declared_at: Mapped[datetime] = mapped_column(
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
