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

Crash-safe checkpoint semantics (ADR-0083):
A checkpoint advances only after every change up to and including the exact
token is durably stored in the content-addressed store with its digest. A crash
between listing and storage re-lists without creating duplicate Documents or
missing items. Replay after a crash is idempotent by delivery identity; a
checkpoint never advances past an unstored change.

Bytes are persisted through the storage interface (``corridor.object_storage``),
never direct filesystem path writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Sequence

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


def build_idempotency_key(
    delivery_identity: str,
    content_digest: str,
) -> str:
    """Deterministic idempotency key combining delivery identity and content digest."""

    canonical = f"{delivery_identity}:{content_digest}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sync_pull_connector(
    connector: PullConnector,
    *,
    customer: str,
    project: str,
    channel: str,
    cursor: str | None = None,
    on_envelope_stored: Callable[[SourceEnvelope], None] | None = None,
) -> tuple[SourceEnvelope, ...]:
    """Execute one crash-safe sync pass over a PullConnector.

    Enforces ADR-0083 semantics:
    - Lists changes since cursor.
    - For every change: fetches bytes, verifies digest, and durably persists
      to content store before creating the envelope.
    - Checkpoint is invoked ONLY after all items up to the token are durably
      stored. A crash before checkpoint advances causes the next pass to re-list,
      where content-addressed storage makes re-storage an idempotent no-op.
    """

    items, next_token = connector.list_changes(cursor)
    envelopes: list[SourceEnvelope] = []

    for item in items:
        body = connector.fetch_version(item.item_id, item.version_id)
        digest = hashlib.sha256(body).hexdigest()

        # Persist through object storage interface
        suffix = ""
        if "." in item.name:
            suffix = "." + item.name.rsplit(".", 1)[1].lower()
        storage_key = content_key(digest, suffix)
        store_bytes(body, sha256=digest, suffix=suffix)

        delivery_id = build_delivery_identity(
            customer=customer,
            project=project,
            channel=channel,
            external_identity=item.item_id,
            external_version=item.version_id,
        )
        idem_key = build_idempotency_key(delivery_id, digest)

        envelope = SourceEnvelope(
            customer=customer,
            project=project,
            channel=channel,
            external_identity=item.item_id,
            external_version=item.version_id,
            original_timestamps=dict(item.original_timestamps),
            content_digest=digest,
            bytes_reference=storage_key,
            metadata=dict(item.metadata),
            delivery_identity=delivery_id,
            idempotency_key=idem_key,
        )

        if on_envelope_stored is not None:
            on_envelope_stored(envelope)

        envelopes.append(envelope)

    # Durably advance checkpoint only after every item is stored
    if next_token and next_token != cursor:
        connector.checkpoint(next_token)

    return tuple(envelopes)
