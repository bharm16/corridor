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

Each family with an emitter owns its payload shape here, as one constructor
function returning the ``AnalyticsEvent`` (``child_decision_event``,
``packet_save_event`` and so on, in the shape ``source_arrival_event`` set).
Emitters call the constructor rather than spelling a dict; the keys a reader may
rely on are the constructor's keyword parameters. The event still carries a
plain ``dict`` payload, because ``as_dict``/``from_dict`` and every historical
row are dicts. Rows logged before a family declared its shape are read through
``declared_payload``, ``act_outcome`` and ``child_decision_action``, which hold
the legacy-key translations once; readers do not fall back inline. Families
that only arrive as imported observations (``artifact_repair``,
``work_observation``, ``measurement_sample``, ``provider_usage``) have no
emitter here and so no constructor.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
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
    PROJECT_OPENING = "project_opening"
    EVIDENCE_OPENING = "evidence_opening"
    COVERAGE_READING = "coverage_reading"
    COVERAGE_CONFIRMATION = "coverage_confirmation"
    PREPARATION_REQUEST = "preparation_request"
    PREPARATION_ATTEMPT = "preparation_attempt"
    DELTA_SUPERSESSION = "delta_supersession"
    WORK_OBSERVATION = "work_observation"
    MEASUREMENT_SAMPLE = "measurement_sample"
    PROVIDER_USAGE = "provider_usage"


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
    EventFamily.PROJECT_OPENING: "Explicit project presentation event; no domain write",
    EventFamily.EVIDENCE_OPENING: "Explicit source/evidence opening; no domain write",
    EventFamily.COVERAGE_READING: "Explicit presentation of the derived coverage reading",
    EventFamily.COVERAGE_CONFIRMATION: "IssueCoverageDeclaration with actor and confirmed_at",
    EventFamily.PREPARATION_REQUEST: "ReleasePreparationRequest with declaration and requested_at",
    EventFamily.PREPARATION_ATTEMPT: "ReleasePreparationAttempt with started_at and finished_at",
    EventFamily.DELTA_SUPERSESSION: "DeltaSupersession naming the prior delta and superseding delta or source reading",
    EventFamily.WORK_OBSERVATION: "Attributable measured-time entry; never inferred from idle time",
    EventFamily.MEASUREMENT_SAMPLE: "Attributable sampling observation and retained evidence reference",
    EventFamily.PROVIDER_USAGE: "Actual provider call/billing receipt, retry or retained cache-use record",
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
    issue_profile_identity: str | None = None
    issue_profile_version: int | None = None
    issue_profile_sha256: str | None = None
    customer_id: str | None = None
    environment: str | None = None
    database_identity: str | None = None

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
            "issue_profile_identity": self.issue_profile_identity,
            "issue_profile_version": self.issue_profile_version,
            "issue_profile_sha256": self.issue_profile_sha256,
            "customer_id": self.customer_id,
            "environment": self.environment,
            "database_identity": self.database_identity,
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
            issue_profile_identity=data.get("issue_profile_identity"),
            issue_profile_version=data.get("issue_profile_version"),
            issue_profile_sha256=data.get("issue_profile_sha256"),
            customer_id=data.get("customer_id"),
            environment=data.get("environment"),
            database_identity=data.get("database_identity"),
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
    issue_profile_identity: str | None = None,
    issue_profile_version: int | None = None,
    issue_profile_sha256: str | None = None,
    customer_id: str | None = None,
    environment: str | None = None,
    database_identity: str | None = None,
) -> AnalyticsBinding:
    """Construct a binding, using the optional measured-deployment declaration.

    The running image's code revision takes precedence over a declaration
    file: a stale file cannot relabel the executable. Missing deployment
    evidence retains the historical placeholders and is reported unmeasured.
    The file contains configuration identities only, never connector secrets.
    """

    configured: dict[str, Any] = {}
    if path := os.environ.get("CORRIDOR_ANALYTICS_BINDING_FILE"):
        configured = AnalyticsBinding.from_dict(json.loads(Path(path).read_text())).as_dict()
    code_revision = os.environ.get("CORRIDOR_CODE_REVISION") or (
        configured.get("code_revision", code_revision) if code_revision == "git:current" else code_revision
    )
    if product_revision == CURRENT_PRODUCT_REVISION:
        product_revision = configured.get("product_revision", product_revision)
    if packetizer_rules_version == CURRENT_PACKETIZER_VERSION:
        packetizer_rules_version = configured.get("packetizer_rules_version", packetizer_rules_version)
    if template_identity == CURRENT_TEMPLATE_IDENTITY:
        template_identity = configured.get("template_identity", template_identity)
    if mapping_identity == CURRENT_MAPPING_IDENTITY:
        mapping_identity = configured.get("mapping_identity", mapping_identity)

    return AnalyticsBinding(
        code_revision=code_revision,
        product_revision=product_revision,
        packetizer_rules_version=packetizer_rules_version,
        source_configuration=source_configuration if source_configuration is not None else configured.get("source_configuration", {}),
        connector_configuration=connector_configuration if connector_configuration is not None else configured.get("connector_configuration", {}),
        template_identity=template_identity,
        mapping_identity=mapping_identity,
        enabled_feature_flags=enabled_feature_flags or tuple(configured.get("enabled_feature_flags", ())),
        issue_profile_identity=issue_profile_identity or configured.get("issue_profile_identity"),
        issue_profile_version=issue_profile_version or configured.get("issue_profile_version"),
        issue_profile_sha256=issue_profile_sha256 or configured.get("issue_profile_sha256"),
        customer_id=customer_id or configured.get("customer_id"),
        environment=environment or configured.get("environment"),
        database_identity=database_identity or configured.get("database_identity"),
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
    byte_count: int | None,
    source_delivery_id: int | None = None,
    occurred_at: datetime | None = None,
    outcome: str = "staged",
    disposition: str | None = None,
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
            "source_delivery_id": source_delivery_id,
            "source_identity": f"delivery:{source_delivery_id}" if source_delivery_id is not None else None,
            "outcome": outcome,
            "disposition": disposition,
        },
        binding=binding,
        occurred_at=occurred_at or datetime.now(timezone.utc),
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
    byte_count: int | None,
    document_id: int | None = None,
    source_delivery_id: int | None = None,
    occurred_at: datetime | None = None,
    outcome: str = "staged",
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
            "document_id": document_id,
            "source_delivery_id": source_delivery_id,
            "source_identity": f"delivery:{source_delivery_id}" if source_delivery_id is not None else
                f"document:{document_id}" if document_id is not None else None,
            "outcome": outcome,
        },
        binding=binding,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        metric_labels=labels,
    )


def proposed_delta_creation_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    source_family: str,
    source_revision: str,
    document_id: int | None,
    delta_id: int,
    outcome: str,
    complete_enumerative_source: bool,
    row_accounting_sealed: bool,
) -> AnalyticsEvent:
    """Construct a proposed_delta_creation event; one delta per event."""

    return AnalyticsEvent(
        family=EventFamily.PROPOSED_DELTA_CREATION,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "project_id": project_id,
            "source_family": source_family,
            "source_revision": source_revision,
            "document_id": document_id,
            "delta_id": delta_id, "delta_count": 1, "delta_ids": [delta_id],
            "outcome": outcome,
            "complete_enumerative_source": complete_enumerative_source,
            "row_accounting_sealed": row_accounting_sealed,
        },
    )


# What the Work List put in front of a person, in the order the screen reads it
# (packet_review._item_payload). Shared by packet_surfacing and packet_opening.
PACKET_ITEM_KEYS: tuple[str, ...] = (
    "project_id",
    "item_key",
    "grouping_key_kind",
    "grouping_key",
    "grouping_rule_version",
    "band",
    "attention_reasons",
    "held_out_reason",
    "child_count",
    "ready_count",
    "held_out_count",
    "unchanged_count",
    "customer_artifacts",
    "artifact_rule_version",
    "observed_accepted_revision_id",
    "cutoff",
    "issue_profile_id",
    "issue_profile_identity",
    "issue_profile_version",
    "issue_profile_sha256",
    "issue_profile_problems",
    "consequence_rule_version",
    "consequence_level",
    "child_consequences",
)


def _packet_item_event(
    family: EventFamily,
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    principal_subject: str | None,
    **item: Any,
) -> AnalyticsEvent:
    if set(item) != set(PACKET_ITEM_KEYS):
        raise InvalidEventError(
            f"{family.value} payload must carry exactly the declared item keys; "
            f"missing {sorted(set(PACKET_ITEM_KEYS) - set(item))}, unexpected {sorted(set(item) - set(PACKET_ITEM_KEYS))}"
        )
    return AnalyticsEvent(
        family=family,
        binding=binding,
        occurred_at=occurred_at,
        payload={**{key: item[key] for key in PACKET_ITEM_KEYS}, "principal_subject": principal_subject},
        metric_labels={
            "grouping_key_kind": item["grouping_key_kind"],
            "band": item["band"],
            "held_out_reason": item["held_out_reason"] or "none",
            "consequence_level": item["consequence_level"] or "none",
        },
    )


def packet_surfacing_event(
    binding: AnalyticsBinding, *, occurred_at: datetime, principal_subject: str | None, **item: Any
) -> AnalyticsEvent:
    """Construct a packet_surfacing event from one Work List item (PACKET_ITEM_KEYS)."""

    return _packet_item_event(EventFamily.PACKET_SURFACING, binding, occurred_at=occurred_at,
                              principal_subject=principal_subject, **item)


def packet_opening_event(
    binding: AnalyticsBinding, *, occurred_at: datetime, principal_subject: str | None, **item: Any
) -> AnalyticsEvent:
    """Construct a packet_opening event from one Work List item (PACKET_ITEM_KEYS)."""

    return _packet_item_event(EventFamily.PACKET_OPENING, binding, occurred_at=occurred_at,
                              principal_subject=principal_subject, **item)


def child_decision_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    delta_id: int,
    action: str,
    effect_kind: str | None,
    outcome: str,
    refusal_reason: str | None,
    revision_id: int | None,
    packet_owned: bool,
    support_assessment_count: int,
) -> AnalyticsEvent:
    """Construct a child_decision event; ``action`` is the record's disposition vocabulary."""

    return AnalyticsEvent(
        family=EventFamily.CHILD_DECISION,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "project_id": project_id,
            "delta_id": delta_id,
            "action": action,
            "effect_kind": effect_kind,
            "outcome": outcome,
            "refusal_reason": refusal_reason,
            "revision_id": revision_id,
            "packet_owned": packet_owned,
            "support_assessment_count": support_assessment_count,
        },
        metric_labels={
            "action": action,
            "outcome": outcome,
            "refusal_reason": refusal_reason or "none",
        },
    )


def packet_save_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    receipt_id: int | None,
    revision_id: int | None,
    grouping_rule_version: str,
    grouping_key_kind: str,
    grouping_key: str,
    observed_accepted_revision_id: int | None,
    child_count: int,
    outcome_counts: dict[str, int],
    outcome: str,
    refusal_reason: str | None,
) -> AnalyticsEvent:
    """Construct a packet_save event, refusals included."""

    return AnalyticsEvent(
        family=EventFamily.PACKET_SAVE,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "project_id": project_id,
            "receipt_id": receipt_id,
            "revision_id": revision_id,
            "grouping_rule_version": grouping_rule_version,
            "grouping_key_kind": grouping_key_kind,
            "grouping_key": grouping_key,
            "observed_accepted_revision_id": observed_accepted_revision_id,
            "child_count": child_count,
            "outcome_counts": outcome_counts,
            "outcome": outcome,
            "refusal_reason": refusal_reason,
            "wrote_revision": revision_id is not None,
        },
        metric_labels={
            "grouping_key_kind": grouping_key_kind,
            "outcome": outcome,
            "refusal_reason": refusal_reason or "none",
            "wrote_revision": "true" if revision_id is not None else "false",
        },
    )


def follow_up_plan_creation_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    delta_id: int,
    follow_up_plan_id: int,
    revision_id: int,
    grouping_rule_version: str,
    has_return_date: bool,
    responsible_kind: str,
    evidence_count: int,
) -> AnalyticsEvent:
    """Construct a follow_up_plan_creation event."""

    return AnalyticsEvent(
        family=EventFamily.FOLLOW_UP_PLAN_CREATION,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "project_id": project_id,
            "delta_id": delta_id,
            "follow_up_plan_id": follow_up_plan_id,
            "revision_id": revision_id,
            "grouping_rule_version": grouping_rule_version,
            "has_return_date": has_return_date,
            "responsible_kind": responsible_kind,
            "evidence_count": evidence_count,
        },
        metric_labels={
            "responsible_kind": responsible_kind,
            "has_return_date": "true" if has_return_date else "false",
        },
    )


def release_candidate_preparation_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    surface: str,
    outcome: str,
    principal_subject: str,
    project_id: int,
    source_cutoff: str,
    accepted_revision_id: int,
    previous_package_id: int | None,
    issue_profile_id: int | None,
    issue_profile_version: int | None,
    coverage_identity: str | None,
    configured_artifact_types: list[str],
    candidate_identity: str | None,
    content_sha256: str | None,
    readiness: str,
    blocker_count: int,
    exception_count: int,
    refusal_code: str | None,
) -> AnalyticsEvent:
    """Construct a release_candidate_preparation event.

    The act's outcome is the ``status`` metric label, not a payload key; the
    project and the person stay in the payload (#491, #522).
    """

    return AnalyticsEvent(
        family=EventFamily.RELEASE_CANDIDATE_PREPARATION,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "principal_subject": principal_subject,
            "project_id": project_id,
            "source_cutoff": source_cutoff,
            "accepted_revision_id": accepted_revision_id,
            "previous_package_id": previous_package_id,
            "issue_profile_id": issue_profile_id,
            "issue_profile_version": issue_profile_version,
            "coverage_identity": coverage_identity,
            "configured_artifact_types": configured_artifact_types,
            "candidate_identity": candidate_identity,
            "content_sha256": content_sha256,
            "readiness": readiness,
            "blocker_count": blocker_count,
            "exception_count": exception_count,
            "refusal_code": refusal_code,
        },
        metric_labels={"surface": surface, "status": outcome},
    )


def release_authorization_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    surface: str,
    status: str,
    principal_subject: str,
    project_id: int | None,
    candidate_id: int | None,
    candidate_identity: str | None,
    accepted_revision_id: int | None,
    issue_profile_version: int | None,
    source_cutoff: str | None,
    package_identity: str | None,
    issue_number: int | None,
    refusal_code: str | None,
) -> AnalyticsEvent:
    """Construct a release_authorization event; ``status`` is its metric label, as above."""

    return AnalyticsEvent(
        family=EventFamily.RELEASE_AUTHORIZATION,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "principal_subject": principal_subject,
            "project_id": project_id,
            "candidate_id": candidate_id,
            "candidate_identity": candidate_identity,
            "accepted_revision_id": accepted_revision_id,
            "issue_profile_version": issue_profile_version,
            "source_cutoff": source_cutoff,
            "package_identity": package_identity,
            "issue_number": issue_number,
            "refusal_code": refusal_code,
        },
        metric_labels={"surface": surface, "status": status},
    )


def portfolio_reading_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    principal_subject: str,
    cutoff: str,
    projects: list[dict[str, Any]],
) -> AnalyticsEvent:
    """Construct a portfolio_reading event naming every project shown and its state."""

    return AnalyticsEvent(
        family=EventFamily.PORTFOLIO_READING,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "principal_subject": principal_subject,
            "cutoff": cutoff,
            "project_count": len(projects),
            "projects": projects,
        },
        metric_labels={"surface": "portfolio", "status": "presented"},
    )


def project_selection_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    principal_subject: str,
    project_id: int,
    state: str,
    landing: str,
    measurement_context: dict[str, Any] | None,
) -> AnalyticsEvent:
    """Construct a project_selection event; the standing's measurement context is spread in."""

    return AnalyticsEvent(
        family=EventFamily.PROJECT_SELECTION,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "principal_subject": principal_subject,
            "project_id": project_id,
            "state": state,
            "landing": landing,
            **(measurement_context or {}),
        },
        metric_labels={"surface": "portfolio", "state": state},
    )


def follow_up_reading_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    surface: str,
    principal_subject: str,
    project_id: int,
    cutoff: str,
    rule_set: str,
    accepted_revision_id: int | None,
    reading_identity: str,
    bundle_count: int,
    retained_outgoing_requests: int,
    bundles_by_band: dict[str, int],
) -> AnalyticsEvent:
    """Construct a follow_up_reading event."""

    return AnalyticsEvent(
        family=EventFamily.FOLLOW_UP_READING,
        binding=binding,
        occurred_at=occurred_at,
        payload={
            "principal_subject": principal_subject,
            "project_id": project_id,
            "cutoff": cutoff,
            "rule_set": rule_set,
            "accepted_revision_id": accepted_revision_id,
            "reading_identity": reading_identity,
            "bundle_count": bundle_count,
            "retained_outgoing_requests": retained_outgoing_requests,
            "bundles_by_band": bundles_by_band,
        },
        metric_labels={"surface": surface, "status": "presented"},
    )


def _presentation_event(
    family: EventFamily, binding: AnalyticsBinding, *, occurred_at: datetime, project_id: int,
    principal_subject: str, **payload: Any,
) -> AnalyticsEvent:
    return AnalyticsEvent(
        family=family, binding=binding, occurred_at=occurred_at,
        payload={"project_id": project_id, "principal_subject": principal_subject, **payload},
        metric_labels={"surface": family.value},
    )


def project_opening_event(
    binding: AnalyticsBinding, *, occurred_at: datetime, project_id: int, principal_subject: str
) -> AnalyticsEvent:
    """Construct a project_opening event."""

    return _presentation_event(EventFamily.PROJECT_OPENING, binding, occurred_at=occurred_at,
                               project_id=project_id, principal_subject=principal_subject)


def coverage_reading_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    principal_subject: str,
    reading_sha256: str,
    issue_profile_identity: str | None,
    issue_profile_version: int | None,
    issue_profile_sha256: str | None,
    coverage_declaration_id: int | None,
    through_source_delivery_id: int | None,
) -> AnalyticsEvent:
    """Construct a coverage_reading event."""

    return _presentation_event(
        EventFamily.COVERAGE_READING, binding, occurred_at=occurred_at, project_id=project_id,
        principal_subject=principal_subject,
        reading_sha256=reading_sha256,
        issue_profile_identity=issue_profile_identity,
        issue_profile_version=issue_profile_version,
        issue_profile_sha256=issue_profile_sha256,
        coverage_declaration_id=coverage_declaration_id,
        through_source_delivery_id=through_source_delivery_id,
    )


def evidence_opening_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    principal_subject: str,
    item_key: str,
    delta_id: int | None,
    source_row_id: int | None,
    link_role: str,
) -> AnalyticsEvent:
    """Construct an evidence_opening event."""

    return _presentation_event(
        EventFamily.EVIDENCE_OPENING, binding, occurred_at=occurred_at, project_id=project_id,
        principal_subject=principal_subject,
        item_key=item_key, delta_id=delta_id, source_row_id=source_row_id, link_role=link_role,
    )


def _preparation_interaction_event(
    family: EventFamily, binding: AnalyticsBinding, *, occurred_at: datetime, project_id: int, receipt_id: int,
    issue_profile_identity: str | None, issue_profile_version: int | None, issue_profile_sha256: str | None,
    principal_subject: str, **payload: Any,
) -> AnalyticsEvent:
    return AnalyticsEvent(
        family=family, binding=binding, occurred_at=occurred_at,
        payload={"project_id": project_id, "receipt_id": receipt_id,
                 "issue_profile_identity": issue_profile_identity,
                 "issue_profile_version": issue_profile_version,
                 "issue_profile_sha256": issue_profile_sha256,
                 "principal_subject": principal_subject, **payload},
    )


def preparation_request_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    receipt_id: int,
    issue_profile_identity: str | None,
    issue_profile_version: int | None,
    issue_profile_sha256: str | None,
    principal_subject: str,
    request_id: int,
    coverage_declaration_id: int,
    outcome: str,
) -> AnalyticsEvent:
    """Construct a preparation_request event beside its ReleasePreparationRequest row."""

    return _preparation_interaction_event(
        EventFamily.PREPARATION_REQUEST, binding, occurred_at=occurred_at, project_id=project_id,
        receipt_id=receipt_id, issue_profile_identity=issue_profile_identity,
        issue_profile_version=issue_profile_version, issue_profile_sha256=issue_profile_sha256,
        principal_subject=principal_subject,
        request_id=request_id, coverage_declaration_id=coverage_declaration_id, outcome=outcome,
    )


def coverage_confirmation_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    receipt_id: int,
    issue_profile_identity: str | None,
    issue_profile_version: int | None,
    issue_profile_sha256: str | None,
    principal_subject: str,
    coverage_declaration_id: int,
    reading_sha256: str,
    annotation_count: int,
    unchanged_declaration_reused: bool,
) -> AnalyticsEvent:
    """Construct a coverage_confirmation event beside its IssueCoverageDeclaration row."""

    return _preparation_interaction_event(
        EventFamily.COVERAGE_CONFIRMATION, binding, occurred_at=occurred_at, project_id=project_id,
        receipt_id=receipt_id, issue_profile_identity=issue_profile_identity,
        issue_profile_version=issue_profile_version, issue_profile_sha256=issue_profile_sha256,
        principal_subject=principal_subject,
        coverage_declaration_id=coverage_declaration_id, reading_sha256=reading_sha256,
        annotation_count=annotation_count, unchanged_declaration_reused=unchanged_declaration_reused,
    )


def preparation_attempt_started_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    request_id: int,
    coverage_declaration_id: int,
    principal_subject: str,
    started_at: str,
    issue_profile_identity: str | None,
    issue_profile_version: int | None,
    issue_profile_sha256: str | None,
) -> AnalyticsEvent:
    """Construct the preparation_attempt event that marks an attempt's start."""

    return AnalyticsEvent(
        family=EventFamily.PREPARATION_ATTEMPT, binding=binding, occurred_at=occurred_at,
        payload={"project_id": project_id, "request_id": request_id,
                 "coverage_declaration_id": coverage_declaration_id,
                 "principal_subject": principal_subject, "outcome": "started",
                 "started_at": started_at,
                 "issue_profile_identity": issue_profile_identity,
                 "issue_profile_version": issue_profile_version,
                 "issue_profile_sha256": issue_profile_sha256},
    )


def preparation_attempt_finished_event(
    binding: AnalyticsBinding,
    *,
    occurred_at: datetime,
    project_id: int,
    request_id: int,
    attempt_id: int,
    outcome: str,
    candidate_id: int | None,
    refusal_code: str | None,
    started_at: str,
    coverage_declaration_id: int,
    principal_subject: str,
    issue_profile_identity: str | None,
    issue_profile_version: int | None,
    issue_profile_sha256: str | None,
) -> AnalyticsEvent:
    """Construct the preparation_attempt event that records what a finished attempt produced."""

    return AnalyticsEvent(
        family=EventFamily.PREPARATION_ATTEMPT, binding=binding, occurred_at=occurred_at,
        payload={"project_id": project_id, "request_id": request_id,
                 "attempt_id": attempt_id, "outcome": outcome,
                 "candidate_id": candidate_id, "refusal_code": refusal_code,
                 "started_at": started_at,
                 "coverage_declaration_id": coverage_declaration_id,
                 "principal_subject": principal_subject,
                 "issue_profile_identity": issue_profile_identity,
                 "issue_profile_version": issue_profile_version,
                 "issue_profile_sha256": issue_profile_sha256},
    )


def delta_supersession_event(
    binding: AnalyticsBinding,
    *,
    project_id: int,
    prior_delta_id: int,
    superseding_delta_id: int | None,
    source_reading_id: int,
    source_revision: str,
    comparison_rule_version: str,
    occurred_at: datetime | None = None,
) -> AnalyticsEvent:
    """Construct a delta_supersession event."""

    return AnalyticsEvent(
        family=EventFamily.DELTA_SUPERSESSION,
        binding=binding,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        payload={"project_id": project_id,
                 "prior_delta_id": prior_delta_id,
                 "superseding_delta_id": superseding_delta_id,
                 "source_reading_id": source_reading_id, "source_revision": source_revision,
                 "comparison_rule_version": comparison_rule_version},
    )


# --- Reading a logged payload through its declared keys ----------------------
#
# Rows logged before every family declared its shape spelled some keys
# differently. Each translation is named here, once; a reader calls these
# rather than falling back inline.

# ``outcome`` is the declared payload key for the act's outcome; older rows
# spelled it ``status``.
LEGACY_PAYLOAD_KEYS: dict[str, str] = {"status": "outcome"}

# Older child_decision rows named the act in the screen's words; the record's
# disposition vocabulary (PACKET_CHILD_OUTCOMES in models/delta.py) is the declared one.
LEGACY_CHILD_DECISION_ACTIONS: dict[str, str] = {
    "accept": "apply",
    "edit": "edit_and_apply",
    "reject": "keep_current",
}


def declared_payload(event: AnalyticsEvent) -> dict[str, Any]:
    """The event's payload with every legacy spelling also present under its declared key."""

    payload = dict(event.payload)
    for legacy, declared in LEGACY_PAYLOAD_KEYS.items():
        if legacy in payload:
            payload.setdefault(declared, payload[legacy])
    return payload


def act_outcome(event: AnalyticsEvent) -> str | None:
    """The outcome of the act this event records.

    Most families declare it as the ``outcome`` payload key.
    ``release_candidate_preparation`` and ``release_authorization`` declare it
    as their ``status`` metric label, which is also where rows logged before
    ``outcome`` was declared carried it.
    """

    return declared_payload(event).get("outcome", event.metric_labels.get("status"))


def child_decision_action(action: str | None) -> str | None:
    """A child_decision act in the record's disposition vocabulary, whichever way it was logged."""

    return LEGACY_CHILD_DECISION_ACTIONS.get(action, action)


class EventEmitter(Protocol):
    """Protocol for analytics event sinks."""

    def emit(self, event: AnalyticsEvent) -> None:
        """Emit one analytics event."""
        ...


class LoggingEventEmitter:
    """Default emitter: outputs structured JSON records to logging."""

    def emit(self, event: AnalyticsEvent) -> None:
        logger.info(
            "analytics_event",
            extra={"analytics_event": event.as_dict(),
                   "corridor_fields": {"analytics_event": event.as_dict()}},
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
