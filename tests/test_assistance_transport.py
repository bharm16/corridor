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


@pytest.mark.parametrize('failure, expected', [(None, 'completed'), ('timeout', 'timeout'), ('invalid', 'transport_failure')])
def test_real_assistance_execution_retains_usage_and_closes_the_transport(monkeypatch, failure, expected):
    from hashlib import sha256
    from pathlib import Path
    from corridor.bounded_explanation import BoundedExplanationPlan, execute_bounded_explanation
    from corridor.prompt_library import Prompt

    attempts, closed = [], []
    def respond(transport, request):
        attempts.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('unavailable', request=request)
        return httpx.Response(200, json={
            'usage': {'input_tokens': 7, 'output_tokens': 3},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'bad-json' if failure else '{}'}]}],
        })
    monkeypatch.setattr('corridor.llm.settings.openai_api_key', 'test-only')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', respond)
    monkeypatch.setattr(httpx.HTTPTransport, 'close', lambda transport: closed.append(transport))
    configuration = SimpleNamespace(model='test-model', timeout_seconds=1, max_input_tokens=100,
        max_output_tokens=100, max_requests=1, retry_policy='none')
    outcome = execute_bounded_explanation(BoundedExplanationPlan(
        configuration=configuration, client_factory=get_run_explanation_client_factory(),
        prompt=Prompt('test', Path('unused'), b'system', sha256(b'system').hexdigest(), {'type': 'object'}),
        user_message='user', is_current=lambda: True, stale_reason='stale', validate=lambda result: (result, None),
    ))
    assert outcome.status == expected
    assert len(attempts) == 1
    assert closed
    if failure != 'timeout':
        assert outcome.usage_json['reported']['input_tokens'] == 7
        assert outcome.usage_json['reported']['output_tokens'] == 3
