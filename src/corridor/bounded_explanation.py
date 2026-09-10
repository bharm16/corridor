"""Run one frozen, budgeted, non-authoritative explanation request.

Production-run explanations, revision-change explanations, and extraction-failure
diagnoses all need the same cage: refuse before spend when input exceeds the
declared budget, perform one external request with no retry, re-check the frozen
reading afterward, validate the structured result deterministically, and retain
redacted execution lineage.  Their source snapshots, schemas, validators, stale
reasons, and durable receipt rows remain domain-specific.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from corridor import digests


@dataclass(frozen=True)
class BoundedExplanationOutcome:
    """One terminal result ready for a domain-specific durable receipt."""

    adapter: str
    adapter_contract_version: str | None
    status: str
    reason: str | None
    output_json: dict | None
    execution_lineage_json: dict | None
    usage_json: dict


@dataclass(frozen=True)
class BoundedExplanationPlan:
    """The frozen variation points for one bounded explanation execution."""

    configuration: Any
    client_factory: Callable[[Any], object]
    system_prompt: str
    user_message: str
    schema: dict
    is_current: Callable[[], bool]
    stale_reason: str
    validate: Callable[[object], tuple[dict | None, str | None]]


def sanitize_text(value: object, *, max_len: int = 2_000) -> str:
    """Strip control characters and bound retained or rendered text."""
    text = "" if value is None else str(value)
    text = "".join(
        character
        for character in text
        if character in "\n\t" or (character >= " " and character != "\x7f")
    )
    return text.strip()[:max_len]


def canonical_json(payload: object) -> str:
    """The retained serialization of a receipt or an untrusted snapshot.

    `corridor.digests.coerced_json` owns the encoding; explanation callers
    hand the result to a model and to a receipt column as text.
    """
    return digests.coerced_json(payload).decode()


content_sha256 = digests.coerced_sha256


def budget_snapshot(configuration: object) -> dict:
    """The common declared request bounds stored on every explanation receipt."""
    return {
        "max_input_tokens": configuration.max_input_tokens,
        "max_output_tokens": configuration.max_output_tokens,
        "timeout_seconds": configuration.timeout_seconds,
        "max_requests": configuration.max_requests,
        "retry_policy": configuration.retry_policy,
    }


def execute_bounded_explanation(
    plan: BoundedExplanationPlan,
) -> BoundedExplanationOutcome:
    """Execute one declared request and return exactly one terminal outcome."""
    estimated_input_tokens = (
        len(plan.system_prompt) + len(plan.user_message) + 3
    ) // 4
    initial_usage = {"estimated_input_tokens": estimated_input_tokens}
    if estimated_input_tokens > plan.configuration.max_input_tokens:
        return BoundedExplanationOutcome(
            adapter="none",
            adapter_contract_version=None,
            status="budget_exhausted",
            reason=(
                f"input estimate {estimated_input_tokens} exceeds declared "
                f"budget {plan.configuration.max_input_tokens}; no model call was made"
            ),
            output_json=None,
            execution_lineage_json=None,
            usage_json=initial_usage,
        )

    client = plan.client_factory(plan.configuration)
    adapter = sanitize_text(
        getattr(client, "adapter", type(client).__name__), max_len=64
    )
    adapter_contract_version = getattr(client, "adapter_contract_version", None)
    if adapter_contract_version is not None:
        adapter_contract_version = sanitize_text(
            adapter_contract_version, max_len=128
        )
    started = time.monotonic()
    try:
        result = client.complete(
            system=plan.system_prompt,
            user=plan.user_message,
            schema=plan.schema,
        )
    except TimeoutError as exc:
        return BoundedExplanationOutcome(
            adapter=adapter,
            adapter_contract_version=adapter_contract_version,
            status="timeout",
            reason=f"model request exceeded declared time budget: {exc}",
            output_json=None,
            execution_lineage_json=None,
            usage_json=initial_usage,
        )
    except Exception as exc:  # external adapter failures are terminal receipts
        return BoundedExplanationOutcome(
            adapter=adapter,
            adapter_contract_version=adapter_contract_version,
            status="transport_failure",
            reason=f"model transport failed: {type(exc).__name__}: {exc}",
            output_json=None,
            execution_lineage_json=None,
            usage_json=initial_usage,
        )

    lineage = {
        "adapter": adapter,
        "adapter_contract_version": adapter_contract_version,
        "request_sha256": content_sha256([plan.system_prompt, plan.user_message]),
        "result_sha256": content_sha256(result),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    reported_usage = getattr(client, "last_usage", None)
    usage = {
        "estimated_input_tokens": estimated_input_tokens,
        "reported": reported_usage if isinstance(reported_usage, dict) else {},
    }
    if not plan.is_current():
        return BoundedExplanationOutcome(
            adapter=adapter,
            adapter_contract_version=adapter_contract_version,
            status="stale_input",
            reason=plan.stale_reason,
            output_json=None,
            execution_lineage_json=lineage,
            usage_json=usage,
        )

    validated, error = plan.validate(result)
    if error is not None:
        return BoundedExplanationOutcome(
            adapter=adapter,
            adapter_contract_version=adapter_contract_version,
            status="validation_refused",
            reason=error,
            output_json=None,
            execution_lineage_json=lineage,
            usage_json=usage,
        )
    return BoundedExplanationOutcome(
        adapter=adapter,
        adapter_contract_version=adapter_contract_version,
        status="completed",
        reason=None,
        output_json=validated,
        execution_lineage_json=lineage,
        usage_json=usage,
    )
