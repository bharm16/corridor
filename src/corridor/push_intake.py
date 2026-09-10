"""Bind a pushed delivery to one customer and project before anything parses it.

ADR-0058 gave each project its own inbound address.  ADR-0059 replaced that
with one global address and read the project out of the message: attachment
hashes, registry ids in filenames, identifiers in the body, the sender.
``email_intake`` still implements exactly that, and it is the defect this
module exists to remove.  Deciding which customer a delivery belongs to *after*
receiving its bytes means the payload chooses its own boundary, and every one
of those tiers reads attacker-controlled text.  ADR-0078 rejected the ordering
and ADR-0083 named the shape that replaces it: three intake layers, of which
``PullConnector`` (#496) is the half that fetches and this is the half that is
handed bytes it never asked for.

A push channel has exactly one trustworthy fact about a delivery: the
credential the authenticated transport presented.  An alias token or a webhook
secret resolves, through ``push_intake_credentials``, to one customer and one
project — and only then may a parser run, inside a ``PushBinding`` no parser
can construct (``tests/test_architecture.py`` holds the construction site to
this module).  Headers, thread identifiers, filenames, MIME structure, and
every address inside the body are data.  Content-based inference survives only
where ADR-0078 left it: choosing among rows already inside the bound project,
or leaving that choice visible as bounded triage.

The envelope is #496's ``SourceEnvelope``, not a second one — that is the point
of "shared after ingress".  What differs is where its identity comes from.  A
pull connector reads the location's own item id and version; a pushed delivery
has only what the transport supplied, so the transport's delivery identifier
names it, the content digest names it when the transport has none, and the
digest is always its version.  Neither is ever read out of the payload, which
is why a crafted message cannot collide with, or replay over, a real one.

What was considered and rejected: extending ``PullConnector`` with a fifth
method for pushed deliveries.  The four methods are a cursor protocol —
``list_changes``/``checkpoint`` only mean something to a caller that decides
when to fetch — and a push channel decides nothing.  ADR-0083 separates the two
contracts for that reason, and they meet only at the envelope.

They now also meet in one persisted family.  ``push_deliveries`` was the
envelope persisted for push alone, and the pull half had no record at all;
ADR-0089 calls that an accidental implementation asymmetry and makes both
transports write ``source_deliveries`` through ``corridor.source_delivery``.
This module keeps the part that is genuinely a push concern — the credential
that binds the boundary before anything parses — and hands the delivery itself
to the shared ledger.  That includes reading the delivery back: the envelope a
receipt carries is ``source_delivery.envelope_for_delivery``, the one reader
both transports share, not a push-shaped copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.connectors.pull_connector import SourceEnvelope
from corridor.intake_hardening import HostileContentRefused, inspect_byte_gate
from corridor.models import Project, PushIntakeCredential
from corridor.object_storage import content_key, store_bytes
from corridor.source_delivery import (
    DISPOSITION_DUPLICATE,
    DISPOSITION_TERMINALLY_REFUSED,
    DeliveryBinding,
    DeliveryObservation,
    delivery_identity_for,
    envelope_for_delivery,
    record_delivery,
    stored_delivery,
    take_delivery,
)

# Who took delivery, for the ledger ADR-0089 shares with the pull half.
SERVICE_IDENTITY = "corridor.push_intake"

# The push channels ADR-0078 lists that a credential may bind.  A channel is
# not a routing hint: it is part of the credential's identity, so a webhook
# secret cannot be presented as a mail alias.
MAIL_CHANNELS = frozenset({"project_alias", "shared_mailbox"})
CHANNELS = MAIL_CHANNELS | {"webhook"}


class PushIntakeRefused(ValueError):
    """A presented credential does not bind exactly one customer and project."""


class PushDeliveryRefused(PushIntakeRefused):
    """The intake gate refused a bound delivery, and the ledger recorded it.

    Carries the ledger identity so the caller can commit the refusal record
    before refusing the request: a refusal that rolls back with the response is
    exactly the loss ADR-0089 set out to remove.
    """

    def __init__(self, message: str, *, delivery_id: int) -> None:
        super().__init__(message)
        self.delivery_id = delivery_id


@dataclass(frozen=True, slots=True)
class PushCredential:
    """What the authenticated transport presents, before anything is parsed.

    ``material`` is the alias token the message was *delivered to* (the
    envelope recipient the MTA reports), or a webhook's shared secret.  It is
    never a header: ``To``, ``Cc``, and ``Delivered-To`` are written by whoever
    composed the message, so reading a boundary out of one would restore the
    defect this module removes.
    """

    channel: str
    material: str


@dataclass(frozen=True, slots=True)
class PushBinding:
    """One credential's proof of customer and project, established before parsing.

    Constructed only by ``bind_credential``.  Every function that parses a
    pushed payload requires one, so "the credential bound the boundary first"
    is a property of the call graph rather than a rule someone remembers.
    """

    customer: str
    project_id: int
    project_slug: str
    channel: str
    credential_id: int


@dataclass(frozen=True, slots=True)
class PushPayload:
    """The untrusted bytes of one delivery and the transport's own facts about it."""

    body: bytes
    filename: str
    transport_delivery_id: str | None = None
    original_timestamps: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PushReceipt:
    """One accepted delivery: its envelope, its ledger row, and its staged bytes."""

    envelope: SourceEnvelope
    delivery_id: int
    staged_path: Path
    replayed: bool


class PushIntake(Protocol):
    """The push half of ADR-0083's intake, deliberately not ``PullConnector``.

    ``bind`` resolves a presented credential to one customer and project and
    must run before any parser sees the payload.  ``accept`` persists the bytes
    through the storage interface and normalizes the delivery into the shared
    ``SourceEnvelope``, idempotently by delivery identity.  ``replay`` answers
    whether a delivery identity has already been taken, so a transport that
    retries after a crash learns the outcome instead of producing a second one.
    """

    def bind(self, credential: PushCredential) -> PushBinding:
        """Resolve a presented credential to its customer and project."""
        ...

    def accept(self, binding: PushBinding, payload: PushPayload) -> PushReceipt:
        """Persist and normalize one delivery inside an established boundary."""
        ...

    def replay(self, binding: PushBinding, payload: PushPayload) -> PushReceipt | None:
        """The receipt of an already-taken delivery, or ``None``."""
        ...


class DatabasePushIntake:
    """``PushIntake`` over one session's credential registry and delivery ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def bind(self, credential: PushCredential) -> PushBinding:
        return bind_credential(self._session, credential)

    def accept(self, binding: PushBinding, payload: PushPayload) -> PushReceipt:
        return accept_delivery(self._session, binding, payload)

    def replay(self, binding: PushBinding, payload: PushPayload) -> PushReceipt | None:
        return replay_delivery(self._session, binding, payload)


def credential_digest(channel: str, material: str) -> str:
    """The one-way digest a presented credential is recognized by.

    The channel is inside the digest so the same string presented on another
    channel is a different credential; a mail alias is not a webhook secret.
    """

    return sha256(f"{channel}\x00{material}".encode("utf-8")).hexdigest()


def normalize_credential_material(channel: str, material: str) -> str:
    """Normalize case and spacing for an alias; a secret is compared verbatim."""

    cleaned = " ".join((material or "").split())
    return cleaned.casefold() if channel in MAIL_CHANNELS else cleaned


def register_push_credential(
    session: Session,
    *,
    customer: str,
    project: Project,
    channel: str,
    material: str,
) -> PushIntakeCredential:
    """Declare that one credential belongs to one customer's one project.

    This is the whole boundary.  Registering it is an operations act performed
    before the transport is pointed at Corridor, which is what makes the
    binding available *before* the first byte of the first delivery.
    """

    channel = (channel or "").strip().casefold()
    if channel not in CHANNELS:
        raise PushIntakeRefused("a push credential names one supported channel")
    customer = " ".join((customer or "").split())
    if not customer:
        raise PushIntakeRefused("a push credential names its customer")
    normalized = normalize_credential_material(channel, material)
    if not normalized:
        raise PushIntakeRefused("a push credential needs its material")
    digest = credential_digest(channel, normalized)
    existing = session.scalars(
        select(PushIntakeCredential).where(
            PushIntakeCredential.credential_sha256 == digest
        )
    ).first()
    if existing is not None:
        # Re-pointing a live alias at another project is exactly the move this
        # module exists to prevent, so registration never overwrites: revoke
        # the credential and issue a new one.
        raise PushIntakeRefused("this push credential is already bound")
    record = PushIntakeCredential(
        customer=customer,
        project_id=project.id,
        channel=channel,
        credential_sha256=digest,
    )
    session.add(record)
    session.flush()
    return record


def revoke_push_credential(session: Session, *, credential_id: int) -> None:
    """Stop one credential binding anything; its past deliveries are untouched."""

    record = session.get(PushIntakeCredential, credential_id)
    if record is None:
        raise PushIntakeRefused("no such push credential")
    record.state = "revoked"
    session.flush()


def bind_credential(session: Session, credential: PushCredential) -> PushBinding:
    """Resolve a presented credential to its customer and project, or refuse.

    Unknown, revoked, and structurally invalid credentials share one refusal
    message: a caller probing aliases must not learn which of the three it hit,
    and least of all that some other customer holds the one it guessed.
    """

    channel = (credential.channel or "").strip().casefold()
    material = normalize_credential_material(channel, credential.material)
    if channel not in CHANNELS or not material:
        raise PushIntakeRefused("this delivery presents no bound push credential")
    digest = credential_digest(channel, material)
    record = session.scalars(
        select(PushIntakeCredential).where(
            PushIntakeCredential.credential_sha256 == digest,
            PushIntakeCredential.state == "active",
        )
    ).first()
    if record is None:
        raise PushIntakeRefused("this delivery presents no bound push credential")
    project = session.get(Project, record.project_id)
    if project is None:
        raise PushIntakeRefused("this delivery presents no bound push credential")
    return PushBinding(
        customer=record.customer,
        project_id=record.project_id,
        project_slug=project.slug,
        channel=record.channel,
        credential_id=record.id,
    )


def delivery_identity_of(binding: PushBinding, payload: PushPayload) -> tuple[str, str]:
    """``(delivery_identity, idempotency_key)`` for one pushed delivery.

    Built from the binding and the transport's own facts, sharing #496's
    derivation so a pull and a push delivery are the same kind of identity.
    The external identity is the transport's delivery id, or the content digest
    when the transport has none; the version is always the digest, so the same
    transport delivery re-sent with different bytes is a different version
    rather than a silent overwrite.

    It reads the same observation ``accept_delivery`` records, through the same
    ``delivery_identity_for`` the database re-derives from the stored row.  It
    used to compute the external identity itself while ``_observation``
    computed it again, which is silent while the two agree and, the day they
    stop, means a retry no longer finds the delivery it already took.
    """

    if not isinstance(binding, PushBinding):
        raise PushIntakeRefused("a pushed delivery needs an established binding")
    digest = sha256(payload.body).hexdigest()
    return delivery_identity_for(
        _delivery_binding(binding), _observation(payload, digest)
    )


def _delivery_binding(binding: PushBinding) -> DeliveryBinding:
    """One pushed delivery's place in ADR-0089's shared family.

    The channel credential *is* the configuration a pushed delivery arrived
    under, which is what the pull side names with a connector identity.
    """

    return DeliveryBinding(
        customer=binding.customer,
        project_id=binding.project_id,
        project_slug=binding.project_slug,
        transport="push",
        channel=binding.channel,
        configuration_identity=f"credential:{binding.credential_id}",
        credential_id=binding.credential_id,
    )


def _observation(
    payload: PushPayload, digest: str, bytes_reference: str = ""
) -> DeliveryObservation:
    """What the transport offered, and the one place its identity is derived."""

    return DeliveryObservation(
        external_identity=(payload.transport_delivery_id or "").strip() or digest,
        external_version=digest,
        content_digest=digest,
        bytes_reference=bytes_reference,
        original_timestamps=dict(payload.original_timestamps),
        metadata=dict(payload.metadata),
    )


def replay_delivery(
    session: Session, binding: PushBinding, payload: PushPayload
) -> PushReceipt | None:
    """The receipt of an already-taken delivery, without taking it again."""

    from corridor.activation_runtime import require_source_delivery
    require_source_delivery(session, _delivery_binding(binding))
    _delivery_identity, idempotency_key = delivery_identity_of(binding, payload)
    row = stored_delivery(session, idempotency_key=idempotency_key)
    if row is None:
        return None
    return PushReceipt(
        envelope=envelope_for_delivery(session, row.id),
        delivery_id=row.id,
        staged_path=_stage(payload, row.content_sha256),
        replayed=True,
    )


def accept_delivery(
    session: Session, binding: PushBinding, payload: PushPayload
) -> PushReceipt:
    """Persist one delivery's bytes and record its envelope inside the boundary.

    The byte gate of #490 runs on the untrusted bytes, the object is written
    through the storage interface before the row that references it, and the
    ledger row is keyed by delivery identity so a retry after a crash converges
    on the delivery already taken instead of producing a second one.

    A delivery the gate refuses is now *recorded* rather than lost (ADR-0089).
    The digest is taken first, so the refusal carries the exact identity of
    what arrived, and the refusal is raised as a ``PushDeliveryRefused``
    naming the ledger row, so the caller can commit the record it just made
    while still refusing the request.
    """

    if not isinstance(binding, PushBinding):
        raise PushIntakeRefused("a pushed delivery needs an established binding")
    digest = sha256(payload.body).hexdigest()
    delivery_binding = _delivery_binding(binding)
    from corridor.activation_runtime import require_source_delivery
    require_source_delivery(session, delivery_binding)
    # A push channel has no pass and no cursor: the run that took delivery is
    # this acceptance.  It is not part of the delivery's identity, so a replay
    # converges on the row the first run wrote and keeps that run's name.
    run_identity = f"push-intake:{uuid4().hex}"
    try:
        inspect_byte_gate(payload.body, payload.filename)
    except HostileContentRefused as exc:
        refused = record_delivery(
            session,
            delivery_binding,
            _observation(payload, digest),
            disposition=DISPOSITION_TERMINALLY_REFUSED,
            service_identity=SERVICE_IDENTITY,
            run_identity=run_identity,
            refusal_reason=f"{exc.rule}: {exc.reason}",
        )
        raise PushDeliveryRefused(
            f"this delivery was refused at intake [{exc.rule}]: {exc.reason}",
            delivery_id=refused.delivery_id,
        ) from exc

    # The object is written first, so a crash between the two leaves an
    # unreferenced object rather than a ledger row without its bytes.
    staged = _stage(payload, digest)
    observation = _observation(
        payload, digest, content_key(digest, _suffix(payload.filename))
    )
    recorded = take_delivery(
        session,
        delivery_binding,
        observation,
        service_identity=SERVICE_IDENTITY,
        run_identity=run_identity,
    )
    # The receipt always names the row the bytes were taken on, never the
    # duplicate observation of it: a registered message points at the taking,
    # and a retry must reach the same one.
    taken = stored_delivery(session, idempotency_key=recorded.idempotency_key)
    return PushReceipt(
        envelope=envelope_for_delivery(session, taken.id),
        delivery_id=taken.id,
        staged_path=staged,
        replayed=recorded.disposition == DISPOSITION_DUPLICATE,
    )


def _stage(payload: PushPayload, digest: str) -> Path:
    """Persist through the storage interface and return the locally staged path.

    Content-addressed, so a replay writes nothing new and still hands the
    caller a path to the exact bytes it was delivered.
    """

    return store_bytes(payload.body, sha256=digest, suffix=_suffix(payload.filename))


def _suffix(filename: str) -> str:
    """The bare extension of a delivered filename, or none.

    A filename is untrusted, so only a short alphanumeric extension survives;
    everything else stores without one rather than letting a name shape a key.
    """

    _head, separator, tail = (filename or "").rpartition(".")
    if not separator or not tail.isalnum() or not 1 <= len(tail) <= 8:
        return ""
    return f".{tail.lower()}"
