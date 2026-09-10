"""The four-method PullConnector contract and normalized SourceEnvelope (#496).

ADR-0078 defined connected location intake, and ADR-0083 corrected its design:
connected intake is split into three layers:

1. ``SourceEnvelope``: the one normalized ingress record every intake channel
   produces (customer, project, channel, external identity and version,
   original timestamps, content digest and bytes reference, metadata, delivery
   identity, and idempotency key).
2. ``PullConnector``: a four-method pull contract:
   - ``list_changes(cursor)``
   - ``fetch_version(item_id, version_id)``
   - ``get_metadata(item_id)``
   - ``checkpoint(token)`` (renamed from ``acknowledge`` in ADR-0083).
3. ``PushIntake``: authenticated, pre-bound delivery for webhooks and uploads (#511).

Crash-safe checkpoint semantics (ADR-0083, extended by ADR-0089):
A checkpoint advances only after every change up to and including the exact
token is durably stored in the content-addressed store with its digest. A crash
between listing and storage re-lists without creating duplicate Documents or
missing items. Replay after a crash is idempotent by delivery identity; a
checkpoint never advances past an unstored change.

ADR-0083's rule, read literally, had no answer for a change that will never be
stored because the intake gate refused it: one poisoned object stalled a
location forever. ADR-0089 states the rule in full. A delivery the gate refused
permits an advance past it **only** once its digest and the refusal evidence
are durably recorded in the delivery ledger, which is why ``sync_pull_connector``
refuses to checkpoint past a refusal it could not record. A transient failure —
a scanner that raised, an object store that rejected a write, a provider that
returned an error — never permits one, because it says nothing about the
delivery and advancing past it drops a source revision silently.

Bytes are persisted through the storage interface (``corridor.object_storage``),
never direct filesystem path writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Sequence

from corridor.intake_hardening import HostileContentRefused, inspect_byte_gate
from corridor.object_storage import content_key, content_store, store_bytes


@dataclass(frozen=True)
class ChangeItem:
    """One observed change in a connected location."""

    item_id: str
    version_id: str
    name: str
    original_timestamps: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceEnvelope:
    """The normalized ingress record shared across pull and push channels."""

    customer: str
    project: str
    channel: str
    external_identity: str
    external_version: str
    original_timestamps: dict[str, Any]
    content_digest: str
    bytes_reference: str
    metadata: dict[str, Any]
    delivery_identity: str
    idempotency_key: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PullConnector(Protocol):
    """The four-method pull connector contract (ADR-0078, ADR-0083)."""

    def list_changes(self, cursor: str | None = None) -> tuple[Sequence[ChangeItem], str]:
        """List changes since cursor, returning items and next checkpoint token."""
        ...

    def fetch_version(self, item_id: str, version_id: str) -> bytes:
        """Fetch raw bytes for a specific version of an item."""
        ...

    def get_metadata(self, item_id: str) -> dict[str, Any]:
        """Fetch metadata for an item."""
        ...

    def checkpoint(self, token: str) -> None:
        """Advance cursor token only after durable storage."""
        ...


def build_delivery_identity(
    *,
    customer: str,
    project: str,
    channel: str,
    external_identity: str,
    external_version: str,
) -> str:
    """Deterministic delivery identity per ADR-0083."""

    canonical = f"{customer}:{project}:{channel}:{external_identity}:{external_version}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def delivered_metadata(item: ChangeItem) -> dict[str, Any]:
    """The metadata both the ledger row and the envelope of one delivery carry.

    The connector's own metadata, plus the name the source was delivered as —
    which only ``ChangeItem.name`` holds and which a consumer needs before it
    can register the source at all.  One function rather than one derivation
    per writer, because ``require_stored_envelope`` compares a consumer's
    envelope against the retained row for equality: two derivations that
    disagree about one key do not read differently, they refuse the delivery.
    """

    return {"filename": item.name, **dict(item.metadata)}


def build_idempotency_key(
    delivery_identity: str,
    content_digest: str,
) -> str:
    """Deterministic idempotency key combining delivery identity and content digest."""

    canonical = f"{delivery_identity}:{content_digest}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeliveryRecord:
    """One item's outcome in a sync pass, and the ledger row that holds it.

    ``delivery_id`` is the identity the ledger assigned; it is ``None`` when no
    ledger was supplied, which is exactly the case in which a refusal has no
    durable evidence and therefore cannot be advanced past.
    """

    item_id: str
    version_id: str
    disposition: str
    content_digest: str
    delivery_id: int | None = None
    refusal_reason: str | None = None
    envelope: SourceEnvelope | None = None


@dataclass(frozen=True)
class PullSyncResult:
    """What one pass took delivery of, and whether its cursor could move."""

    envelopes: tuple[SourceEnvelope, ...]
    records: tuple[DeliveryRecord, ...]
    checkpoint_token: str | None
    advanced: bool
    blocked_by: tuple[str, ...] = ()

    def delivery_ids(self) -> tuple[int, ...]:
        return tuple(
            record.delivery_id
            for record in self.records
            if record.delivery_id is not None
        )


class DeliveryLedger(Protocol):
    """Where a pass records what it took delivery of.

    Deliberately not a fifth ``PullConnector`` method: the four methods are a
    cursor protocol and a connector "does not read, classify, or route content"
    (ADR-0078), so recording a delivery is the runtime's act on the envelope the
    connector returned, not a connector responsibility (ADR-0089).
    """

    def record(
        self,
        item: ChangeItem,
        *,
        disposition: str,
        content_digest: str,
        bytes_reference: str,
        refusal_reason: str | None,
    ) -> int:
        """Persist one delivery outcome and return its ledger identity."""
        ...


def sync_pull_connector(
    connector: PullConnector,
    *,
    customer: str,
    project: str,
    channel: str,
    cursor: str | None = None,
    on_envelope_stored: Callable[[SourceEnvelope], None] | None = None,
    ledger: DeliveryLedger | None = None,
) -> PullSyncResult:
    """Execute one crash-safe sync pass over a PullConnector.

    Enforces ADR-0083 semantics as ADR-0089 extends them:
    - Lists changes since cursor.
    - For every change: fetches bytes, runs the #490 byte gate on them, and
      durably persists to the content store before creating the envelope.
    - A gate refusal is a fact about the delivery: the pass records it with its
      exact digest and its reason and carries on.
    - A scanner or storage failure is a fact about one attempt: the pass records
      it and refuses to advance the cursor at all.
    - Checkpoint is invoked ONLY after every item up to the token is either
      durably stored or durably recorded as refused. A crash before checkpoint
      advances causes the next pass to re-list, where content-addressed storage
      makes re-storage an idempotent no-op.

    A provider that cannot serve the bytes at all raises out of this pass. The
    attempt fails, nothing checkpoints, and the next pass re-lists from the
    cursor that never moved.
    """

    from corridor.activation_runtime import require_pull_delivery
    require_pull_delivery(ledger, customer=customer, project=project, channel=channel)
    items, next_token = connector.list_changes(cursor)
    envelopes: list[SourceEnvelope] = []
    records: list[DeliveryRecord] = []
    blocked: list[str] = []

    for item in items:
        require_pull_delivery(ledger, customer=customer, project=project, channel=channel)
        body = connector.fetch_version(item.item_id, item.version_id)
        digest = hashlib.sha256(body).hexdigest()

        suffix = ""
        if "." in item.name:
            suffix = "." + item.name.rsplit(".", 1)[1].lower()

        disposition = "stored"
        refusal_reason: str | None = None
        storage_key = ""
        try:
            inspect_byte_gate(body, item.name)
        except HostileContentRefused as refused:
            disposition = "terminally_refused"
            refusal_reason = f"{refused.rule}: {refused.reason}"
        except Exception as failure:
            # The gate's own refusal above is a fact about the delivery; a gate
            # that failed to reach a verdict — a scanner seam that raised — is
            # a fact about one attempt, and says nothing about the bytes.
            disposition = "transient_failure"
            refusal_reason = f"scan_failed: {failure}"
        else:
            try:
                # Re-read deployment inputs after fetch, before storing bytes.
                require_pull_delivery(ledger, customer=customer, project=project, channel=channel)
                # Persist through the object storage interface.
                store_bytes(body, sha256=digest, suffix=suffix)
            except Exception as failure:
                # Any backend may fail, and none of them by failing tell us
                # anything about the delivery.
                disposition = "transient_failure"
                refusal_reason = f"store_failed: {failure}"
            else:
                storage_key = content_key(digest, suffix)

        delivery_id = None
        if ledger is not None:
            delivery_id = ledger.record(
                item,
                disposition=disposition,
                content_digest=digest,
                bytes_reference=storage_key,
                refusal_reason=refusal_reason,
            )

        envelope = None
        if disposition == "stored":
            identity = build_delivery_identity(
                customer=customer,
                project=project,
                channel=channel,
                external_identity=item.item_id,
                external_version=item.version_id,
            )
            envelope = SourceEnvelope(
                customer=customer,
                project=project,
                channel=channel,
                external_identity=item.item_id,
                external_version=item.version_id,
                original_timestamps=dict(item.original_timestamps),
                content_digest=digest,
                bytes_reference=storage_key,
                metadata=delivered_metadata(item),
                delivery_identity=identity,
                idempotency_key=build_idempotency_key(identity, digest),
            )
            if on_envelope_stored is not None:
                on_envelope_stored(envelope)
            envelopes.append(envelope)
        elif disposition == "transient_failure":
            blocked.append(item.item_id)
        elif delivery_id is None:
            # A refusal nobody recorded is a refusal nobody can answer for, so
            # it does not permit an advance past itself (ADR-0089).
            blocked.append(item.item_id)

        records.append(
            DeliveryRecord(
                item_id=item.item_id,
                version_id=item.version_id,
                disposition=disposition,
                content_digest=digest,
                delivery_id=delivery_id,
                refusal_reason=refusal_reason,
                envelope=envelope,
            )
        )

    advanced = False
    if not blocked and next_token and next_token != cursor:
        connector.checkpoint(next_token)
        advanced = True

    return PullSyncResult(
        envelopes=tuple(envelopes),
        records=tuple(records),
        checkpoint_token=next_token if advanced else cursor,
        advanced=advanced,
        blocked_by=tuple(blocked),
    )
