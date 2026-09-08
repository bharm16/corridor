"""The only authorized path from Corridor to a native-matrix model call (#447).

Every refusal test hands the boundary a transport that fails the test if it is
ever asked to send anything: a refusal that still opened a socket is not a
refusal. The authorized tests use a recording transport, so nothing here needs
a network or a key.
"""

from hashlib import sha256
import json
from pathlib import Path

import pytest

from corridor.native_provider_boundary import (
    EXPERIMENT_STAGE, NATIVE_MATRIX_PURPOSE, POSTURE, POSTURE_PATH,
    Budget, CustomerAuthorization, ExperimentScope, NativeProviderRefused,
    RequestBoundary, TransportOutcome, open_native_provider_boundary,
)


SOURCE = "a" * 64
OTHER_SOURCE = "b" * 64
SCHEMA = {"type": "object", "properties": {"rows": {"type": "array", "items": {"type": "string"}}},
          "required": ["rows"], "additionalProperties": False}


def _image(tmp_path: Path, name: str = "page-1.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
    return path


def _experiment(**overrides) -> ExperimentScope:
    values = dict(
        record_id="wsdot-public-utility-listings-2026-09-08",
        dataset="wsdot-public-utility-listings",
        dataset_digest="c" * 64,
        purpose=NATIVE_MATRIX_PURPOSE,
        scope="Public WSDOT utility listings; no customer material",
        source_classes=frozenset({"native_matrix"}),
        source_sha256s=frozenset({SOURCE}),
        max_calls=4,
        max_pages=4,
        max_total_tokens=100_000,
        posture_identity=POSTURE.identity,
        posture_digest=POSTURE.digest,
        recorded_by="local:bharm16",
        recorded_on="2026-09-08",
    )
    values.update(overrides)
    return ExperimentScope(**values)


def _request(**overrides) -> RequestBoundary:
    values = dict(
        project="wsdot-public-utility-listings", source_class="native_matrix",
        purpose=NATIVE_MATRIX_PURPOSE, stage=EXPERIMENT_STAGE,
        posture_identity=POSTURE.identity, model=POSTURE.model,
        reasoning_effort="none", store=False, base_url=POSTURE.base_url,
        image_detail="original", image_dpi=110,
    )
    values.update(overrides)
    return RequestBoundary(**values)


class ForbiddenTransport:
    """Fails the test on any outbound request."""

    def send(self, payload):
        raise AssertionError("a refused boundary must send nothing")


class RecordingTransport:
    """Canned Responses API outcomes, with every payload it was handed."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.payloads = []

    def send(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        if not self.outcomes:
            raise AssertionError("the boundary sent more requests than the test prepared")
        return self.outcomes.pop(0)


def _body(answer, *, response_id="resp_1", cached=0, input_tokens=120, output_tokens=30):
    return TransportOutcome(status=200, body={
        "id": response_id, "status": "completed",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "input_tokens_details": {"cached_tokens": cached}},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
    })


_DEFAULT = object()


def _open(transport, *, record=_DEFAULT, request=None, budget=None, sources=(SOURCE,)):
    return open_native_provider_boundary(
        _experiment() if record is _DEFAULT else record,
        _request() if request is None else request,
        transport=transport,
        budget=Budget(max_calls=2, max_pages=2, max_total_tokens=10_000) if budget is None else budget,
        source_sha256s=frozenset(sources),
        campaign="native-matrix-qualification-2026-09-08",
    )


def test_the_posture_digest_is_the_recorded_document_bytes():
    assert sha256(POSTURE_PATH.read_bytes()).hexdigest() == POSTURE.digest
    assert POSTURE.model == "gpt-5.6-luna" and POSTURE.reasoning_effort == "none"
    assert POSTURE.store is False and POSTURE.customer_processing == "blocked"


def test_an_absent_record_refuses_with_zero_outbound_requests():
    with pytest.raises(NativeProviderRefused) as refused:
        _open(ForbiddenTransport(), record=None)
    assert refused.value.reason == "authorization-absent"
    assert refused.value.outbound_requests == 0
    assert any("authorization-absent" in item for item in refused.value.mismatches)


def test_a_refusal_names_every_failing_field_not_only_the_first():
    request = _request(purpose="minutes-prose-extraction", source_class="native_minutes",
                       model="some-other-model", reasoning_effort="high", store=True)
    with pytest.raises(NativeProviderRefused) as refused:
        _open(ForbiddenTransport(), request=request)
    reported = " | ".join(refused.value.mismatches)
    for field in ("purpose", "source-class", "model", "reasoning-effort", "store"):
        assert field in reported, reported
    assert refused.value.outbound_requests == 0


def test_customer_material_is_refused_while_the_posture_blocks_customer_processing():
    record = CustomerAuthorization(
        record_id="signed-2026-09-08", customer="Example DOT",
        projects=frozenset({"wsdot-public-utility-listings"}),
        source_classes=frozenset({"native_matrix"}), purposes=frozenset({NATIVE_MATRIX_PURPOSE}),
        stages=frozenset({"shadow"}), source_sha256s=frozenset({SOURCE}),
        max_calls=4, max_pages=4, max_total_tokens=100_000,
        posture_identity=POSTURE.identity, posture_digest=POSTURE.digest,
        retention_disclosed=True, signed_by="an actual signatory", signed_on="2026-09-08",
    )
    with pytest.raises(NativeProviderRefused) as refused:
        _open(ForbiddenTransport(), record=record, request=_request(stage="shadow"))
    reported = " | ".join(refused.value.mismatches)
    assert "posture-customer-processing" in reported and "posture-pdf-licensing" in reported
    assert refused.value.outbound_requests == 0


def test_an_experiment_scope_cannot_authorize_a_customer_stage():
    with pytest.raises(NativeProviderRefused) as refused:
        _open(ForbiddenTransport(), request=_request(stage="authoritative"))
    assert "stage" in " | ".join(refused.value.mismatches)


def test_a_source_outside_the_declared_digest_allowlist_is_refused():
    with pytest.raises(NativeProviderRefused) as refused:
        _open(ForbiddenTransport(), sources=(SOURCE, OTHER_SOURCE))
    assert "source-digests" in " | ".join(refused.value.mismatches)
    with pytest.raises(NativeProviderRefused):
        _open(ForbiddenTransport(), sources=())


def test_a_budget_wider_than_the_record_is_refused():
    with pytest.raises(NativeProviderRefused) as refused:
        _open(ForbiddenTransport(), budget=Budget(max_calls=99, max_pages=2, max_total_tokens=10))
    assert "budget-max-calls" in " | ".join(refused.value.mismatches)


def test_an_authorized_call_records_request_response_attempts_usage_and_cache(tmp_path):
    transport = RecordingTransport([_body({"rows": ["one"]}, cached=64)])
    boundary = _open(transport)
    answer = boundary.complete(system="rules", user="listing", schema=SCHEMA, images=[_image(tmp_path)])
    assert answer == {"rows": ["one"]}
    receipt = boundary.last_call_receipt()
    assert receipt["transport_attempts"] == 1 and receipt["response_id"] == "resp_1"
    assert receipt["usage"] == {"input_tokens": 120, "output_tokens": 30, "cached_tokens": 64}
    assert receipt["cached"] is False and receipt["pages"] == 1
    assert len(receipt["request_sha256"]) == 64 and len(receipt["response_sha256"]) == 64
    sent = transport.payloads[0]
    assert sent["model"] == POSTURE.model and sent["store"] is False
    assert sent["reasoning"] == {"effort": "none"}
    assert sent["text"]["format"]["strict"] is True
    assert boundary.usage.prompt_tokens == 120 and boundary.usage.cached_tokens == 64
    cost = boundary.cost_receipt()
    assert cost["counts"] == {"calls": 1, "retries": 0, "failed_attempts": 0,
                              "outbound_requests": 1, "pages_requested": 1, "refusals": 0}
    assert cost["usd"] is None and "not recorded" in cost["usd_note"]
    assert cost["authorization"]["record_id"] == "wsdot-public-utility-listings-2026-09-08"


def test_a_retryable_status_counts_an_attempt_and_not_a_second_call(tmp_path):
    transport = RecordingTransport([
        TransportOutcome(status=429, body=None, retry_after=0.0, error="rate limited"),
        _body({"rows": ["one"]}),
    ])
    boundary = _open(transport)
    boundary.complete(system="rules", user="listing", schema=SCHEMA, images=[_image(tmp_path)])
    receipt = boundary.last_call_receipt()
    assert receipt["transport_attempts"] == 2
    cost = boundary.cost_receipt()
    assert cost["counts"]["calls"] == 1 and cost["counts"]["retries"] == 1
    assert cost["counts"]["outbound_requests"] == 2


def test_the_budget_refuses_the_next_call_before_it_leaves_the_process(tmp_path):
    transport = RecordingTransport([_body({"rows": ["one"]}), _body({"rows": ["two"]})])
    boundary = _open(transport, budget=Budget(max_calls=1, max_pages=2, max_total_tokens=10_000))
    boundary.complete(system="rules", user="listing", schema=SCHEMA, images=[_image(tmp_path)])
    with pytest.raises(NativeProviderRefused) as refused:
        boundary.complete(system="rules", user="listing", schema=SCHEMA, images=[_image(tmp_path, "page-2.png")])
    assert refused.value.reason == "budget-exhausted"
    assert len(transport.payloads) == 1
    assert boundary.cost_receipt()["counts"]["refusals"] == 1


def test_a_token_budget_stops_the_campaign_after_the_call_that_crossed_it(tmp_path):
    transport = RecordingTransport([_body({"rows": ["one"]}, input_tokens=900, output_tokens=200)])
    boundary = _open(transport, budget=Budget(max_calls=4, max_pages=4, max_total_tokens=1_000))
    boundary.complete(system="rules", user="listing", schema=SCHEMA, images=[_image(tmp_path)])
    with pytest.raises(NativeProviderRefused, match="budget"):
        boundary.complete(system="rules", user="listing", schema=SCHEMA, images=[_image(tmp_path, "page-2.png")])
    assert len(transport.payloads) == 1


def test_the_boundary_exposes_the_configuration_the_extractor_seals():
    boundary = _open(RecordingTransport([]))
    from corridor.extractor_lineage import deployed_native_matrix_config

    config = deployed_native_matrix_config(client=boundary)
    assert config.model == "gpt-5.6-luna"
    assert config.config_json["request_controls"]["reasoning_effort"] == "none"
    assert boundary.flex is False and boundary.image_detail == "original"
