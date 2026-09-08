"""Collect bounded measurement observations through #558's existing sink (#532).

Human work, sampling and reconciled provider bills are observations of work,
not domain decisions. These explicit inputs avoid deriving review minutes from
an idle browser or treating a correction as a confirmed policy false write.
Collection never writes the Project Record and never confers authorization.
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import math
from typing import Any
from hashlib import sha256
import json

from sqlalchemy import text

from corridor.analytics import AnalyticsBinding, AnalyticsEvent, EventFamily, default_binding, emit_event
from corridor.pilot_measurement import TIME_CATEGORIES


SAMPLE_KINDS = frozenset({"packet_usefulness", "child_usefulness", "material_change",
                          "baseline_field", "false_write", "release_readiness"})
USAGE_MODES = frozenset({"actual_call", "retry", "cache_reuse", "historical_experiment"})


def binding_for_session(session, binding: AnalyticsBinding | None = None) -> AnalyticsBinding:
    """Bind the actual customer DB's immutable #656 marker, with ordinary SELECT.

    A file cannot relabel a connection, and server IP is not stable across
    failover. The attested customer/environment/deployment marker supplies the
    logical database identity. An old, unmarked database stays unattributed.
    No monitor privilege, cluster-control function or analytics role is needed.
    """
    base = binding or default_binding()
    if session.scalar(text("select to_regclass('public.customer_environment_binding')")) is None:
        return replace(base, customer_id=None, environment=None, database_identity=None)
    rows = session.execute(text(
        "select customer_id, environment_id, deployment_id from public.customer_environment_binding"
    )).mappings().all()
    if len(rows) != 1:
        return replace(base, customer_id=None, environment=None, database_identity=None)
    marker = dict(rows[0])
    identity = "customer-database:" + sha256(json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return replace(base, customer_id=marker["customer_id"], environment=marker["environment_id"],
                   database_identity=identity)


def collect_observation(event: AnalyticsEvent) -> AnalyticsEvent:
    """Validate and emit one explicit analytical observation, retaining its identity.

    Callers supply a real observation/billing reference and an attributable
    principal for human entries. An unavailable value needs its recorded reason;
    a positive or zero value is accepted only as an explicit observation.
    """

    validate_observation(event)
    emit_event(event)
    return event


def validate_observation(event: AnalyticsEvent) -> None:
    p = event.payload
    if not p.get("project_id") or not p.get("evidence_reference"):
        raise ValueError("measurement observations require project and evidence reference")
    if event.family in {EventFamily.WORK_OBSERVATION, EventFamily.ARTIFACT_REPAIR}:
        if not p.get("actor") or p.get("category", "manual_repair") not in TIME_CATEGORIES:
            raise ValueError("work observations require an actor and a known work category")
        minutes = p.get("minutes")
        if minutes is None:
            if not p.get("unavailable_reason"):
                raise ValueError("unavailable time requires a reason")
        elif (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
              or not math.isfinite(minutes) or minutes < 0):
            raise ValueError("minutes must be an explicitly measured nonnegative finite number")
    elif event.family == EventFamily.MEASUREMENT_SAMPLE:
        if not p.get("actor") or p.get("sample_kind") not in SAMPLE_KINDS:
            raise ValueError("sampling requires an actor and a known sample kind")
        if p.get("sample_kind") == "material_change" and p.get("actor") == p.get("resolved_by"):
            raise ValueError("material-change sampling requires a reviewer other than the resolver")
    elif event.family == EventFamily.PROVIDER_USAGE:
        if p.get("mode") not in USAGE_MODES or not p.get("usage_id"):
            raise ValueError("provider usage requires an exact receipt identity and usage mode")
        if not all(p.get(name) for name in ("purpose", "source_class", "provider", "model",
                                            "prompt_version", "policy_version")):
            raise ValueError("provider usage requires purpose, source, provider, model and version attribution")
        if p.get("actual_cost_usd") is None:
            if not p.get("unavailable_reason"):
                raise ValueError("unavailable provider cost requires a reason")
        else:
            try:
                amount = Decimal(str(p["actual_cost_usd"]))
            except InvalidOperation as error:
                raise ValueError("actual cost must be a nonnegative finite decimal") from error
            if not amount.is_finite() or amount < 0:
                raise ValueError("actual cost must be a nonnegative finite decimal")
    else:
        raise ValueError("only explicit work, sampling, repair and provider observations may be imported")


def emit_presentation(
    family: EventFamily, *, project_id: int, principal_subject: str,
    at: datetime, binding: AnalyticsBinding, **payload: Any,
) -> None:
    """Record an actual project, coverage or evidence opening without a DB act."""

    if family not in {EventFamily.PROJECT_OPENING, EventFamily.COVERAGE_READING, EventFamily.EVIDENCE_OPENING}:
        raise ValueError("unsupported presentation family")
    emit_event(AnalyticsEvent(
        family=family, binding=binding, occurred_at=at,
        payload={"project_id": project_id, "principal_subject": principal_subject, **payload},
        metric_labels={"surface": family.value},
    ))


def emit_preparation_interaction(session, family: EventFamily, row, *, at: datetime,
                                 principal_subject: str, **payload: Any) -> None:
    """Capture the actual confirmation/request interaction beside its receipt.

    The log names a flushed row but is not proof it committed. Verified exports
    use the immutable row and this event only supplies its runtime binding.
    """
    if family not in {EventFamily.COVERAGE_CONFIRMATION, EventFamily.PREPARATION_REQUEST}:
        raise ValueError("unsupported preparation interaction")
    profile_sha256 = session.scalar(text(
        "select content_sha256 from project_issue_profiles where id = :profile_id and project_id = :project_id"
    ), {"profile_id": row.issue_profile_id, "project_id": row.project_id})
    emit_event(AnalyticsEvent(
        family=family, binding=binding_for_session(session), occurred_at=at,
        payload={"project_id": row.project_id, "receipt_id": row.id,
                 "issue_profile_identity": row.issue_profile_identity,
                 "issue_profile_version": row.issue_profile_version,
                 "issue_profile_sha256": profile_sha256,
                 "principal_subject": principal_subject, **payload},
    ))
