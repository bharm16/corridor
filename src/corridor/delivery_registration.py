"""Register the source one taken delivery carried, once, for every transport.

ADR-0089 put both transports in one delivery ledger and stopped there: a
delivery was *recorded*, and turning it into something the product can show —
a registered Document, or an inbound message with its thread — stayed
hand-written at every consumer.  ``email_intake.receive_pulled_message``
rebuilt a ``DeliveryBinding`` out of the row, ``project_contacts``,
``shadow_processing`` and the email extraction route each opened with their own
``require_stored_envelope``, and ``m365_replay`` held the only code that
actually registered what a pull connector delivered.  The consequence is the
defect this module removes: ``execute_connector_polling`` fetched, gated,
stored, recorded a delivery and advanced its checkpoint, and registered
nothing, so a production Box or TxDOT poll took delivery of sources that
appeared nowhere in the product.

Two things live here, and they are the two halves of "a delivery is persisted
once whatever transport" (ADR-0089).

**Registration reads the channel, never the bytes.**
``register_delivered_source`` takes the ingress envelope of a *stored* delivery
and produces the registration its channel kind calls for: a shared-mailbox
delivery becomes an inbound message with its thread, and every other channel
becomes a registered Document linked to the delivery that carried it.  The
channel is server-owned configuration — a connector declaration or a bound
credential — so the delivered payload never chooses which registration it gets,
which is the same rule ``push_intake`` keeps for the boundary itself.  Neither
branch is reimplemented here: the mail branch calls ``email_intake``'s bound
registration and the document branch calls ``ingest_document``, which is the
same seam ``source_intake.confirm_intake`` registers a human upload through.
What ``confirm_intake`` adds on top of it — an attributable person and the
fingerprint of the preview they saw — is exactly what a scheduled poll does not
have, so calling it would mean inventing a principal for a machine.

**The kind of source is not a fact the bytes carry.** ADR-0007 leaves it to a
person, which is why ``location_discovery`` will not fetch a reference before
someone authorizes its kind and why the replay command demands an explicit
mapping for every recorded library item.  A live poll has nobody to ask at
delivery time, and refusing the delivery would throw away a source Corridor
already holds.  So an undeclared source registers as ``other``: the bytes, the
digest and the delivery link are all retained, no extractor reads it, and
declaring its kind later is the ordinary registry act it always was.  A caller
that *does* hold a declaration passes one.

**One pass, two transaction shapes.** ``PassLedger`` is the ``DeliveryLedger``
both callers hand to ``sync_pull_connector``, and it also registers each stored
delivery.  The supervised pass writes each delivery in its own short committed
transaction and registers it in another one immediately after — so a
registration that fails can never un-record what arrived, and because the
cursor is recorded last, the next pass re-lists that same change and registers
it against the delivery already in the ledger.  The offline replay command runs
inside the caller's single transaction instead, because its contract is that a
failed replay publishes no cursor and leaves nothing behind at all.  That was
the one real difference between the two runtimes; everything else about them
was duplication.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.connectors.pull_connector import (
    ChangeItem,
    SourceEnvelope,
    delivered_metadata,
)
from corridor.models import DOC_TYPES
from corridor.object_storage import content_store, store_bytes
from corridor.source_delivery import (
    DISPOSITION_STORED,
    DeliveryBinding,
    DeliveryObservation,
    record_delivery,
    require_stored_envelope,
    take_delivery,
)

# The pull channels whose deliveries are messages rather than files.  A shared
# mailbox delivers raw MIME with a thread the headers establish, and
# registering that as an ordinary Document would lose the thread and the
# attachments; ``email_intake`` owns the registration that keeps both.
MESSAGE_CHANNELS = frozenset({"m365-shared-mailbox-v1"})

# What a source registers as while nobody has declared what kind it is.  It is
# a real registered Document with its exact bytes and its delivery link; it is
# not a claim about what the source says (ADR-0007).
UNDECLARED_SOURCE_KIND = "other"


class SourceRegistrationRefused(ValueError):
    """A stored delivery cannot be registered as the caller described it."""


@dataclass(frozen=True, slots=True)
class SourceDeclaration:
    """What a caller already knows that the delivered bytes cannot state.

    ``source_kind`` is the document kind a person declared for this source, and
    ``attachment_source_kinds`` maps a delivered message's attachment digests
    to the kinds declared for them.  A caller with neither passes neither.
    """

    source_kind: str | None = None
    attachment_source_kinds: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    """What one taken delivery registered as, whichever branch registered it."""

    delivery_id: int
    external_version: str
    document_id: int | None = None
    message_id: int | None = None
    thread_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "external_version": self.external_version,
            "document_id": self.document_id,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
        }


def register_delivered_source(
    session: Session,
    envelope: SourceEnvelope,
    *,
    declaration: SourceDeclaration | None = None,
) -> RegisteredSource:
    """Register the Document or inbound message one stored delivery carried.

    Runs in the caller's transaction.  The envelope is how a caller names a
    stored delivery: ``require_stored_envelope`` refuses one that is not the
    exact retained customer, project and bytes, so nothing downstream has to
    trust the caller's copy of the ingress record.  Idempotent by the same
    identities the two registrations already converge on — identical bytes
    reach the Document already registered, and a redelivered message reaches
    the message already received.
    """

    declaration = declaration or SourceDeclaration()
    delivery = require_stored_envelope(session, envelope)
    if delivery.channel in MESSAGE_CHANNELS:
        if declaration.source_kind is not None:
            raise SourceRegistrationRefused(
                "a delivered message's kind is its channel's, not a declared one"
            )
        from corridor.email_intake import receive_pulled_message

        message = receive_pulled_message(
            session,
            envelope=envelope,
            attachment_doc_types=(
                dict(declaration.attachment_source_kinds)
                if declaration.attachment_source_kinds
                else None
            ),
        )
        return RegisteredSource(
            delivery_id=delivery.id,
            external_version=envelope.external_version,
            message_id=message.message_id,
            thread_id=message.thread_id,
        )

    if declaration.attachment_source_kinds:
        raise SourceRegistrationRefused(
            "only a delivered message carries attachments of its own"
        )
    kind = declaration.source_kind or UNDECLARED_SOURCE_KIND
    if kind not in DOC_TYPES or kind == "email":
        raise SourceRegistrationRefused(
            f"{kind!r} is not a document kind a delivered source registers as"
        )
    filename = str(envelope.metadata.get("filename") or "").strip()
    if not filename:
        raise SourceRegistrationRefused(
            "a delivered source registers under the name it was delivered as"
        )
    from corridor.ingest import ingest_document

    # The store may be remote, so the exact bytes are read back through the
    # storage interface and staged locally under their own digest for the
    # registration that parses them.
    body = content_store().get(
        envelope.bytes_reference, sha256=envelope.content_digest
    )
    staged = store_bytes(
        body, sha256=envelope.content_digest, suffix=Path(filename).suffix.lower()
    )
    document = ingest_document(
        session,
        project_id=delivery.project_id,
        path=staged,
        doc_type=kind,
        images_dir=settings.corpus_images,
        filename=filename,
        expected_sha256=envelope.content_digest,
        source_delivery_id=delivery.id,
    )
    return RegisteredSource(
        delivery_id=delivery.id,
        external_version=envelope.external_version,
        document_id=document.id,
    )


class DeliveryTransactions:
    """Where each delivery of one pass is written, and when it commits.

    The two shapes are a real difference and not a preference.  A supervised
    pass must hold no runtime transaction while it talks to an external system,
    and must make its refusal evidence durable *before* deciding whether it may
    advance past it (ADR-0089), so it commits per delivery.  A build command
    replaying a retained recording must leave nothing behind when it fails, so
    it runs inside the one transaction its caller commits.
    """

    def __init__(self, *, session_factory=None, session: Session | None = None):
        if (session_factory is None) == (session is None):
            raise SourceRegistrationRefused(
                "a pass commits per delivery or runs in one caller transaction"
            )
        self._session_factory = session_factory
        self._session = session

    @classmethod
    def committing(cls, session_factory) -> "DeliveryTransactions":
        """One short committed transaction per delivery and per registration."""

        return cls(session_factory=session_factory)

    @classmethod
    def within(cls, session: Session) -> "DeliveryTransactions":
        """The caller's one open transaction, which the caller commits."""

        return cls(session=session)

    @contextmanager
    def writing(self):
        if self._session is not None:
            yield self._session
            return
        with self._session_factory() as session:
            with session.begin():
                yield session

    def activation_context(self, binding: DeliveryBinding):
        """The deployment proof customer pull requires before any connector I/O.

        Only a real session factory can supply it (``activation_runtime``
        insists on opening a database session of its own), so a pass inside a
        caller's transaction has none — and ``require_pull_delivery`` therefore
        admits that shape only on a local or synthetic runtime, which is where
        the offline replay command already refuses to run otherwise.
        """

        if self._session_factory is None:
            return None
        from corridor.activation_runtime import DeliveryActivationContext

        return DeliveryActivationContext(self._session_factory, binding)


class PassLedger:
    """Record one pass's deliveries, and register the source each one carried.

    This is the ``DeliveryLedger`` of ADR-0089 plus the step that was missing
    from it.  ``record`` is called for every listed change, whatever became of
    its bytes; ``register`` is called only for a delivery whose exact bytes are
    stored, and in the committing shape that happens after the delivery's own
    transaction has committed.
    """

    def __init__(
        self,
        transactions: DeliveryTransactions,
        binding: DeliveryBinding,
        *,
        service_identity: str,
        run_identity: str,
        declare: Callable[[SourceEnvelope], SourceDeclaration] | None = None,
    ) -> None:
        self.activation_context = transactions.activation_context(binding)
        self._transactions = transactions
        self._binding = binding
        self._service_identity = service_identity
        self._run_identity = run_identity
        self._declare = declare
        self.dispositions: Counter[str] = Counter()
        self.registered: list[RegisteredSource] = []

    def record(
        self,
        item: ChangeItem,
        *,
        disposition: str,
        content_digest: str,
        bytes_reference: str,
        refusal_reason: str | None,
    ) -> int:
        observation = DeliveryObservation(
            external_identity=item.item_id,
            external_version=item.version_id,
            content_digest=content_digest,
            bytes_reference=bytes_reference,
            original_timestamps=dict(item.original_timestamps),
            metadata=delivered_metadata(item),
        )
        with self._transactions.writing() as writing:
            if disposition == DISPOSITION_STORED:
                recorded = take_delivery(
                    writing,
                    self._binding,
                    observation,
                    service_identity=self._service_identity,
                    run_identity=self._run_identity,
                )
            else:
                recorded = record_delivery(
                    writing,
                    self._binding,
                    observation,
                    disposition=disposition,
                    service_identity=self._service_identity,
                    run_identity=self._run_identity,
                    refusal_reason=refusal_reason,
                )
        self.dispositions[recorded.disposition] += 1
        return recorded.delivery_id

    def register(self, envelope: SourceEnvelope) -> None:
        """Register one stored delivery's source, in its own short transaction."""

        declaration = self._declare(envelope) if self._declare is not None else None
        with self._transactions.writing() as registering:
            self.registered.append(
                register_delivered_source(
                    registering, envelope, declaration=declaration
                )
            )
