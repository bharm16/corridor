"""Declared assistance spend governs the actual web factory's HTTP attempts."""

from types import SimpleNamespace

import httpx
import pytest

from corridor.web.app import (
    get_coordination_summary_client_factory,
    get_failure_diagnosis_client_factory,
    get_intake_draft_client_factory,
    get_revision_change_explanation_client_factory,
    get_run_explanation_client_factory,
)


FACTORIES = (
    get_coordination_summary_client_factory,
    get_run_explanation_client_factory,
    get_failure_diagnosis_client_factory,
    get_revision_change_explanation_client_factory,
    get_intake_draft_client_factory,
)


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda factory: factory.__name__)
@pytest.mark.parametrize("failure", ("status", "transport"))
def test_assistance_factory_never_retries_authorized_single_request(
    monkeypatch, factory, failure
):
    attempts, sleeps = [], []

    def respond(transport, request):
        attempts.append(request)
        if failure == "transport":
            raise httpx.ReadTimeout("response unavailable", request=request)
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    monkeypatch.setattr("corridor.llm.settings.openai_api_key", "test-only")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    monkeypatch.setattr("corridor.llm.time.sleep", sleeps.append)
    configuration = SimpleNamespace(
        model="test-model", timeout_seconds=7, max_output_tokens=123,
        max_requests=1, retry_policy="none",
    )
    with factory()(configuration) as client:
        with pytest.raises(RuntimeError, match="failed after"):
            client.complete(system="test", user="test", schema={"type": "object"})

    assert len(attempts) == 1
    assert sleeps == []


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda factory: factory.__name__)
def test_assistance_factory_preserves_declared_request_limits_on_success(monkeypatch, factory):
    import json

    requests = []

    def respond(transport, request):
        requests.append(request)
        return httpx.Response(200, json={
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
        })

    monkeypatch.setattr("corridor.llm.settings.openai_api_key", "test-only")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    configuration = SimpleNamespace(
        model="test-model", timeout_seconds=7, max_output_tokens=123,
        max_requests=1, retry_policy="none",
    )
    with factory()(configuration) as client:
        assert client.complete(system="test", user="test", schema={"type": "object"}) == {}
    assert len(requests) == 1
    assert json.loads(requests[0].content)["max_output_tokens"] == 123
    assert requests[0].extensions["timeout"]["read"] == 7
