"""The versioned product-analytics event and receipt contract (#558).

Corridor measures net coordination-record economics, review burden, evidence-
to-record latency, and attention quality (#532). Rather than retrofitting
instrumentation after the workflow exists, this module establishes one shared
event contract, recording seam, and binding format that each primitive emits as
it ships.

Ten event families are defined, each with an explicit version, typed payload,
context binding, and a stated derivable-receipt alternative where an in-database
receipt already exists or can serve as the historical record:

1. ``source_arrival``: An external document or stream arrived at the intake
   boundary. Derivable-receipt alternative: ``SourceEnvelope`` ingress log.
2. ``source_capture``: The exact source bytes were durably persisted to content-
   addressed storage with their SHA-256 digest. Derivable-receipt alternative:
   ``Document`` row creation timestamp and stored object digest.
3. ``proposed_delta_creation``: A new Proposed Delta was derived from incoming
   source facts against an accepted baseline. Derivable-receipt alternative:
   ``ProposedDelta`` row creation timestamp.
4. ``packet_surfacing``: A Review Packet was presented on the coordinator's
   Work List. Derivable-receipt alternative: Work List evaluation log.
5. ``packet_opening``: The coordinator opened a specific packet for review.
   Explicit interaction event; no persistent database receipt.
6. ``child_decision``: A decision (accept, edit, reject, defer) was selected on
   a child delta. Derivable-receipt alternative: ``DeltaDisposition`` record.
7. ``packet_save``: A Review Packet was saved atomically with its child
   decisions. Derivable-receipt alternative: atomic review transaction receipt.
8. ``follow_up_plan_creation``: An accepted Follow-up Plan was created from a
   coordination need. Derivable-receipt alternative: ``FollowUpPlan`` table row.
9. ``release_candidate_preparation``: A customer release candidate was prepared
   from a frozen Project Record revision. Derivable-receipt alternative:
   ``ReleaseCandidate`` receipt.
10. ``release_authorization``: A release candidate was sealed and authorized by
    a human principal. Derivable-receipt alternative: ``ReleaseAuthorization`` receipt.
11. ``artifact_repair``: An artifact was manually repaired or reassembled.
    Derivable-receipt alternative: operations reconciliation report.
12. ``portfolio_reading``: The weekly portfolio view was presented. Derivable-
    receipt alternative: weekly portfolio presentation receipt.
13. ``project_selection``: A project was selected from the portfolio view.
    Explicit interaction event; no persistent database receipt.
14. ``follow_up_reading``: The contact-ready follow-up bundles were presented
    for one project at one declared cutoff (#425, #658). Derivable-receipt
    alternative: none — the chase list is derived from accepted authority and
    stores nothing, so being shown it leaves no row behind. The event carries
    the reading's own content digest, so what a coordinator was shown can be
    rebuilt from the records rather than trusted from a screenshot.

Every event binds:
- ``code_revision``: git commit or deployment code version.
- ``product_revision``: release milestone or product architecture version.
- ``packetizer_rules_version``: version of the grouping / packetizer policy.
- ``source_configuration``: configuration of the source family.
- ``connector_configuration``: connector type and connection attributes.
- ``template_identity``: customer UCM template version.
- ``mapping_identity``: semantic mapping version.
- ``enabled_feature_flags``: tuple of active feature flag strings.

Low-cardinality metric rule:
Customer and project identifiers belong in structured logs and database-backed
analytical records, NEVER in infrastructure metric labels (#491, #522). Metric
labels are validated on emission and any high-cardinality identifiers are
refused.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Protocol
from uuid import uuid4

logger = logging.getLogger("corridor.analytics")

DEFAULT_EVENT_VERSION = "1.0"
CURRENT_PRODUCT_REVISION = "ADR-0086-v1"
CURRENT_PACKETIZER_VERSION = "packetizer-v1"
CURRENT_TEMPLATE_IDENTITY = "default-ucm-v1"
CURRENT_MAPPING_IDENTITY = "mapping-v1"

# Identifiers that MUST NEVER appear in metric labels to avoid cardinality explosion
DISALLOWED_METRIC_LABEL_KEYS = frozenset(
    {
        "customer_id",
        "customer",
        "customer_slug",
        "project_id",
        "project",
        "project_slug",
        "user_id",
        "principal_id",
        "actor_id",
        "document_id",
        "sha256",
        "delta_id",
        "packet_id",
    }
)


class AnalyticsError(ValueError):
    """Base error for analytics contract violations."""


class InvalidEventError(AnalyticsError):
    """An event is missing required fields, has an unknown family, or is unversioned."""


class HighCardinalityMetricError(AnalyticsError):
    """Customer or project identifiers were supplied as infrastructure metric labels."""


class EventFamily(str, Enum):
    SOURCE_ARRIVAL = "source_arrival"
    SOURCE_CAPTURE = "source_capture"
    PROPOSED_DELTA_CREATION = "proposed_delta_creation"
    PACKET_SURFACING = "packet_surfacing"
    PACKET_OPENING = "packet_opening"
    CHILD_DECISION = "child_decision"
    PACKET_SAVE = "packet_save"
    FOLLOW_UP_PLAN_CREATION = "follow_up_plan_creation"
    RELEASE_CANDIDATE_PREPARATION = "release_candidate_preparation"
    RELEASE_AUTHORIZATION = "release_authorization"
    ARTIFACT_REPAIR = "artifact_repair"
    PORTFOLIO_READING = "portfolio_reading"
    PROJECT_SELECTION = "project_selection"
    FOLLOW_UP_READING = "follow_up_reading"


DERIVABLE_RECEIPT_ALTERNATIVES: dict[EventFamily, str] = {
    EventFamily.SOURCE_ARRIVAL: "SourceEnvelope ingress log or transport receipt",
    EventFamily.SOURCE_CAPTURE: "Document row creation timestamp and stored object digest",
    EventFamily.PROPOSED_DELTA_CREATION: "ProposedDelta row creation timestamp",
    EventFamily.PACKET_SURFACING: "Work List evaluation log and presentation cache",
    EventFamily.PACKET_OPENING: "Explicit interaction event; no static database receipt",
    EventFamily.CHILD_DECISION: "DeltaDisposition record with attributable timestamp",
    EventFamily.PACKET_SAVE: "Atomic review transaction receipt",
    EventFamily.FOLLOW_UP_PLAN_CREATION: "FollowUpPlan table row",
    EventFamily.RELEASE_CANDIDATE_PREPARATION: "ReleaseCandidate receipt",
    EventFamily.RELEASE_AUTHORIZATION: "ReleaseAuthorization receipt with principal and timestamp",
    EventFamily.ARTIFACT_REPAIR: "Operations reconciliation report",
    EventFamily.PORTFOLIO_READING: "Weekly portfolio presentation receipt",
    EventFamily.PROJECT_SELECTION: "Explicit interaction event; no static database receipt",
    EventFamily.FOLLOW_UP_READING: (
        "Explicit presentation event; the chase list is derived and stores nothing"
    ),
}


@dataclass(frozen=True)
class AnalyticsBinding:
    """The mandatory environmental and configuration context every record binds."""

    code_revision: str
    product_revision: str
    packetizer_rules_version: str
    source_configuration: dict[str, Any] = field(default_factory=dict)
    connector_configuration: dict[str, Any] = field(default_factory=dict)
    template_identity: str = CURRENT_TEMPLATE_IDENTITY
    mapping_identity: str = CURRENT_MAPPING_IDENTITY
    enabled_feature_flags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code_revision": self.code_revision,
            "product_revision": self.product_revision,
            "packetizer_rules_version": self.packetizer_rules_version,
            "source_configuration": self.source_configuration,
            "connector_configuration": self.connector_configuration,
            "template_identity": self.template_identity,
            "mapping_identity": self.mapping_identity,
            "enabled_feature_flags": list(self.enabled_feature_flags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalyticsBinding:
        return cls(
            code_revision=data["code_revision"],
            product_revision=data["product_revision"],
            packetizer_rules_version=data["packetizer_rules_version"],
            source_configuration=data.get("source_configuration", {}),
            connector_configuration=data.get("connector_configuration", {}),
            template_identity=data.get("template_identity", CURRENT_TEMPLATE_IDENTITY),
            mapping_identity=data.get("mapping_identity", CURRENT_MAPPING_IDENTITY),
            enabled_feature_flags=tuple(data.get("enabled_feature_flags", ())),
        )


def default_binding(
    *,
    code_revision: str = "git:current",
    product_revision: str = CURRENT_PRODUCT_REVISION,
    packetizer_rules_version: str = CURRENT_PACKETIZER_VERSION,
    source_configuration: dict[str, Any] | None = None,
    connector_configuration: dict[str, Any] | None = None,
    template_identity: str = CURRENT_TEMPLATE_IDENTITY,
    mapping_identity: str = CURRENT_MAPPING_IDENTITY,
    enabled_feature_flags: tuple[str, ...] = (),
) -> AnalyticsBinding:
    """Construct a standard binding for the current runtime."""

    return AnalyticsBinding(
        code_revision=code_revision,
        product_revision=product_revision,
        packetizer_rules_version=packetizer_rules_version,
        source_configuration=source_configuration or {},
        connector_configuration=connector_configuration or {},
        template_identity=template_identity,
        mapping_identity=mapping_identity,
        enabled_feature_flags=enabled_feature_flags,
    )


def validate_metric_labels(labels: dict[str, str]) -> None:
    """Ensure metric labels do not carry customer or project identifiers."""

    for key, val in labels.items():
        low_key = key.lower()
        if low_key in DISALLOWED_METRIC_LABEL_KEYS:
            raise HighCardinalityMetricError(
                f"metric label {key!r} carries high-cardinality identity; "
                "customer and project identifiers belong in event payloads only"
            )
        if (
            "customer" in low_key
            or "project" in low_key
            or "user" in low_key
            or "account" in low_key
        ):
            raise HighCardinalityMetricError(
                f"metric label {key!r} contains forbidden identity token; "
                "customer and project identifiers must not enter metrics"
            )


@dataclass(frozen=True)
class AnalyticsEvent:
    """An immutable, versioned event record."""

    family: EventFamily
    payload: dict[str, Any]
    binding: AnalyticsBinding
    event_id: str = field(default_factory=lambda: uuid4().hex)
    version: str = DEFAULT_EVENT_VERSION
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metric_labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.version or not isinstance(self.version, str):
            raise InvalidEventError("event must have a non-empty string version")
        if not isinstance(self.family, EventFamily):
            if isinstance(self.family, str):
                try:
                    object.__setattr__(self, "family", EventFamily(self.family))
                except ValueError:
                    raise InvalidEventError(f"unknown event family: {self.family!r}")
            else:
                raise InvalidEventError(f"invalid event family: {self.family!r}")
        if not isinstance(self.binding, AnalyticsBinding):
            raise InvalidEventError("event must carry an AnalyticsBinding")
        validate_metric_labels(self.metric_labels)

    @property
    def derivable_receipt_alternative(self) -> str:
        return DERIVABLE_RECEIPT_ALTERNATIVES[self.family]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "family": self.family.value,
            "version": self.version,
            "occurred_at": self.occurred_at.isoformat(),
            "binding": self.binding.as_dict(),
            "payload": self.payload,
            "metric_labels": self.metric_labels,
            "derivable_receipt_alternative": self.derivable_receipt_alternative,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalyticsEvent:
        if "family" not in data or not data.get("version"):
            raise InvalidEventError("event data missing family or version")
        occurred = datetime.fromisoformat(data["occurred_at"])
        binding = AnalyticsBinding.from_dict(data["binding"])
        return cls(
            event_id=data["event_id"],
            family=EventFamily(data["family"]),
            version=data["version"],
            occurred_at=occurred,
            binding=binding,
            payload=data.get("payload", {}),
            metric_labels=data.get("metric_labels", {}),
        )


# Factory helpers for each event family


def source_arrival_event(
    binding: AnalyticsBinding,
    *,
    customer_id: str | None = None,
    project_id: int | str | None = None,
    channel: str = "upload",
    filename: str,
    content_sha256: str,
    byte_count: int,
    metric_labels: dict[str, str] | None = None,
) -> AnalyticsEvent:
    """Construct a source_arrival event."""

    labels = {"channel": channel, "status": "arrived"}
    if metric_labels:
        labels.update(metric_labels)
    return AnalyticsEvent(
        family=EventFamily.SOURCE_ARRIVAL,
        payload={
            "customer_id": customer_id,
            "project_id": project_id,
            "channel": channel,
            "filename": filename,
            "content_sha256": content_sha256,
            "byte_count": byte_count,
        },
        binding=binding,
        metric_labels=labels,
    )


def source_capture_event(
    binding: AnalyticsBinding,
    *,
    customer_id: str | None = None,
    project_id: int | str | None = None,
    channel: str = "upload",
    storage_key: str,
    content_sha256: str,
    byte_count: int,
    metric_labels: dict[str, str] | None = None,
) -> AnalyticsEvent:
    """Construct a source_capture event."""

    labels = {"channel": channel, "status": "captured"}
    if metric_labels:
        labels.update(metric_labels)
    return AnalyticsEvent(
        family=EventFamily.SOURCE_CAPTURE,
        payload={
            "customer_id": customer_id,
            "project_id": project_id,
            "channel": channel,
            "storage_key": storage_key,
            "content_sha256": content_sha256,
            "byte_count": byte_count,
        },
        binding=binding,
        metric_labels=labels,
    )


class EventEmitter(Protocol):
    """Protocol for analytics event sinks."""

    def emit(self, event: AnalyticsEvent) -> None:
        """Emit one analytics event."""
        ...


class LoggingEventEmitter:
    """Default emitter: outputs structured JSON records to logging."""

    def emit(self, event: AnalyticsEvent) -> None:
        logger.info(
            "analytics_event %s",
            json.dumps(event.as_dict(), sort_keys=True),
            extra={"analytics_event": event.as_dict()},
        )


class InMemoryEventCollector:
    """In-memory collector used in tests and verification harnesses."""

    def __init__(self) -> None:
        self.events: list[AnalyticsEvent] = []

    def emit(self, event: AnalyticsEvent) -> None:
        self.events.append(event)

    def clear(self) -> None:
        self.events.clear()

    def by_family(self, family: EventFamily) -> list[AnalyticsEvent]:
        return [e for e in self.events if e.family == family]


_CURRENT_EMITTER: EventEmitter = LoggingEventEmitter()


def get_emitter() -> EventEmitter:
    return _CURRENT_EMITTER


def set_emitter(emitter: EventEmitter) -> None:
    global _CURRENT_EMITTER
    _CURRENT_EMITTER = emitter


@contextmanager
def capture_events() -> Iterator[InMemoryEventCollector]:
    """Context manager to capture emitted analytics events in memory."""

    collector = InMemoryEventCollector()
    previous = get_emitter()
    set_emitter(collector)
    try:
        yield collector
    finally:
        set_emitter(previous)


def emit_event(event: AnalyticsEvent) -> None:
    """Emit an analytics event through the current emitter."""

    get_emitter().emit(event)
