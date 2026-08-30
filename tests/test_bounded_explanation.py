"""Shared behavior for one bounded, non-authoritative explanation request."""

from dataclasses import dataclass

import pytest

from corridor.bounded_explanation import (
    BoundedExplanationPlan,
    execute_bounded_explanation,
)


@dataclass(frozen=True)
class Configuration:
    max_input_tokens: int
    max_output_tokens: int = 100
    timeout_seconds: int = 30
    max_requests: int = 1
    retry_policy: str = "none"


class Client:
    adapter = "recording"
    adapter_contract_version = "recording-v1"
    last_usage = {"input_tokens": 4, "output_tokens": 2}

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def complete(self, *, system, user, schema):
        if self.error is not None:
            raise self.error
        return self.result


def _plan(client, *, is_current=lambda: True, validate=lambda value: (value, None)):
    return BoundedExplanationPlan(
        configuration=Configuration(max_input_tokens=100),
        client_factory=lambda _configuration: client,
        system_prompt="system",
        user_message="user",
        schema={"type": "object"},
        is_current=is_current,
        stale_reason="the frozen input changed",
        validate=validate,
    )


def test_input_budget_refuses_before_an_external_adapter_exists():
    adapter_created = False

    def client_factory(_configuration):
        nonlocal adapter_created
        adapter_created = True
        raise AssertionError("an over-budget request must not create an adapter")

    outcome = execute_bounded_explanation(
        BoundedExplanationPlan(
            configuration=Configuration(max_input_tokens=1),
            client_factory=client_factory,
            system_prompt="system prompt that exceeds one token",
            user_message="bounded input",
            schema={"type": "object"},
            is_current=lambda: True,
            stale_reason="the frozen input changed",
            validate=lambda payload: (payload, None),
        )
    )

    assert adapter_created is False
    assert outcome.status == "budget_exhausted"
    assert outcome.output_json is None
    assert outcome.execution_lineage_json is None
    assert outcome.usage_json["estimated_input_tokens"] > 1


def test_completed_request_retains_validated_output_lineage_and_usage():
    outcome = execute_bounded_explanation(
        _plan(
            Client({"value": "observed"}),
            validate=lambda value: ({"kept": value["value"]}, None),
        )
    )

    assert outcome.status == "completed"
    assert outcome.output_json == {"kept": "observed"}
    assert outcome.execution_lineage_json["adapter"] == "recording"
    assert len(outcome.execution_lineage_json["request_sha256"]) == 64
    assert len(outcome.execution_lineage_json["result_sha256"]) == 64
    assert outcome.usage_json["reported"] == Client.last_usage


def test_changed_input_is_refused_after_the_external_request():
    outcome = execute_bounded_explanation(
        _plan(Client({"value": "observed"}), is_current=lambda: False)
    )

    assert outcome.status == "stale_input"
    assert outcome.reason == "the frozen input changed"
    assert outcome.output_json is None
    assert outcome.execution_lineage_json is not None


def test_domain_validator_owns_the_terminal_validation_refusal():
    outcome = execute_bounded_explanation(
        _plan(
            Client({"value": "unsupported"}),
            validate=lambda _value: (None, "unsupported explanation"),
        )
    )

    assert outcome.status == "validation_refused"
    assert outcome.reason == "unsupported explanation"
    assert outcome.output_json is None


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (TimeoutError("late"), "timeout"),
        (RuntimeError("offline"), "transport_failure"),
    ],
)
def test_external_failures_are_terminal_receipts(error, status):
    outcome = execute_bounded_explanation(_plan(Client(error=error)))

    assert outcome.status == status
    assert outcome.output_json is None
    assert outcome.execution_lineage_json is None
