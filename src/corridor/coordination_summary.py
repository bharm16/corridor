"""Explicit, bounded Coordination Summary drafting receipts (#355).

``briefing`` already owns the factual citation and complete-alert-coverage
contract.  This module deliberately adds only the product boundary missing
from that command-line view: a human explicitly asks, a server-owned
configuration names the exact permitted spend, and one immutable receipt
records either the checked AI draft or the reason no draft was shown.

It is not a queue, worker, notification path, project writer, or review
authority.  A request runs synchronously and exactly once for one frozen
project reading; refreshing a read route never invokes it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.briefing import (
    Briefing,
    PROMPT,
    PROMPT_VERSION,
    Sentence,
    _assemble,
    _check,
    _user_message,
    render,
)
from corridor.models import (
    CoordinationSummaryConfiguration,
    CoordinationSummaryRequest,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_reading import freeze_project_reading


class ConfigurationRequired(ValueError):
    """No declared, complete server configuration permits a model request."""


class InvalidSummaryConfiguration(ValueError):
    """A configuration would permit an ambiguous or unbounded request."""


_SOURCE_SCOPES = frozenset({"all_sources", "documents_only"})
_RETRY_POLICY = "none"
_RETENTION_POLICY = "retained_indefinitely"
_OBSERVATION_CONTEXT = "internal_working_view"


def _latest_configuration(
    session: Session, project_id: int
) -> CoordinationSummaryConfiguration | None:
    return session.scalars(
        select(CoordinationSummaryConfiguration)
        .where(CoordinationSummaryConfiguration.project_id == project_id)
        .order_by(CoordinationSummaryConfiguration.id.desc())
    ).first()


def declare_configuration(
    session: Session,
    *,
    project_id: int,
    principal: HumanPrincipal,
    model: str,
    prompt_version: str,
    source_scope: str,
    max_input_tokens: int,
    max_output_tokens: int,
    timeout_seconds: int,
    max_requests: int,
    retry_policy: str,
    retention_policy: str,
    observation_context: str,
) -> CoordinationSummaryConfiguration:
    """Append the complete declaration required before summary spend.

    There is intentionally no environment, credential, or previous-request
    fallback.  A changed bound is another attributable configuration, not an
    edit of an earlier receipt's authority.
    """
    require_human_principal(principal)
    text_values = {
        "model": model,
        "prompt_version": prompt_version,
        "source_scope": source_scope,
        "retry_policy": retry_policy,
        "retention_policy": retention_policy,
        "observation_context": observation_context,
    }
    if any(
        not isinstance(value, str) or not value.strip()
        for value in text_values.values()
    ):
        raise InvalidSummaryConfiguration(
            "every summary configuration field must be declared"
        )
    if source_scope not in _SOURCE_SCOPES:
        raise InvalidSummaryConfiguration(
            "source scope must be all_sources or documents_only"
        )
    if prompt_version != PROMPT_VERSION:
        raise InvalidSummaryConfiguration(
            "the configured prompt is not the installed Coordination Summary prompt"
        )
    if retry_policy != _RETRY_POLICY or max_requests != 1:
        raise InvalidSummaryConfiguration(
            "Coordination Summary permits one request and no automatic retry"
        )
    if retention_policy != _RETENTION_POLICY:
        raise InvalidSummaryConfiguration(
            "retention must be declared as retained_indefinitely"
        )
    if observation_context != _OBSERVATION_CONTEXT:
        raise InvalidSummaryConfiguration(
            "observation context must be internal_working_view"
        )
    if not 1 <= max_input_tokens <= 200_000:
        raise InvalidSummaryConfiguration(
            "input budget must be between 1 and 200000 tokens"
        )
    if not 1 <= max_output_tokens <= 20_000:
        raise InvalidSummaryConfiguration(
            "output budget must be between 1 and 20000 tokens"
        )
    if not 1 <= timeout_seconds <= 600:
        raise InvalidSummaryConfiguration(
            "time budget must be between 1 and 600 seconds"
        )
    configuration = CoordinationSummaryConfiguration(
        project_id=project_id,
        model=model.strip(),
        prompt_version=prompt_version,
        source_scope=source_scope,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        max_requests=max_requests,
        retry_policy=retry_policy,
        retention_policy=retention_policy,
        observation_context=observation_context,
        created_by=principal.subject,
    )
    session.add(configuration)
    session.flush()
    return configuration


def current_configuration(
    session: Session, project_id: int
) -> CoordinationSummaryConfiguration | None:
    """Read the latest declared authority; absence is deliberately not a default."""
    return _latest_configuration(session, project_id)


def _citable_payload(citable) -> dict:
    data = asdict(citable)
    data["covers"] = list(data["covers"])
    return data


def _reading_payload(
    reading, configuration, citables, floor, user_message: str
) -> dict:
    publication_fingerprint = sha256(
        json.dumps(
            asdict(reading.statement_publication.fingerprint),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "project_id": reading.project.id,
        "dependency_ids": list(reading.dependency_ids),
        "evaluated_on": reading.evaluation.today.isoformat(),
        "ruleset_version": reading.evaluation.ruleset_version,
        "thresholds": asdict(reading.evaluation.thresholds),
        "statement_publication_fingerprint": publication_fingerprint,
        "provenance_mode": "documents_only"
        if configuration.source_scope == "documents_only"
        else "all_sources",
        "configuration_id": configuration.id,
        "model": configuration.model,
        "prompt_version": configuration.prompt_version,
        "observation_context": configuration.observation_context,
        "required_alert_floor": list(floor),
        "citables": [_citable_payload(citable) for citable in citables],
        # This is the exact, bounded model-visible reading, not a later
        # reconstruction from current project state.
        "prompt_user": user_message,
    }


def _reading_sha256(payload: dict) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _receipt(
    *,
    project_id: int,
    configuration: CoordinationSummaryConfiguration,
    principal: HumanPrincipal,
    payload: dict,
    reading_sha256: str,
    status: str,
    reason: str | None,
    summary_markdown: str | None,
) -> CoordinationSummaryRequest:
    return CoordinationSummaryRequest(
        public_id=str(uuid4()),
        project_id=project_id,
        configuration_id=configuration.id,
        requested_by=principal.subject,
        reading_sha256=reading_sha256,
        project_reading_json=payload,
        evaluated_on=date.fromisoformat(payload["evaluated_on"]),
        ruleset_version=payload["ruleset_version"],
        statement_publication_fingerprint=payload["statement_publication_fingerprint"],
        provenance_mode=payload["provenance_mode"],
        status=status,
        reason=reason,
        summary_markdown=summary_markdown,
    )


def request_summary(
    session: Session,
    *,
    project_id: int,
    principal: HumanPrincipal,
    client_factory: Callable[[CoordinationSummaryConfiguration], object],
    today: date | None = None,
) -> CoordinationSummaryRequest:
    """Run one explicit summary request or reuse its exact retained receipt."""
    require_human_principal(principal)
    configuration = _latest_configuration(session, project_id)
    if configuration is None:
        raise ConfigurationRequired(
            "Coordination Summary is unavailable until a complete server configuration is declared"
        )
    reading = freeze_project_reading(
        session,
        project_id,
        today=today,
        document_only=configuration.source_scope == "documents_only",
    )
    dependencies = [row.dependency for row in reading.rows]
    citables, floor, committed_dates = _assemble(
        session,
        dependencies,
        reading.evaluation,
        reading.statement_publication,
        project_scope=True,
    )
    user_message = _user_message(dependencies, citables, committed_dates, floor)
    payload = _reading_payload(reading, configuration, citables, floor, user_message)
    reading_sha256 = _reading_sha256(payload)
    existing = session.scalars(
        select(CoordinationSummaryRequest).where(
            CoordinationSummaryRequest.configuration_id == configuration.id,
            CoordinationSummaryRequest.reading_sha256 == reading_sha256,
        )
    ).first()
    if existing is not None:
        return existing

    if not citables:
        receipt = _receipt(
            project_id=project_id,
            configuration=configuration,
            principal=principal,
            payload=payload,
            reading_sha256=reading_sha256,
            status="empty_input",
            reason="no eligible cited project content; no model call was made",
            summary_markdown=None,
        )
        session.add(receipt)
        session.flush()
        return receipt
    # Conservative token accounting.  The exact input survives in the receipt,
    # and a request over the declared budget is refused before an adapter exists.
    estimated_input_tokens = (len(PROMPT.read_text()) + len(user_message) + 3) // 4
    if estimated_input_tokens > configuration.max_input_tokens:
        receipt = _receipt(
            project_id=project_id,
            configuration=configuration,
            principal=principal,
            payload=payload,
            reading_sha256=reading_sha256,
            status="budget_exhausted",
            reason=(
                f"input estimate {estimated_input_tokens} exceeds declared "
                f"budget {configuration.max_input_tokens}; no model call was made"
            ),
            summary_markdown=None,
        )
        session.add(receipt)
        session.flush()
        return receipt

    client = client_factory(configuration)
    try:
        result = client.complete(
            system=PROMPT.read_text(),
            user=user_message,
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["sentences"],
                "properties": {
                    "sentences": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text", "cites"],
                            "properties": {
                                "text": {"type": "string"},
                                "cites": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    }
                },
            },
        )
    except TimeoutError as exc:
        status, reason = (
            "timeout",
            f"model request exceeded declared time budget: {exc}",
        )
    except Exception as exc:  # adapter errors remain a receipt, never a hidden retry
        status, reason = (
            "transport_failure",
            f"model transport failed: {type(exc).__name__}: {exc}",
        )
    else:
        if not isinstance(result, dict):
            status, reason = (
                "validation_refused",
                "model returned no structured summary object",
            )
        else:
            drafted = [
                Sentence(
                    text=(item.get("text") or "").strip(),
                    cites=tuple(item.get("cites") or []),
                )
                for item in (result.get("sentences") or [])
                if isinstance(item, dict)
            ]
            kept, withheld = _check(
                drafted, {citable.ref: citable for citable in citables}
            )
            cited = {ref for sentence in kept for ref in sentence.cites}
            missing = [ref for ref in floor if ref not in cited]
            if missing:
                status = "validation_refused"
                reason = "required alert coverage missing: " + ", ".join(missing)
            else:
                briefing = Briefing(
                    ref_code=f"{reading.project.slug} — {len(dependencies)} records",
                    sentences=tuple(kept),
                    withheld=withheld,
                    floor=tuple(floor),
                    citables=tuple(citables),
                    prompt_version=configuration.prompt_version,
                    model=configuration.model,
                    evaluated_at=reading.evaluation.today,
                    ruleset_version=reading.evaluation.ruleset_version,
                    thresholds=reading.evaluation.thresholds,
                )
                receipt = _receipt(
                    project_id=project_id,
                    configuration=configuration,
                    principal=principal,
                    payload=payload,
                    reading_sha256=reading_sha256,
                    status="completed",
                    reason=None,
                    summary_markdown=render(briefing),
                )
                session.add(receipt)
                session.flush()
                return receipt
    receipt = _receipt(
        project_id=project_id,
        configuration=configuration,
        principal=principal,
        payload=payload,
        reading_sha256=reading_sha256,
        status=status,
        reason=reason,
        summary_markdown=None,
    )
    session.add(receipt)
    session.flush()
    return receipt
