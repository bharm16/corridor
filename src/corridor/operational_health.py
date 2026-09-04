"""Runtime health and operational signals read from what the runtime already wrote (#491A).

An operator needs to know four things before anything else: is the process up,
is the database reachable, is the object store reachable, and is the worker
still beating. Then four numbers: how far behind the queue is, how many
attempts were retried, how many failed, and how long since anything succeeded.

A metrics library and a heartbeat table were both rejected. The Due Work
runtime already writes a durable claim on every attempt and an append-only
receipt on every outcome, with the timestamps and the retained error code, so
every one of those readings is a query over rows that exist; a heartbeat column
would be a second, weaker record of the same fact, and the migration window has
no room for one (`corridor.migrations.policy`). A worker with nothing to do
writes nothing, so freshness is judged against the tightest enabled schedule's
cadence — and against when the schedule was enabled, so a schedule configured a
minute ago is not reported stale before its first slot.

Signal labels carry the queue and the retained reason and nothing else. The
customer and project an event belongs to go in the structured log line
(`corridor.telemetry`); putting them in a label is what the shared rule in
`corridor.analytics` refuses, and every signal here is validated by it.

These readings are operational only. The Due Work receipt remains the audit
record; nothing here is authoritative and nothing here is a product measure
(#532).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from corridor.analytics import validate_metric_labels
from corridor.due_work import CADENCE_INTERVAL_SECONDS
from corridor.models import DueWorkOccurrence, DueWorkReceipt, DueWorkSchedule
from corridor.object_storage import ObjectStore


# One missed slot is a slow tick; two consecutive missed slots is the first
# reading that cannot be explained by scheduling jitter.
HEARTBEAT_GRACE = 2


@dataclass(frozen=True)
class ComponentHealth:
    """One runtime component's state, with a bounded reason code as its detail.

    The detail is never an exception message: this report is served
    unauthenticated, and a driver error carries the connection string.
    """

    component: str
    healthy: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "healthy": self.healthy,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class OperationalSignal:
    """One named operational reading and its low-cardinality labels."""

    name: str
    value: float
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_metric_labels(dict(self.labels))

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "labels": dict(self.labels)}


@dataclass(frozen=True)
class RuntimeReport:
    """What one process can say about the runtime it is part of."""

    role: str
    checks: tuple[ComponentHealth, ...]
    signals: tuple[OperationalSignal, ...]

    @property
    def healthy(self) -> bool:
        return all(check.healthy for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.healthy else "degraded",
            "role": self.role,
            "checks": [check.as_dict() for check in self.checks],
            "signals": [signal.as_dict() for signal in self.signals],
        }


def check_database(session: Session) -> ComponentHealth:
    """Reachable means the configured credential completed one round trip."""

    try:
        session.execute(text("select 1")).one()
    except Exception:
        return ComponentHealth("database", False, "unreachable")
    return ComponentHealth("database", True, "reachable")


def check_object_storage(store: ObjectStore) -> ComponentHealth:
    """Reachable means the backend's own probe answered without raising."""

    try:
        store.probe()
    except Exception:
        return ComponentHealth("object_storage", False, "unreachable")
    return ComponentHealth("object_storage", True, "reachable")


def check_worker_heartbeat(session: Session, *, now: datetime) -> ComponentHealth:
    """Beating means an attempt was claimed or retained within the schedule window."""

    reading = heartbeat_reading(session, now=now)
    if reading is None:
        return ComponentHealth("worker_heartbeat", True, "no_enabled_schedule")
    age, window = reading
    beating = age <= window
    return ComponentHealth(
        "worker_heartbeat", beating, "beating" if beating else "stale"
    )


def heartbeat_reading(
    session: Session, *, now: datetime
) -> tuple[float, float] | None:
    """Seconds since the newest durable worker act, and the window it is judged by.

    ``None`` when no schedule is enabled: a worker with nothing to run has
    nothing to prove, and reporting that as a stopped worker would page an
    operator for a correctly idle deployment.
    """

    now = _aware(now)
    enabled = session.execute(
        select(DueWorkSchedule.cadence, func.min(DueWorkSchedule.enabled_at))
        .where(DueWorkSchedule.disabled_at.is_(None))
        .group_by(DueWorkSchedule.cadence)
    ).all()
    intervals = [
        CADENCE_INTERVAL_SECONDS[cadence]
        for cadence, _ in enabled
        if cadence in CADENCE_INTERVAL_SECONDS
    ]
    if not intervals:
        return None
    window = float(min(intervals) * HEARTBEAT_GRACE)
    # Before the first attempt the schedule's own start is the only honest
    # reference: nothing was due yet, so nothing is late yet.
    reference = _latest_worker_act(session) or min(
        _aware(enabled_at) for _, enabled_at in enabled
    )
    return (now - _aware(reference)).total_seconds(), window


def due_work_signals(
    session: Session, *, now: datetime
) -> tuple[OperationalSignal, ...]:
    """Backlog, queue lag, retries, failures, last-success age, and heartbeat age."""

    now = _aware(now)
    signals: list[OperationalSignal] = []

    ready = session.execute(
        select(
            DueWorkSchedule.handler_key,
            func.count(),
            func.min(DueWorkOccurrence.due_at),
        )
        .select_from(DueWorkOccurrence)
        .join(DueWorkSchedule, DueWorkSchedule.id == DueWorkOccurrence.scheduled_job_id)
        .where(
            DueWorkSchedule.disabled_at.is_(None),
            DueWorkOccurrence.due_at <= now,
            or_(
                DueWorkOccurrence.state == "pending",
                (DueWorkOccurrence.state == "retry_due")
                & (DueWorkOccurrence.next_attempt_at <= now),
            ),
        )
        .group_by(DueWorkSchedule.handler_key)
    ).all()
    for handler_key, waiting, oldest_due_at in ready:
        signals.append(
            OperationalSignal(
                "corridor_processing_backlog", waiting, {"queue": handler_key}
            )
        )
        signals.append(
            OperationalSignal(
                "corridor_due_work_queue_lag_seconds",
                (now - _aware(oldest_due_at)).total_seconds(),
                {"queue": handler_key},
            )
        )

    retries = session.execute(
        select(DueWorkReceipt.handler_key, func.count())
        .where(DueWorkReceipt.execution_outcome == "retry_due")
        .group_by(DueWorkReceipt.handler_key)
    ).all()
    for handler_key, retried in retries:
        signals.append(
            OperationalSignal(
                "corridor_due_work_retries_total", retried, {"queue": handler_key}
            )
        )

    failures = session.execute(
        select(DueWorkReceipt.handler_key, DueWorkReceipt.error_code, func.count())
        .where(DueWorkReceipt.execution_outcome == "failed")
        .group_by(DueWorkReceipt.handler_key, DueWorkReceipt.error_code)
    ).all()
    for handler_key, error_code, failed in failures:
        signals.append(
            OperationalSignal(
                "corridor_due_work_failures_total",
                failed,
                {"queue": handler_key, "reason": error_code or "unrecorded"},
            )
        )

    successes = session.execute(
        select(DueWorkReceipt.handler_key, func.max(DueWorkReceipt.finished_at))
        .where(DueWorkReceipt.execution_outcome == "completed")
        .group_by(DueWorkReceipt.handler_key)
    ).all()
    for handler_key, finished_at in successes:
        signals.append(
            OperationalSignal(
                "corridor_due_work_last_success_age_seconds",
                (now - _aware(finished_at)).total_seconds(),
                {"queue": handler_key},
            )
        )

    reading = heartbeat_reading(session, now=now)
    if reading is not None:
        signals.append(
            OperationalSignal("corridor_worker_heartbeat_age_seconds", reading[0])
        )
    return tuple(signals)


def runtime_report(
    session: Session, *, now: datetime, store: ObjectStore, role: str
) -> RuntimeReport:
    """One process's answer to whether the runtime is serving, and how far behind."""

    database = check_database(session)
    checks = [
        ComponentHealth("application", True, "running"),
        database,
        check_object_storage(store),
    ]
    if not database.healthy:
        # Every remaining reading is a query. Reporting them as zero would
        # claim an empty queue during exactly the outage that fills it.
        checks.append(
            ComponentHealth("worker_heartbeat", False, "database_unreachable")
        )
        return RuntimeReport(role=role, checks=tuple(checks), signals=())
    checks.append(check_worker_heartbeat(session, now=now))
    return RuntimeReport(
        role=role,
        checks=tuple(checks),
        signals=due_work_signals(session, now=now),
    )


def serving_report(
    session: Session, *, store: ObjectStore, role: str
) -> RuntimeReport:
    """Whether this process can serve a request right now.

    A load balancer asks a narrower question than an operator does. The worker
    heartbeat is a reading about the fleet's cadence, not about whether this
    web process can answer: judging readiness on it would deregister a web task
    that is serving perfectly well because a batch schedule went unattended,
    taking the coordinator UI down for a reason the UI has nothing to do with.
    That is not hypothetical here -- the deployed environment runs no resident
    worker, so an enabled schedule with nothing to claim it goes stale by
    design.

    `runtime_report` keeps the aggregate answer, worker heartbeat included, and
    remains what /health serves and what alerting reads.
    """

    return RuntimeReport(
        role=role,
        checks=(
            ComponentHealth("application", True, "running"),
            check_database(session),
            check_object_storage(store),
        ),
        signals=(),
    )


def _latest_worker_act(session: Session) -> datetime | None:
    """The newest thing a worker durably did: hold a claim, or retain a receipt."""

    finished = session.scalar(select(func.max(DueWorkReceipt.finished_at)))
    claimed = session.scalar(select(func.max(DueWorkOccurrence.claimed_at)))
    return max(
        (item for item in (finished, claimed) if item is not None), default=None
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
