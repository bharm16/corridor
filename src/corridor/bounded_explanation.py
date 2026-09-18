"""Run one frozen, budgeted, non-authoritative explanation request.

Production-run explanations, revision-change explanations, extraction-failure
diagnoses, and source-intake drafts all need the same cage: refuse before spend when input exceeds the
declared budget, perform one external request with no retry, re-check the frozen
reading afterward, validate the structured result deterministically, and retain
redacted execution lineage.  Their source snapshots, validators, stale
reasons, and durable receipt rows remain domain-specific.

The prompt arrives as the identity `prompt_library` resolved -- version, exact
bytes, digest and the output schema those bytes promise -- rather than as a
`str` beside an unrelated `schema` dict.  Four families used to read the file
themselves and pass the two separately, so nothing said which schema a given
prompt version promised and no digest was taken at all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Any, Callable

from corridor import digests
from corridor.prompt_library import Prompt
from corridor.llm import Usage


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
    prompt: Prompt
    user_message: str
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


def execute_assistance_request(
    *, configuration: Any, client_factory: Callable[[Any], object],
    prompt: Prompt, user_message: str,
) -> BoundedExplanationOutcome:
    """Own one adapter's lifetime, actual usage and terminal transport outcome.

    Summary and explanation requests share this operation. Their factual
    validation remains with the consumer. Construction and cleanup used to sit
    outside the receipt path, and a fake-only usage attribute hid real spend.
    """
    estimated = (len(prompt.text) + len(user_message) + 3) // 4
    usage = {"estimated_input_tokens": estimated}
    if estimated > configuration.max_input_tokens:
        return BoundedExplanationOutcome(
            "none", None, "budget_exhausted",
            f"input estimate {estimated} exceeds declared budget {configuration.max_input_tokens}; no model call was made",
            None, None, usage,
        )
    client = None
    adapter, version = "none", None
    result = None
    status, reason = "completed", None
    started = time.monotonic()
    try:
        client = client_factory(configuration)
        adapter = sanitize_text(getattr(client, "adapter", type(client).__name__), max_len=64)
        version = getattr(client, "adapter_contract_version", None)
        if version is not None:
            version = sanitize_text(version, max_len=128)
        result = client.complete(system=prompt.text, user=user_message, schema=prompt.schema)
    except TimeoutError as exc:
        status, reason = "timeout", f"model request exceeded declared time budget: {exc}"
    except Exception as exc:
        status, reason = "transport_failure", f"model transport failed: {type(exc).__name__}: {exc}"
    finally:
        counters = getattr(client, "usage", None)
        usage["reported"] = counters.as_dict() if isinstance(counters, Usage) else {}
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                if status == "completed":
                    status, reason = "transport_failure", f"model adapter cleanup failed: {type(exc).__name__}: {exc}"
    lineage = None
    if status == "completed":
        lineage = {
            "adapter": adapter, "adapter_contract_version": version,
            "request_sha256": content_sha256([prompt.text, user_message]),
            "result_sha256": content_sha256(result),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    return BoundedExplanationOutcome(
        adapter, version, status, reason, result if status == "completed" else None,
        lineage, usage,
    )


def execute_bounded_explanation(plan: BoundedExplanationPlan) -> BoundedExplanationOutcome:
    """Execute once, then check freshness and validate the retained answer."""
    outcome = execute_assistance_request(
        configuration=plan.configuration, client_factory=plan.client_factory,
        prompt=plan.prompt, user_message=plan.user_message,
    )
    if outcome.status != "completed":
        return outcome
    if not plan.is_current():
        return replace(outcome, status="stale_input", reason=plan.stale_reason, output_json=None)
    validated, error = plan.validate(outcome.output_json)
    if error is not None:
        return replace(outcome, status="validation_refused", reason=error, output_json=None)
    return replace(outcome, output_json=validated)
