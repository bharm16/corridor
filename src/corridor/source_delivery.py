"""The one delivery ledger both transports write, and the cursor it carries.

ADR-0083 declared the ``SourceEnvelope`` the single normalized ingress record
every channel produces.  The implementation did not come out that way: #511
persisted the push half (``push_deliveries``, "ADR-0083's SourceEnvelope,
persisted"), and #496 built the pull half with no delivery record at all —
``sync_pull_connector`` produced envelopes and handed them on, and #488 kept
only the checkpoint token, on the completed Due Work receipt of the occurrence
that reached it.  ADR-0089 calls that an accidental implementation asymmetry
rather than a domain distinction and puts both transports here.

Three rules shape this module.

**Identity is the database's.** ``record_delivery`` inserts and lets
``uq_source_deliveries_observation`` decide whether the row is new; it never
reads rows, decides they do not match, and then writes.  #457 closed after
finding two constraints that existed and proved nothing — a nullable digest
behind a partial index, and a key computed in Python and never checked against
its row — so the derivation here is checked by a trigger that re-derives both
ADR-0083 digests from the row's own columns.

**The checkpoint rule turns on the disposition.** ``stored`` and ``duplicate``
permit an advance past the delivery, exactly as ADR-0083 decided.
``terminally_refused`` and ``quarantined`` permit one only because the digest
and the refusal evidence are durably recorded here — that evidence is what
makes advancing past bytes nobody will ever store safe.  ``transient_failure``
never permits one: a scanner that timed out, an object store that rejected a
write, or a provider that returned a 500 has said nothing about the delivery,
and advancing past it drops a source revision silently.  The rule is enforced
where the coverage is written, by a trigger on
``connector_checkpoint_advance_deliveries``, so it is not a rule a caller has
to remember.

**The envelope names the project the way the identity does.**  A consumer needs
a project id, and the envelope carries a slug, so the question of adding the id
to ``SourceEnvelope`` is a fair one.  It is not added: the identity ADR-0083
fixed is derived from the customer, the *slug*, the channel and the external
version, and the database re-derives it from the stored row, so an id in the
envelope would be a second reference to the project that could disagree with
the row the identity was checked against.  Every consumer already holds the row
— ``require_stored_envelope`` hands it back — so ``project_id`` and the binding
are read from there, through ``binding_of_delivery``, rather than rebuilt.

**The cursor lives with the configuration, not with a receipt.**
``connector_polling``'s docstring recorded a rejected alternative — "a
checkpoint table was rejected: the migration window is closed" — and ADR-0089
reverses it, because retention is not identity: deleting an old Due Work
receipt would otherwise reset a live connector's external cursor, or land it
on a stale token.  The current checkpoint is derived from the newest recorded
advance and stored nowhere.

Alongside the ledger sits one relation for the act the ledger deliberately
does not record: ``confirm_delivery`` is a person admitting a stored delivery
to processing (#823).  ``stored`` says Corridor holds the exact bytes, which
it does from the moment they arrive and whether or not anybody has decided
they should be read; a product upload sits between the two for as long as its
preview is on screen, and one that is abandoned stays there rather than
disappearing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from corridor.connectors.pull_connector import (
    SourceEnvelope,
    build_delivery_identity,
    build_idempotency_key,
)
from corridor.models import (
    ConnectorCheckpointAdvance,
    ConnectorCheckpointAdvanceDelivery,
    SourceDelivery,
    SourceDeliveryConfirmation,
    Project,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.analytics import emit_event, source_arrival_event
from corridor.measurement_collection import binding_for_source

# ADR-0089's five dispositions.  ``stored`` and ``duplicate`` are the two
# outcomes in which Corridor holds the exact bytes; the other three are the
# outcomes it does not, and they differ in whether the delivery itself, or one
# attempt at it, is what failed.
DISPOSITION_STORED = "stored"
DISPOSITION_DUPLICATE = "duplicate"
DISPOSITION_QUARANTINED = "quarantined"
DISPOSITION_TERMINALLY_REFUSED = "terminally_refused"
DISPOSITION_TRANSIENT_FAILURE = "transient_failure"
DISPOSITIONS = (
    DISPOSITION_STORED,
    DISPOSITION_DUPLICATE,
    DISPOSITION_QUARANTINED,
    DISPOSITION_TERMINALLY_REFUSED,
    DISPOSITION_TRANSIENT_FAILURE,
)
# ``quarantined`` is recorded by no intake path today, and that is the honest
# state rather than a gap: ADR-0089 defines it as the gate having *held* the
# bytes with a quarantine reference saying where they are, and every channel
# gates before it stores anything, so a refused delivery is
# ``terminally_refused`` with its exact digest and reason and no bytes to point
# at.  A path that starts holding refused bytes records this disposition; one
# that merely refuses them must not, because the reference would name nothing.
# Which dispositions a checkpoint may advance past is asked once, by
# ``enforce_checkpoint_advance_coverage`` on
# ``connector_checkpoint_advance_deliveries``: a transient failure never, and a
# refused or quarantined delivery only on its recorded evidence.  A Python
# tuple naming the same set was defined here and read by nothing, which is the
# only reason it never disagreed with the trigger.


class SourceDeliveryRefused(ValueError):
    """A delivery or an advance cannot be recorded as described."""


@dataclass(frozen=True, slots=True)
class DeliveryBinding:
    """Who a delivery belongs to, and which configuration carried it.

    The same fields for both transports, which is the point: a pull connector
    reads the location's own item id and version and a pushed delivery gets its
    identity from the authenticated transport, and after that they are one
    family (ADR-0089).

    A push says which of the two things authenticated it (#823).  A machine
    push names the credential the transport presented; a product upload names
    the signed-in person who handed the bytes over, because nothing else
    authenticated that delivery and minting a push secret for a person would
    open a real door into the project to record one.
    """

    customer: str
    project_id: int
    project_slug: str
    transport: str
    channel: str
    configuration_identity: str
    configuration_version: str = ""
    credential_id: int | None = None
    delivered_by_principal: str = ""

    def __post_init__(self) -> None:
        if self.transport not in ("pull", "push"):
            raise SourceDeliveryRefused("a delivery is carried by pull or push")
        principal = str(self.delivered_by_principal or "").strip()
        if self.transport == "pull":
            if self.credential_id is not None or principal:
                raise SourceDeliveryRefused(
                    "a pulled delivery is bound by its connector configuration "
                    "and authenticates neither way"
                )
        elif (self.credential_id is not None) == bool(principal):
            raise SourceDeliveryRefused(
                "a pushed delivery names the one thing that authenticated it: "
                "a machine credential, or the person who handed it over"
            )
        if not str(self.configuration_identity).strip():
            raise SourceDeliveryRefused(
                "a delivery names the connector or channel configuration it "
                "arrived under"
            )


@dataclass(frozen=True, slots=True)
class DeliveryObservation:
    """What the external system offered, and what became of the bytes."""

    external_identity: str
    external_version: str
    content_digest: str
    bytes_reference: str = ""
    original_timestamps: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecordedDelivery:
    """One ledger row, whether this call wrote it or converged on it."""

    delivery_id: int
    disposition: str
    delivery_identity: str
    idempotency_key: str
    content_digest: str
    bytes_reference: str
    created: bool


def delivery_identity_for(
    binding: DeliveryBinding, observation: DeliveryObservation
) -> tuple[str, str]:
    """``(delivery_identity, idempotency_key)`` for one delivery.

    The same derivation for both transports, and the same one the database
    re-derives from the stored row, so a writer that computes it differently is
    refused rather than trusted.
    """

    identity = build_delivery_identity(
        customer=binding.customer,
        project=binding.project_slug,
        channel=binding.channel,
        external_identity=observation.external_identity,
        external_version=observation.external_version,
    )
    return identity, build_idempotency_key(identity, observation.content_digest)


def record_delivery(
    session: Session,
    binding: DeliveryBinding,
    observation: DeliveryObservation,
    *,
    disposition: str,
    service_identity: str,
    run_identity: str,
    refusal_reason: str | None = None,
) -> RecordedDelivery:
    """Record one outcome of one delivery, converging if it is already recorded.

    The insert carries ``on conflict do nothing`` over the identity constraint,
    so a replay and a competing writer converge on the row the ledger already
    holds rather than racing between a read and an insert.
    """

    from corridor.activation_runtime import require_source_delivery
    require_source_delivery(session, binding)
    if disposition not in DISPOSITIONS:
        raise SourceDeliveryRefused(f"{disposition!r} is not a delivery disposition")
    taken = disposition in (DISPOSITION_STORED, DISPOSITION_DUPLICATE)
    if taken and refusal_reason is not None:
        raise SourceDeliveryRefused("a taken delivery has no refusal reason")
    if not taken and not (refusal_reason or "").strip():
        raise SourceDeliveryRefused(
            "a refused, quarantined, or failed delivery records why"
        )
    identity, idempotency_key = delivery_identity_for(binding, observation)
    values = {
        "credential_id": binding.credential_id,
        "delivered_by_principal": binding.delivered_by_principal or None,
        "customer": binding.customer,
        "project_id": binding.project_id,
        "transport": binding.transport,
        "channel": binding.channel,
        "configuration_identity": binding.configuration_identity,
        "configuration_version": binding.configuration_version,
        "external_identity": observation.external_identity,
        "external_version": observation.external_version,
        "original_timestamps_json": dict(observation.original_timestamps),
        "content_sha256": observation.content_digest,
        "bytes_reference": observation.bytes_reference,
        "metadata_json": dict(observation.metadata),
        "delivery_identity": identity,
        "idempotency_key": idempotency_key,
        "service_identity": service_identity,
        "run_identity": run_identity,
        "disposition": disposition,
        "refusal_reason": refusal_reason,
    }
    inserted = session.execute(
        pg_insert(SourceDelivery)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_source_deliveries_observation")
        .returning(SourceDelivery.id, SourceDelivery.received_at)
    ).one_or_none()
    if inserted is not None:
        _emit_delivery_arrival(session, binding, observation, inserted.id, inserted.received_at,
                               disposition=disposition, outcome="recorded")
        return RecordedDelivery(
            delivery_id=int(inserted.id),
            disposition=disposition,
            delivery_identity=identity,
            idempotency_key=idempotency_key,
            content_digest=observation.content_digest,
            bytes_reference=observation.bytes_reference,
            created=True,
        )
    existing = session.execute(
        select(SourceDelivery.id, SourceDelivery.bytes_reference).where(
            SourceDelivery.project_id == binding.project_id,
            SourceDelivery.delivery_identity == identity,
            SourceDelivery.content_sha256 == observation.content_digest,
            SourceDelivery.disposition == disposition,
        )
    ).one()
    _emit_delivery_arrival(session, binding, observation, existing.id, datetime.now(timezone.utc),
                           disposition=disposition, outcome="replayed")
    return RecordedDelivery(
        delivery_id=int(existing.id),
        disposition=disposition,
        delivery_identity=identity,
        idempotency_key=idempotency_key,
        content_digest=observation.content_digest,
        bytes_reference=existing.bytes_reference,
        created=False,
    )


def _emit_delivery_arrival(session, binding, observation, delivery_id, at, *, disposition, outcome):
    """A newly recorded arrival names its native ID/time; retry time is separate."""
    size = observation.metadata.get("byte_count")
    filename = observation.metadata.get("filename")
    emit_event(source_arrival_event(
        binding_for_source(session, binding), customer_id=binding.customer, project_id=binding.project_id,
        channel=binding.channel, filename=filename if isinstance(filename, str) else "",
        content_sha256=observation.content_digest, byte_count=size if type(size) is int and size >= 0 else None,
        source_delivery_id=int(delivery_id), occurred_at=at, outcome=outcome, disposition=disposition,
    ))


def take_delivery(
    session: Session,
    binding: DeliveryBinding,
    observation: DeliveryObservation,
    *,
    service_identity: str,
    run_identity: str,
) -> RecordedDelivery:
    """Record a delivery whose exact bytes are durably stored.

    A first taking is ``stored``.  A later one of the same external version
    with the same bytes wrote no new object, so it is ``duplicate`` — its own
    outcome of the same delivery, and bounded, because a second re-delivery
    converges on the row the first one wrote.
    """

    stored = record_delivery(
        session,
        binding,
        observation,
        disposition=DISPOSITION_STORED,
        service_identity=service_identity,
        run_identity=run_identity,
    )
    if stored.created:
        return stored
    return record_delivery(
        session,
        binding,
        observation,
        disposition=DISPOSITION_DUPLICATE,
        service_identity=service_identity,
        run_identity=run_identity,
    )


def stored_delivery(
    session: Session, *, idempotency_key: str
) -> SourceDelivery | None:
    """The ledger row of a delivery already taken, or ``None``.

    Scoped to ``stored``: a delivery the intake gate refused carries the same
    idempotency key, and answering "this was already taken" from a refusal row
    would hand a caller a receipt for bytes nobody holds.
    """

    return session.scalars(
        select(SourceDelivery).where(
            SourceDelivery.idempotency_key == idempotency_key,
            SourceDelivery.disposition == DISPOSITION_STORED,
        )
    ).first()


def confirm_delivery(
    session: Session, *, delivery: SourceDelivery, principal: HumanPrincipal
) -> SourceDeliveryConfirmation:
    """Record one person's admission of one stored delivery to processing.

    Taking delivery and admitting it are two acts by two parties, and only the
    first is what ``stored`` means: Corridor holds the exact bytes from the
    moment they arrive, whether or not anybody has decided they should be read.
    A product upload lives in the gap between the two for as long as the person
    is looking at the preview, and an upload they abandon stays there.

    Idempotent by delivery, through the same constraint the ledger itself uses
    rather than a read-then-write: a retried confirmation converges on the act
    already recorded, so one admission is never attributed twice or to two
    people.
    """

    principal = require_human_principal(principal)
    if delivery.disposition != DISPOSITION_STORED:
        raise SourceDeliveryRefused(
            "a delivery is confirmed for processing only where its exact bytes "
            "are stored"
        )
    session.execute(
        pg_insert(SourceDeliveryConfirmation)
        .values(
            delivery_id=int(delivery.id),
            project_id=int(delivery.project_id),
            confirmed_by_principal=principal.subject,
        )
        .on_conflict_do_nothing(
            constraint="uq_source_delivery_confirmation_delivery"
        )
    )
    return session.scalars(
        select(SourceDeliveryConfirmation).where(
            SourceDeliveryConfirmation.delivery_id == int(delivery.id)
        )
    ).one()


def delivery_confirmations(
    session: Session, project_id: int
) -> dict[int, SourceDeliveryConfirmation]:
    """One project's admissions, by the delivery each one admitted.

    The register (#841) shows who admitted a delivery and when, not only that
    somebody did, and one relation read one way is what keeps the two readings
    from disagreeing about which deliveries are confirmed.
    """

    return {
        int(row.delivery_id): row
        for row in session.scalars(
            select(SourceDeliveryConfirmation).where(
                SourceDeliveryConfirmation.project_id == project_id
            )
        ).all()
    }


def confirmed_delivery_ids(session: Session, project_id: int) -> frozenset[int]:
    """Which of one project's deliveries a person has admitted to processing."""

    return frozenset(delivery_confirmations(session, project_id))


def binding_of_delivery(session: Session, row: SourceDelivery) -> DeliveryBinding:
    """The binding one retained delivery arrived under, read back from its row.

    A consumer used to rebuild this from the row's own columns beside a
    hard-coded transport, which is the same "two definitions of one identity"
    ADR-0089 removed from the envelope: the row already says which transport,
    channel and configuration carried the delivery, so it says so once here.
    """

    return DeliveryBinding(
        customer=row.customer,
        project_id=row.project_id,
        project_slug=session.get_one(Project, row.project_id).slug,
        transport=row.transport,
        channel=row.channel,
        configuration_identity=row.configuration_identity,
        configuration_version=row.configuration_version or "",
        credential_id=row.credential_id,
        delivered_by_principal=row.delivered_by_principal or "",
    )


def envelope_for_delivery(session: Session, delivery_id: int) -> SourceEnvelope:
    """Read the one retained ingress envelope for any source-specific consumer.

    The only construction of an ingress envelope from a delivery there is.  It
    was three: this reader, one filled from whatever objects a writer happened
    to hold, and a third inside ``push_intake`` for its own transport.  They had
    to agree exactly rather than approximately, because
    ``require_stored_envelope`` admits a consumer's envelope by comparing it
    with this one for equality — a second construction that formatted one field
    differently would not read differently, it would refuse the delivery.
    """
    row = session.get_one(SourceDelivery, delivery_id)
    project = session.get_one(Project, row.project_id)
    return SourceEnvelope(
        customer=row.customer, project=project.slug, channel=row.channel,
        external_identity=row.external_identity, external_version=row.external_version,
        original_timestamps=dict(row.original_timestamps_json or {}),
        content_digest=row.content_sha256, bytes_reference=row.bytes_reference,
        metadata=dict(row.metadata_json or {}), delivery_identity=row.delivery_identity,
        idempotency_key=row.idempotency_key,
    )


def require_stored_envelope(session: Session, envelope: SourceEnvelope) -> SourceDelivery:
    """A consumer cannot change a retained delivery's customer, project or bytes."""
    row = stored_delivery(session, idempotency_key=envelope.idempotency_key)
    if row is None or envelope_for_delivery(session, row.id) != envelope:
        raise SourceDeliveryRefused("source requires its exact stored customer/project envelope")
    from corridor.activation_runtime import require_source_project
    require_source_project(session, row.project_id)
    return row


def record_checkpoint_advance(
    session: Session,
    *,
    project_id: int,
    schedule_id: int,
    configuration_identity: str,
    configuration_version: str,
    channel: str,
    checkpoint_token: str,
    service_identity: str,
    run_identity: str,
    delivery_ids: Sequence[int] = (),
) -> ConnectorCheckpointAdvance:
    """Record that one connector configuration's cursor reached a token.

    The deliveries the advance covered are recorded with it, and the database
    refuses coverage of a delivery that does not permit an advance, so an
    advance past a transient failure is not a mistake a caller can make.
    """

    if not (checkpoint_token or "").strip():
        raise SourceDeliveryRefused("an advance names the token it reached")
    advance = ConnectorCheckpointAdvance(
        project_id=project_id,
        schedule_id=schedule_id,
        configuration_identity=configuration_identity,
        configuration_version=configuration_version,
        channel=channel,
        checkpoint_token=checkpoint_token,
        service_identity=service_identity,
        run_identity=run_identity,
    )
    session.add(advance)
    session.flush()
    for delivery_id in dict.fromkeys(delivery_ids):
        session.add(
            ConnectorCheckpointAdvanceDelivery(
                advance_id=advance.id, delivery_id=delivery_id
            )
        )
    session.flush()
    return advance


def current_checkpoint_token(session: Session, schedule_id: int) -> str | None:
    """The token this connector configuration's newest advance reached.

    Derived, never stored: the cursor is the last advance recorded for the
    configuration, so deleting an old Due Work receipt cannot move it.
    """

    token = session.scalars(
        select(ConnectorCheckpointAdvance.checkpoint_token)
        .where(ConnectorCheckpointAdvance.schedule_id == schedule_id)
        .order_by(ConnectorCheckpointAdvance.id.desc())
        .limit(1)
    ).first()
    return str(token) if token else None
