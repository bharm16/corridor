"""Tests for the versioned product-analytics event and receipt contract (#558)."""

from datetime import datetime, timezone
import pytest

from corridor.analytics import (
    AnalyticsBinding,
    AnalyticsEvent,
    DERIVABLE_RECEIPT_ALTERNATIVES,
    DEFAULT_EVENT_VERSION,
    EventFamily,
    HighCardinalityMetricError,
    InMemoryEventCollector,
    InvalidEventError,
    capture_events,
    default_binding,
    emit_event,
    source_arrival_event,
    source_capture_event,
    validate_metric_labels,
)
from corridor.source_intake import validate_and_stage


def test_every_event_family_has_stated_derivable_receipt_alternative() -> None:
    """Every event family names its stated derivable-receipt alternative."""
    assert len(EventFamily) == 14
    for family in EventFamily:
        alternative = DERIVABLE_RECEIPT_ALTERNATIVES.get(family)
        assert alternative is not None, f"missing derivable receipt alternative for {family}"
        assert len(alternative) > 10


def test_analytics_binding_carries_mandatory_context() -> None:
    """Every binding binds code, product, rules, templates, and flags."""
    binding = default_binding(
        code_revision="git:abc1234",
        product_revision="ADR-0086-v1",
        packetizer_rules_version="packetizer-v2",
        source_configuration={"format": "matrix_pdf"},
        connector_configuration={"type": "box"},
        template_identity="template-ohio-dot",
        mapping_identity="mapping-standard",
        enabled_feature_flags=("flag_delta_preview",),
    )
    data = binding.as_dict()
    assert data["code_revision"] == "git:abc1234"
    assert data["product_revision"] == "ADR-0086-v1"
    assert data["packetizer_rules_version"] == "packetizer-v2"
    assert data["source_configuration"] == {"format": "matrix_pdf"}
    assert data["connector_configuration"] == {"type": "box"}
    assert data["template_identity"] == "template-ohio-dot"
    assert data["mapping_identity"] == "mapping-standard"
    assert data["enabled_feature_flags"] == ["flag_delta_preview"]

    roundtrip = AnalyticsBinding.from_dict(data)
    assert roundtrip == binding


def test_event_round_trip_serialization() -> None:
    """An analytics event round-trips through dictionary serialization."""
    binding = default_binding()
    event = source_arrival_event(
        binding,
        customer_id="cust-123",
        project_id=42,
        channel="email",
        filename="plan.pdf",
        content_sha256="deadbeef" * 8,
        byte_count=1024,
    )

    data = event.as_dict()
    assert data["family"] == "source_arrival"
    assert data["version"] == DEFAULT_EVENT_VERSION
    assert data["payload"]["customer_id"] == "cust-123"
    assert data["payload"]["project_id"] == 42
    assert "derivable_receipt_alternative" in data

    reconstructed = AnalyticsEvent.from_dict(data)
    assert reconstructed.event_id == event.event_id
    assert reconstructed.family == event.family
    assert reconstructed.version == event.version
    assert reconstructed.binding == event.binding
    assert reconstructed.payload == event.payload
    assert reconstructed.metric_labels == event.metric_labels


def test_unknown_or_unversioned_event_is_refused() -> None:
    """Events without a version or with an unknown family must fail validation."""
    binding = default_binding()

    with pytest.raises(InvalidEventError, match="non-empty string version"):
        AnalyticsEvent(
            family=EventFamily.SOURCE_ARRIVAL,
            payload={},
            binding=binding,
            version="",
        )

    with pytest.raises(InvalidEventError, match="unknown event family"):
        AnalyticsEvent(
            family="not_a_real_family",  # type: ignore[arg-type]
            payload={},
            binding=binding,
            version="1.0",
        )


def test_low_cardinality_rule_refuses_customer_and_project_metric_labels() -> None:
    """Customer and project identifiers must never appear as metric labels."""
    binding = default_binding()

    # Valid metric labels
    validate_metric_labels({"channel": "upload", "status": "ok"})

    # Forbidden label key: customer_id
    with pytest.raises(HighCardinalityMetricError, match="carries high-cardinality identity"):
        validate_metric_labels({"customer_id": "cust-999"})

    # Forbidden label key: project_id
    with pytest.raises(HighCardinalityMetricError, match="carries high-cardinality identity"):
        validate_metric_labels({"project_id": "proj-1"})

    # Forbidden label token in name
    with pytest.raises(HighCardinalityMetricError, match="forbidden identity token"):
        validate_metric_labels({"sub_project_name": "highway-22"})

    # In event constructor
    with pytest.raises(HighCardinalityMetricError):
        AnalyticsEvent(
            family=EventFamily.SOURCE_ARRIVAL,
            payload={},
            binding=binding,
            metric_labels={"project_id": "123"},
        )


def test_source_intake_emits_arrival_and_capture_events() -> None:
    """validate_and_stage emits source_arrival and source_capture events."""
    pdf_bytes = b"%PDF-1.4 header and minimal content for test"

    with capture_events() as collector:
        staged = validate_and_stage(
            pdf_bytes,
            "matrix.pdf",
            customer_id="cust-pilot-1",
            project_id=101,
            channel="test_upload",
        )

        assert len(collector.events) == 2
        arrival, capture = collector.events

        assert arrival.family == EventFamily.SOURCE_ARRIVAL
        assert arrival.payload["customer_id"] == "cust-pilot-1"
        assert arrival.payload["project_id"] == 101
        assert arrival.payload["channel"] == "test_upload"
        assert arrival.payload["filename"] == "matrix.pdf"
        assert arrival.payload["content_sha256"] == staged.sha256

        assert capture.family == EventFamily.SOURCE_CAPTURE
        assert capture.payload["customer_id"] == "cust-pilot-1"
        assert capture.payload["project_id"] == 101
        assert capture.payload["content_sha256"] == staged.sha256
        assert capture.payload["storage_key"].endswith(f"{staged.sha256}.pdf")
