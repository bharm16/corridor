"""One supervised runtime for bounded operational work and crash recovery.

Feature-owned schedulers were rejected because they duplicate leases, retries,
clocks, and failure semantics while hiding production ownership. This module
keeps a small interface over one durable occurrence/claim/receipt lifecycle.
Persisted rows select only handlers in the server-owned registry; they can never
name imports, commands, arbitrary destinations, or model tools.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor.models import (
    Document,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    ExtractionRun,
    Project,
)


HANDLER_PROCESSING_HEALTH = "processing_health"
HANDLER_PROJECT_PROCESSING = "project_processing"
# The upper safety ceiling for a declared model spend. A processing schedule
# must declare a positive budget (never a silent zero); it may not exceed this.
_PROJECT_PROCESSING_TOKEN_CEILING = 100_000_000
_CONFIGURATION_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_EXTRACTOR_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_RUNTIME_OWNER = re.compile(r"^runtime:[A-Za-z0-9][A-Za-z0-9._:-]{1,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DueWorkRefusal(ValueError):
    """A schedule, claim, result, or runtime identity is unsafe."""


class StaleDueWorkClaim(DueWorkRefusal):
    """A worker tried to finalize an occurrence it no longer owns."""


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class ProcessingHealthDeclaration:
    project_id: int
    configuration_version: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
    ) -> "ProcessingHealthDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=60,
            claim_ttl_seconds=300,
            deadline_seconds=120,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class ProjectProcessingDeclaration:
    """One validated gate-7 declaration that enables project processing.

    Processing reads models, so its bounds differ from the read-only health
    handler: the claim lease and deadline are longer, and a positive model
    spending budget must be declared explicitly rather than left at a silent
    zero. Scope names the exact project and the deployed extractor identity;
    authorized destinations stay empty and concurrency stays one.
    """

    project_id: int
    configuration_version: str
    extractor_identity: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        extractor_identity: str,
        starts_at: datetime,
        model_token_budget: int = 2_000_000,
    ) -> "ProjectProcessingDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            extractor_identity=extractor_identity,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=1800,
            deadline_seconds=1800,
            concurrency_limit=1,
            model_token_budget=model_token_budget,
            notification_budget=0,
        )


@dataclass(frozen=True)
class DueWorkClaim:
    occurrence_id: int
    occurrence_public_id: str
    schedule_id: int
    attempt_number: int
    attempt_id: str
    claim_token: str
    runtime_owner: str
    started_at: datetime
    lease_expires_at: datetime
    deadline_at: datetime


@dataclass(frozen=True)
class DueWorkRunResult:
    occurrence_id: int
    receipt_id: int
    occurrence_public_id: str
    receipt_public_id: str
    job_public_id: str
    attempt_id: str
    project_id: int
    handler_key: str
    configuration_version: str
    input_identity_sha256: str
    execution_outcome: str
    handler_result: dict[str, Any] | None
    error_code: str | None
    safe_next_step: str


@dataclass(frozen=True)
class HandlerContract:
    key: str
    scope_kind: str
    idempotency_contract: str
    max_result_bytes: int
    model_token_budget: int
    notification_budget: int
    # A read-only handler runs inside a `reading` session and returns a result
    # dict the runtime finalizes. An effectful handler owns its own durable
    # commits and instead takes an `EffectfulContext`; exactly one is set.
    run: Callable[[Session, DueWorkSchedule, datetime], dict[str, Any]] | None = None
    run_effectful: Callable[["EffectfulContext"], dict[str, Any]] | None = None


@dataclass(frozen=True)
class EffectfulContext:
    """What an effectful handler needs to own its durable work and be recovered.

    The handler commits its domain work through ``session_factory`` before the
    runtime finalizes the claim, so a stale-claim or deadline failure at finalize
    only re-runs work that is already durable and idempotent.
    """

    session_factory: Any
    schedule: DueWorkSchedule
    claim: "DueWorkClaim"
    clock: "Clock"
    registry: Mapping[str, "HandlerContract"]


def _processing_health(
    session: Session,
    schedule: DueWorkSchedule,
    observed_at: datetime,
) -> dict[str, Any]:
    project_id = schedule.project_id
    document_count = int(
        session.scalar(
            select(func.count()).select_from(Document).where(
                Document.project_id == project_id
            )
        )
        or 0
    )
    failed_document_count = int(
        session.scalar(
            select(func.count()).select_from(Document).where(
                Document.project_id == project_id,
                Document.parse_status == "failed",
            )
        )
        or 0
    )
    failed_extraction_count = int(
        session.scalar(
            select(func.count())
            .select_from(ExtractionRun)
            .join(Document, Document.id == ExtractionRun.document_id)
            .where(
                Document.project_id == project_id,
                or_(
                    ExtractionRun.outcome != "completed",
                    ExtractionRun.page_errors > 0,
                ),
            )
        )
        or 0
    )
    health = (
        "healthy"
        if failed_document_count == 0 and failed_extraction_count == 0
        else "processing_failures_observed"
    )
    return {
        "schema_version": "processing-health-result-v1",
        "project_id": project_id,
        "configuration_version": schedule.configuration_version,
        "observed_at": _iso(observed_at),
        "health": health,
        "document_count": document_count,
        "failed_document_count": failed_document_count,
        "failed_extraction_count": failed_extraction_count,
    }


def _project_processing_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Run the bounded project-processing pass for a claimed occurrence.

    The pass commits extraction and Record Inclusion durably through the
    session factory; this wrapper only adapts the runtime's claim into a
    ``process_project`` call over the currently deployed extraction routes and
    summarizes the result into a bounded receipt.
    """

    from corridor.llm import OpenAIClient
    from corridor.pipeline import extraction_route
    from corridor.project_processing import process_project, summarize_pass

    with context.session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
        if schedule is None:
            raise DueWorkRefusal("Due Work schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version

    client = OpenAIClient()
    try:
        result = process_project(
            context.session_factory,
            project_id=project_id,
            select_route=lambda document: extraction_route(document, client=client),
            clock=context.clock,
        )
    finally:
        client.close()
    return summarize_pass(
        result,
        configuration_version=configuration_version,
        observed_at=_aware_utc(context.clock.now()),
    )


HANDLER_REGISTRY: Mapping[str, HandlerContract] = MappingProxyType(
    {
        HANDLER_PROCESSING_HEALTH: HandlerContract(
            key=HANDLER_PROCESSING_HEALTH,
            scope_kind="one_project_stored_processing_facts",
            idempotency_contract="read_only_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            run=_processing_health,
        ),
        HANDLER_PROJECT_PROCESSING: HandlerContract(
            key=HANDLER_PROJECT_PROCESSING,
            scope_kind="one_registered_project_extraction",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=8192,
            model_token_budget=_PROJECT_PROCESSING_TOKEN_CEILING,
            notification_budget=0,
            run_effectful=_project_processing_effectful,
        ),
    }
)


def configure_processing_health(
    session: Session,
    declaration: ProcessingHealthDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 processing-health declaration."""

    now = _aware_utc(now)
    configuration = _validated_health_declaration(declaration)
    if session.get(Project, declaration.project_id) is None:
        raise DueWorkRefusal(f"project {declaration.project_id} does not exist")
    configuration_sha256 = _sha256(configuration)
    input_identity_sha256 = _sha256(
        {
            "handler": HANDLER_PROCESSING_HEALTH,
            "project_id": declaration.project_id,
            "source": "stored_processing_facts-v1",
        }
    )
    existing = session.scalar(
        select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == declaration.project_id,
            DueWorkSchedule.handler_key == HANDLER_PROCESSING_HEALTH,
            DueWorkSchedule.configuration_version
            == declaration.configuration_version,
            DueWorkSchedule.input_identity_sha256 == input_identity_sha256,
        )
    )
    if existing is not None:
        if existing.configuration_sha256 != configuration_sha256:
            raise DueWorkRefusal(
                "configuration version already names different Due Work rules"
            )
        return existing

    active = session.scalars(
        select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == declaration.project_id,
            DueWorkSchedule.handler_key == HANDLER_PROCESSING_HEALTH,
            DueWorkSchedule.disabled_at.is_(None),
        )
    ).all()
    for prior in active:
        prior.disabled_at = now

    public_id = f"due-job:{configuration_sha256[:24]}"
    schedule = DueWorkSchedule(
        public_id=public_id,
        project_id=declaration.project_id,
        handler_key=HANDLER_PROCESSING_HEALTH,
        configuration_version=declaration.configuration_version,
        scope_json={"project_id": declaration.project_id},
        configuration_json=configuration,
        configuration_sha256=configuration_sha256,
        input_identity_sha256=input_identity_sha256,
        starts_at=declaration.starts_at,
        cadence=declaration.cadence,
        timezone_name=declaration.timezone_name,
        missed_run_policy=declaration.missed_run_policy,
        retention_days=declaration.retention_days,
        max_attempts=declaration.max_attempts,
        backoff_seconds=declaration.backoff_seconds,
        claim_ttl_seconds=declaration.claim_ttl_seconds,
        deadline_seconds=declaration.deadline_seconds,
        concurrency_limit=declaration.concurrency_limit,
        model_token_budget=declaration.model_token_budget,
        notification_budget=declaration.notification_budget,
        enabled_at=now,
    )
    session.add(schedule)
    session.flush([schedule])
    return schedule


def configure_project_processing(
    session: Session,
    declaration: ProjectProcessingDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 project-processing declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written; model credentials alone never enable a project sweep. Enabling a
    new configuration disables the project's prior processing schedule while
    retaining it for audit.
    """

    now = _aware_utc(now)
    configuration = _validated_processing_declaration(declaration)
    if session.get(Project, declaration.project_id) is None:
        raise DueWorkRefusal(f"project {declaration.project_id} does not exist")
    configuration_sha256 = _sha256(configuration)
    input_identity_sha256 = _sha256(
        {
            "handler": HANDLER_PROJECT_PROCESSING,
            "project_id": declaration.project_id,
            "extractor_identity": declaration.extractor_identity,
        }
    )
    existing = session.scalar(
        select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == declaration.project_id,
            DueWorkSchedule.handler_key == HANDLER_PROJECT_PROCESSING,
            DueWorkSchedule.configuration_version
            == declaration.configuration_version,
            DueWorkSchedule.input_identity_sha256 == input_identity_sha256,
        )
    )
    if existing is not None:
        if existing.configuration_sha256 != configuration_sha256:
            raise DueWorkRefusal(
                "configuration version already names different Due Work rules"
            )
        return existing

    active = session.scalars(
        select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == declaration.project_id,
            DueWorkSchedule.handler_key == HANDLER_PROJECT_PROCESSING,
            DueWorkSchedule.disabled_at.is_(None),
        )
    ).all()
    for prior in active:
        prior.disabled_at = now

    public_id = f"due-job:{configuration_sha256[:24]}"
    schedule = DueWorkSchedule(
        public_id=public_id,
        project_id=declaration.project_id,
        handler_key=HANDLER_PROJECT_PROCESSING,
        configuration_version=declaration.configuration_version,
        scope_json={
            "project_id": declaration.project_id,
            "extractor_identity": declaration.extractor_identity,
        },
        configuration_json=configuration,
        configuration_sha256=configuration_sha256,
        input_identity_sha256=input_identity_sha256,
        starts_at=declaration.starts_at,
        cadence=declaration.cadence,
        timezone_name=declaration.timezone_name,
        missed_run_policy=declaration.missed_run_policy,
        retention_days=declaration.retention_days,
        max_attempts=declaration.max_attempts,
        backoff_seconds=declaration.backoff_seconds,
        claim_ttl_seconds=declaration.claim_ttl_seconds,
        deadline_seconds=declaration.deadline_seconds,
        concurrency_limit=declaration.concurrency_limit,
        model_token_budget=declaration.model_token_budget,
        notification_budget=declaration.notification_budget,
        enabled_at=now,
    )
    session.add(schedule)
    session.flush([schedule])
    return schedule


def enqueue_due_work(session: Session, *, now: datetime) -> tuple[DueWorkOccurrence, ...]:
    """Coalesce the latest due occurrence for every active validated schedule."""

    now = _aware_utc(now)
    schedules = session.scalars(
        select(DueWorkSchedule)
        .where(
            DueWorkSchedule.disabled_at.is_(None),
            DueWorkSchedule.starts_at <= now,
        )
        .order_by(DueWorkSchedule.id)
    ).all()
    keys: list[str] = []
    for schedule in schedules:
        _validate_stored_schedule(schedule)
        due_at = now.replace(minute=0, second=0, microsecond=0)
        if due_at < schedule.starts_at:
            continue
        occurrence_key = _sha256(
            {
                "handler": schedule.handler_key,
                "scope": schedule.scope_json,
                "configuration_version": schedule.configuration_version,
                "configuration_sha256": schedule.configuration_sha256,
                "input_identity_sha256": schedule.input_identity_sha256,
                "due_at": _iso(due_at),
            }
        )
        keys.append(occurrence_key)
        session.execute(
            insert(DueWorkOccurrence)
            .values(
                public_id=f"due-occurrence:{occurrence_key[:24]}",
                scheduled_job_id=schedule.id,
                occurrence_key=occurrence_key,
                due_at=due_at,
                state="pending",
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["occurrence_key"])
        )
        _fail_missed_latest_only(
            session,
            schedule,
            latest_due_at=due_at,
            now=now,
        )
    if not keys:
        return ()
    return tuple(
        session.scalars(
            select(DueWorkOccurrence)
            .where(DueWorkOccurrence.occurrence_key.in_(keys))
            .order_by(DueWorkOccurrence.due_at, DueWorkOccurrence.id)
        ).all()
    )


def claim_due_work(
    session: Session,
    *,
    now: datetime,
    owner: str,
) -> DueWorkClaim | None:
    """Claim one ready occurrence, recovering or retaining abandoned attempts."""

    now = _aware_utc(now)
    _validate_owner(owner)
    candidates = session.scalars(
        select(DueWorkOccurrence)
        .join(DueWorkSchedule, DueWorkSchedule.id == DueWorkOccurrence.scheduled_job_id)
        .where(
            DueWorkSchedule.disabled_at.is_(None),
            DueWorkOccurrence.due_at <= now,
            or_(
                DueWorkOccurrence.state == "pending",
                (
                    (DueWorkOccurrence.state == "retry_due")
                    & (DueWorkOccurrence.next_attempt_at <= now)
                ),
                (
                    (DueWorkOccurrence.state == "claimed")
                    & (DueWorkOccurrence.lease_expires_at <= now)
                ),
            ),
        )
        .order_by(DueWorkOccurrence.due_at, DueWorkOccurrence.id)
        .with_for_update(skip_locked=True)
    ).all()
    for occurrence in candidates:
        schedule = session.get(DueWorkSchedule, occurrence.scheduled_job_id)
        if schedule is None:
            continue
        _validate_stored_schedule(schedule)
        if occurrence.state == "claimed":
            if occurrence.attempt_count >= schedule.max_attempts:
                _record_abandoned_failure(session, occurrence, schedule, now=now)
                continue
            _record_abandoned_retry(session, occurrence, schedule, now=now)
        active_claims = int(
            session.scalar(
                select(func.count()).select_from(DueWorkOccurrence).where(
                    DueWorkOccurrence.scheduled_job_id == schedule.id,
                    DueWorkOccurrence.state == "claimed",
                    DueWorkOccurrence.lease_expires_at > now,
                )
            )
            or 0
        )
        if active_claims >= schedule.concurrency_limit:
            continue
        token = uuid4().hex
        occurrence.attempt_count += 1
        occurrence.state = "claimed"
        occurrence.owner = owner
        occurrence.claim_token = token
        occurrence.claimed_at = now
        occurrence.lease_expires_at = now + timedelta(seconds=schedule.claim_ttl_seconds)
        occurrence.deadline_at = now + timedelta(seconds=schedule.deadline_seconds)
        occurrence.next_attempt_at = None
        occurrence.last_error_code = None
        session.flush([occurrence])
        return DueWorkClaim(
            occurrence_id=occurrence.id,
            occurrence_public_id=occurrence.public_id,
            schedule_id=schedule.id,
            attempt_number=occurrence.attempt_count,
            attempt_id=f"due-attempt:{token}",
            claim_token=token,
            runtime_owner=owner,
            started_at=now,
            lease_expires_at=occurrence.lease_expires_at,
            deadline_at=occurrence.deadline_at,
        )
    return None


def complete_due_work(
    session: Session,
    claim: DueWorkClaim,
    *,
    handler_result: dict[str, Any],
    now: datetime,
) -> DueWorkRunResult:
    """Finalize a live claim with one validated append-only completed receipt."""

    now = _aware_utc(now)
    occurrence, schedule = _locked_live_claim(session, claim, now=now)
    contract = _handler(schedule.handler_key)
    _validate_handler_result(contract, handler_result)
    safe_next_step = _safe_next_step(contract, handler_result)
    receipt = _append_receipt(
        session,
        occurrence,
        schedule,
        claim,
        execution_outcome="completed",
        handler_result=handler_result,
        error_code=None,
        safe_next_step=safe_next_step,
        finished_at=now,
    )
    _release_occurrence(occurrence, state="completed", error_code=None)
    session.flush([occurrence])
    return _run_result(receipt, occurrence=occurrence, schedule=schedule)


def fail_due_work(
    session: Session,
    claim: DueWorkClaim,
    *,
    error_code: str,
    now: datetime,
    retryable: bool = True,
) -> DueWorkRunResult:
    """Retain a failed attempt and schedule bounded retry or terminal failure."""

    now = _aware_utc(now)
    occurrence, schedule = _locked_live_claim(session, claim, now=now)
    if not error_code or len(error_code) > 64:
        raise DueWorkRefusal("Due Work error code is invalid")
    retry = retryable and occurrence.attempt_count < schedule.max_attempts
    outcome = "retry_due" if retry else "failed"
    safe_next_step = "retry_after_backoff" if retry else "inspect_processing_failure"
    receipt = _append_receipt(
        session,
        occurrence,
        schedule,
        claim,
        execution_outcome=outcome,
        handler_result=None,
        error_code=error_code,
        safe_next_step=safe_next_step,
        finished_at=now,
    )
    _release_occurrence(occurrence, state=outcome, error_code=error_code)
    if retry:
        occurrence.next_attempt_at = now + timedelta(
            seconds=schedule.backoff_seconds * (2 ** (occurrence.attempt_count - 1))
        )
    session.flush([occurrence])
    return _run_result(receipt, occurrence=occurrence, schedule=schedule)


def run_due_work_once(
    session_factory,
    *,
    clock: Clock,
    owner: str,
    registry: Mapping[str, HandlerContract] = HANDLER_REGISTRY,
) -> DueWorkRunResult | None:
    """Claim, execute outside the claim transaction, and reconcile one attempt."""

    started_at = _aware_utc(clock.now())
    with session_factory() as claiming:
        with claiming.begin():
            claim = claim_due_work(claiming, now=started_at, owner=owner)
    if claim is None:
        return None

    try:
        with session_factory() as reading:
            schedule = reading.get(DueWorkSchedule, claim.schedule_id)
            if schedule is None or schedule.handler_key not in registry:
                raise DueWorkRefusal("claimed handler is not server-owned")
            contract = registry[schedule.handler_key]
            if contract.run_effectful is None:
                # Read-only handler: run inside the reading transaction and
                # let the runtime finalize its result.
                handler_result = contract.run(reading, schedule, started_at)
        if contract.run_effectful is not None:
            # Effectful handler: it owns its durable commits, run outside any
            # runtime-held transaction so no domain lock spans the finalize.
            handler_result = contract.run_effectful(
                EffectfulContext(
                    session_factory=session_factory,
                    schedule=schedule,
                    claim=claim,
                    clock=clock,
                    registry=registry,
                )
            )
        finished_at = _aware_utc(clock.now())
        with session_factory() as finalizing:
            with finalizing.begin():
                if finished_at > claim.deadline_at:
                    # The domain work already committed durably and is
                    # idempotent, so recovering and re-running this occurrence
                    # is safe.
                    return fail_due_work(
                        finalizing,
                        claim,
                        error_code="deadline_exceeded",
                        now=finished_at,
                    )
                return complete_due_work(
                    finalizing,
                    claim,
                    handler_result=handler_result,
                    now=finished_at,
                )
    except StaleDueWorkClaim:
        raise
    except Exception:
        failed_at = _aware_utc(clock.now())
        with session_factory() as finalizing:
            with finalizing.begin():
                return fail_due_work(
                    finalizing,
                    claim,
                    error_code="handler_execution_failed",
                    now=failed_at,
                )


def due_work_status(session: Session, *, project_id: int | None = None) -> dict[str, Any]:
    """Return credential-free operational schedule, occurrence, and receipt status."""

    schedule_query = select(DueWorkSchedule)
    if project_id is not None:
        schedule_query = schedule_query.where(DueWorkSchedule.project_id == project_id)
    schedules = session.scalars(schedule_query.order_by(DueWorkSchedule.id)).all()
    schedule_ids = [schedule.id for schedule in schedules]
    occurrences = (
        session.scalars(
            select(DueWorkOccurrence)
            .where(DueWorkOccurrence.scheduled_job_id.in_(schedule_ids))
            .order_by(DueWorkOccurrence.due_at, DueWorkOccurrence.id)
        ).all()
        if schedule_ids
        else []
    )
    occurrence_ids = [occurrence.id for occurrence in occurrences]
    receipts = (
        session.scalars(
            select(DueWorkReceipt)
            .where(DueWorkReceipt.occurrence_id.in_(occurrence_ids))
            .order_by(DueWorkReceipt.id)
        ).all()
        if occurrence_ids
        else []
    )
    occurrence_by_id = {item.id: item for item in occurrences}
    schedule_by_id = {item.id: item for item in schedules}
    for receipt in receipts:
        occurrence = occurrence_by_id.get(receipt.occurrence_id)
        schedule = (
            schedule_by_id.get(occurrence.scheduled_job_id)
            if occurrence is not None
            else None
        )
        if occurrence is None or schedule is None:
            raise DueWorkRefusal("Due Work receipt lineage is incomplete")
        expected = _receipt_content(
            occurrence_public_id=occurrence.public_id,
            schedule=schedule,
            attempt_number=receipt.attempt_number,
            attempt_id=receipt.attempt_id,
            runtime_owner=receipt.runtime_owner,
            execution_outcome=receipt.execution_outcome,
            handler_result=receipt.handler_result_json,
            error_code=receipt.error_code,
            safe_next_step=receipt.safe_next_step,
            started_at=receipt.started_at,
            finished_at=receipt.finished_at,
        )
        if receipt.content_sha256 != _sha256(expected):
            raise DueWorkRefusal("Due Work receipt integrity check failed")
    return {
        "jobs": [
            {
                "job_id": item.public_id,
                "project_id": item.project_id,
                "handler": item.handler_key,
                "configuration_version": item.configuration_version,
                "configuration_sha256": item.configuration_sha256,
                "input_identity": item.configuration_json["input_identity"],
                "input_identity_sha256": item.input_identity_sha256,
                "cadence": item.cadence,
                "timezone": item.timezone_name,
                "missed_run_policy": item.missed_run_policy,
                "retention_policy": f"retain_at_least_{item.retention_days}_days",
                "enabled": item.disabled_at is None,
                "model_token_budget": item.model_token_budget,
                "notification_budget": item.notification_budget,
            }
            for item in schedules
        ],
        "occurrences": [
            {
                "occurrence_id": item.public_id,
                "job_id": schedule_by_id[item.scheduled_job_id].public_id,
                "project_id": schedule_by_id[item.scheduled_job_id].project_id,
                "handler": schedule_by_id[item.scheduled_job_id].handler_key,
                "configuration_version": schedule_by_id[
                    item.scheduled_job_id
                ].configuration_version,
                "input_identity_sha256": schedule_by_id[
                    item.scheduled_job_id
                ].input_identity_sha256,
                "due_at": _iso(item.due_at),
                "state": item.state,
                "attempt_count": item.attempt_count,
                "next_attempt_at": (
                    _iso(item.next_attempt_at) if item.next_attempt_at else None
                ),
                "runtime_owner": item.owner,
                "last_error_code": item.last_error_code,
            }
            for item in occurrences
        ],
        "receipts": [
            {
                "receipt_id": item.public_id,
                "occurrence_id": occurrence_by_id[item.occurrence_id].public_id,
                "job_id": schedule_by_id[
                    occurrence_by_id[item.occurrence_id].scheduled_job_id
                ].public_id,
                "project_id": item.project_id,
                "handler": item.handler_key,
                "configuration_version": schedule_by_id[
                    occurrence_by_id[item.occurrence_id].scheduled_job_id
                ].configuration_version,
                "input_identity_sha256": schedule_by_id[
                    occurrence_by_id[item.occurrence_id].scheduled_job_id
                ].input_identity_sha256,
                "attempt_id": item.attempt_id,
                "attempt_number": item.attempt_number,
                "runtime_owner": item.runtime_owner,
                "execution_outcome": item.execution_outcome,
                "handler_result": item.handler_result_json,
                "error_code": item.error_code,
                "safe_next_step": item.safe_next_step,
                "content_sha256": item.content_sha256,
            }
            for item in receipts
        ],
    }


def supervise_due_work(
    session_factory,
    *,
    clock: Clock,
    owner: str,
    stop_requested: Callable[[], bool],
    wait: Callable[[float], None],
    poll_seconds: float,
) -> int:
    """Run the production supervisor until shutdown stops taking new work."""

    if poll_seconds <= 0 or poll_seconds > 60:
        raise DueWorkRefusal("Due Work supervisor poll interval is invalid")
    completed_cycles = 0
    while not stop_requested():
        tick_at = _aware_utc(clock.now())
        with session_factory() as ticking:
            with ticking.begin():
                enqueue_due_work(ticking, now=tick_at)
        if stop_requested():
            break
        result = run_due_work_once(
            session_factory,
            clock=clock,
            owner=owner,
        )
        if result is not None:
            completed_cycles += 1
            continue
        wait(poll_seconds)
    return completed_cycles


def _validated_health_declaration(
    declaration: ProcessingHealthDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("processing-health starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "processing-health supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 900
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and declaration.model_token_budget == 0
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal("processing-health gate-7 resource declaration is invalid")
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_PROCESSING_HEALTH,
        "project_id": declaration.project_id,
        "scope": {"project_id": declaration.project_id},
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "stored_processing_facts-v1",
            "project_id": declaration.project_id,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "read_only_reconcilable",
    }


def _validated_processing_declaration(
    declaration: ProjectProcessingDeclaration,
) -> dict[str, Any]:
    starts_at = _aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal("project-processing starts_at must align to a UTC hour")
    if not _CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if not _EXTRACTOR_IDENTITY.fullmatch(declaration.extractor_identity):
        raise DueWorkRefusal("project-processing extractor identity is invalid")
    if (
        declaration.cadence != "hourly"
        or declaration.timezone_name != "UTC"
        or declaration.missed_run_policy != "latest_only"
    ):
        raise DueWorkRefusal(
            "project-processing supports only hourly UTC latest-only scheduling"
        )
    if not (
        1 <= declaration.max_attempts <= 5
        and 0 <= declaration.backoff_seconds <= 3600
        and 30 <= declaration.claim_ttl_seconds <= 3600
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and 1 <= declaration.model_token_budget <= _PROJECT_PROCESSING_TOKEN_CEILING
        and declaration.notification_budget == 0
    ):
        raise DueWorkRefusal(
            "project-processing gate-7 resource declaration is invalid"
        )
    return {
        "schema_version": "due-work-gate-7-v1",
        "handler": HANDLER_PROJECT_PROCESSING,
        "project_id": declaration.project_id,
        "scope": {
            "project_id": declaration.project_id,
            "extractor_identity": declaration.extractor_identity,
        },
        "configuration_version": declaration.configuration_version,
        "input_identity": {
            "kind": "registered_project_extraction-v1",
            "project_id": declaration.project_id,
            "extractor_identity": declaration.extractor_identity,
        },
        "starts_at": _iso(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
        },
        "authorized_destinations": [],
        "idempotency_contract": "at_least_once_reconcilable",
    }


def _validate_stored_schedule(schedule: DueWorkSchedule) -> None:
    if schedule.handler_key not in HANDLER_REGISTRY:
        raise DueWorkRefusal("persisted Due Work handler is not server-owned")
    if schedule.configuration_sha256 != _sha256(schedule.configuration_json):
        raise DueWorkRefusal("persisted Due Work configuration digest does not match")
    if schedule.configuration_json.get("handler") != schedule.handler_key:
        raise DueWorkRefusal("persisted Due Work configuration handler does not match")
    if schedule.handler_key == HANDLER_PROJECT_PROCESSING:
        expected_config = _validated_processing_declaration(
            ProjectProcessingDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                extractor_identity=schedule.scope_json.get("extractor_identity", ""),
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {
            "project_id": schedule.project_id,
            "extractor_identity": schedule.scope_json.get("extractor_identity", ""),
        }
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "extractor_identity": schedule.scope_json.get("extractor_identity", ""),
            }
        )
    else:
        expected_config = _validated_health_declaration(
            ProcessingHealthDeclaration(
                project_id=schedule.project_id,
                configuration_version=schedule.configuration_version,
                starts_at=schedule.starts_at,
                cadence=schedule.cadence,
                timezone_name=schedule.timezone_name,
                missed_run_policy=schedule.missed_run_policy,
                retention_days=schedule.retention_days,
                max_attempts=schedule.max_attempts,
                backoff_seconds=schedule.backoff_seconds,
                claim_ttl_seconds=schedule.claim_ttl_seconds,
                deadline_seconds=schedule.deadline_seconds,
                concurrency_limit=schedule.concurrency_limit,
                model_token_budget=schedule.model_token_budget,
                notification_budget=schedule.notification_budget,
            )
        )
        expected_scope = {"project_id": schedule.project_id}
        expected_input_identity = _sha256(
            {
                "handler": schedule.handler_key,
                "project_id": schedule.project_id,
                "source": "stored_processing_facts-v1",
            }
        )
    if schedule.configuration_json != expected_config:
        raise DueWorkRefusal("persisted Due Work configuration is unsupported")
    if (
        schedule.scope_json != expected_scope
        or schedule.input_identity_sha256 != expected_input_identity
    ):
        raise DueWorkRefusal("persisted Due Work scope or input identity is invalid")
    contract = HANDLER_REGISTRY[schedule.handler_key]
    if (
        schedule.model_token_budget > contract.model_token_budget
        or schedule.notification_budget > contract.notification_budget
    ):
        raise DueWorkRefusal("persisted Due Work budgets exceed the handler contract")


def _handler(key: str) -> HandlerContract:
    contract = HANDLER_REGISTRY.get(key)
    if contract is None:
        raise DueWorkRefusal("Due Work handler is not server-owned")
    return contract


def _safe_next_step(contract: HandlerContract, handler_result: dict[str, Any]) -> str:
    """The recovery pointer a completed receipt records, per handler."""

    if handler_result.get("health") == "healthy":
        return "none"
    if contract.key == HANDLER_PROJECT_PROCESSING:
        return "inspect_processing_attention"
    return "inspect_failed_document_processing"


def _validate_handler_result(contract: HandlerContract, result: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise DueWorkRefusal("Due Work handler result must be an object")
    encoded = _canonical_json(result)
    if len(encoded) > contract.max_result_bytes:
        raise DueWorkRefusal("Due Work handler result exceeds its resource contract")
    if contract.key == HANDLER_PROCESSING_HEALTH and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "document_count",
            "failed_document_count",
            "failed_extraction_count",
        }
        or result.get("health") not in {"healthy", "processing_failures_observed"}
    ):
        raise DueWorkRefusal("processing-health handler result is invalid")
    if contract.key == HANDLER_PROJECT_PROCESSING and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "eligible_document_count",
            "extracted",
            "skipped",
            "failed",
            "unreadable",
            "quarantined",
            "held_out",
            "processing_failures",
            "reconciled",
            "admitted",
            "waiting",
            "ambiguous_documents",
        }
        or result.get("health") not in {"healthy", "processing_attention_required"}
    ):
        raise DueWorkRefusal("project-processing handler result is invalid")


def _locked_live_claim(
    session: Session,
    claim: DueWorkClaim,
    *,
    now: datetime,
) -> tuple[DueWorkOccurrence, DueWorkSchedule]:
    occurrence = session.scalar(
        select(DueWorkOccurrence)
        .where(DueWorkOccurrence.id == claim.occurrence_id)
        .with_for_update()
    )
    if (
        occurrence is None
        or occurrence.state != "claimed"
        or occurrence.owner != claim.runtime_owner
        or occurrence.claim_token != claim.claim_token
        or occurrence.attempt_count != claim.attempt_number
        or occurrence.lease_expires_at is None
        or occurrence.lease_expires_at <= now
    ):
        raise StaleDueWorkClaim("Due Work claim is stale or no longer owned")
    schedule = session.get(DueWorkSchedule, occurrence.scheduled_job_id)
    if schedule is None:
        raise DueWorkRefusal("Due Work schedule disappeared")
    return occurrence, schedule


def _append_receipt(
    session: Session,
    occurrence: DueWorkOccurrence,
    schedule: DueWorkSchedule,
    claim: DueWorkClaim,
    *,
    execution_outcome: str,
    handler_result: dict[str, Any] | None,
    error_code: str | None,
    safe_next_step: str,
    finished_at: datetime,
) -> DueWorkReceipt:
    content = _receipt_content(
        occurrence_public_id=occurrence.public_id,
        schedule=schedule,
        attempt_number=claim.attempt_number,
        attempt_id=claim.attempt_id,
        runtime_owner=claim.runtime_owner,
        execution_outcome=execution_outcome,
        handler_result=handler_result,
        error_code=error_code,
        safe_next_step=safe_next_step,
        started_at=claim.started_at,
        finished_at=finished_at,
    )
    digest = _sha256(content)
    receipt = DueWorkReceipt(
        public_id=f"due-receipt:{digest[:24]}",
        occurrence_id=occurrence.id,
        project_id=schedule.project_id,
        handler_key=schedule.handler_key,
        attempt_number=claim.attempt_number,
        attempt_id=claim.attempt_id,
        runtime_owner=claim.runtime_owner,
        execution_outcome=execution_outcome,
        handler_result_json=handler_result,
        error_code=error_code,
        safe_next_step=safe_next_step,
        started_at=claim.started_at,
        finished_at=finished_at,
        content_sha256=digest,
    )
    session.add(receipt)
    session.flush([receipt])
    return receipt


def _receipt_content(
    *,
    occurrence_public_id: str,
    schedule: DueWorkSchedule,
    attempt_number: int,
    attempt_id: str,
    runtime_owner: str,
    execution_outcome: str,
    handler_result: dict[str, Any] | None,
    error_code: str | None,
    safe_next_step: str,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": "due-work-attempt-receipt-v1",
        "occurrence_id": occurrence_public_id,
        "job_id": schedule.public_id,
        "project_id": schedule.project_id,
        "handler": schedule.handler_key,
        "configuration_sha256": schedule.configuration_sha256,
        "attempt_number": attempt_number,
        "attempt_id": attempt_id,
        "runtime_owner": runtime_owner,
        "execution_outcome": execution_outcome,
        "handler_result": handler_result,
        "error_code": error_code,
        "safe_next_step": safe_next_step,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
    }


def _record_abandoned_failure(
    session: Session,
    occurrence: DueWorkOccurrence,
    schedule: DueWorkSchedule,
    *,
    now: datetime,
) -> None:
    claim = DueWorkClaim(
        occurrence_id=occurrence.id,
        occurrence_public_id=occurrence.public_id,
        schedule_id=schedule.id,
        attempt_number=occurrence.attempt_count,
        attempt_id=f"due-attempt:{occurrence.claim_token}",
        claim_token=str(occurrence.claim_token),
        runtime_owner=str(occurrence.owner),
        started_at=occurrence.claimed_at or now,
        lease_expires_at=occurrence.lease_expires_at or now,
        deadline_at=occurrence.deadline_at or now,
    )
    _append_receipt(
        session,
        occurrence,
        schedule,
        claim,
        execution_outcome="failed",
        handler_result=None,
        error_code="claim_abandoned_exhausted",
        safe_next_step="inspect_processing_failure",
        finished_at=now,
    )
    _release_occurrence(
        occurrence,
        state="failed",
        error_code="claim_abandoned_exhausted",
    )


def _fail_missed_latest_only(
    session: Session,
    schedule: DueWorkSchedule,
    *,
    latest_due_at: datetime,
    now: datetime,
) -> None:
    older = session.scalars(
        select(DueWorkOccurrence)
        .where(
            DueWorkOccurrence.scheduled_job_id == schedule.id,
            DueWorkOccurrence.due_at < latest_due_at,
            or_(
                DueWorkOccurrence.state.in_(("pending", "retry_due")),
                (
                    (DueWorkOccurrence.state == "claimed")
                    & (DueWorkOccurrence.lease_expires_at <= now)
                ),
            ),
        )
        .order_by(DueWorkOccurrence.due_at, DueWorkOccurrence.id)
        .with_for_update(skip_locked=True)
    ).all()
    for occurrence in older:
        if occurrence.state == "claimed":
            attempt_number = occurrence.attempt_count
            attempt_id = f"due-attempt:{occurrence.claim_token}"
            runtime_owner = str(occurrence.owner)
            started_at = occurrence.claimed_at or occurrence.due_at
        else:
            occurrence.attempt_count += 1
            attempt_number = occurrence.attempt_count
            attempt_id = (
                f"due-attempt:missed:{occurrence.occurrence_key[:20]}:"
                f"{attempt_number}"
            )
            runtime_owner = "runtime:scheduler"
            started_at = occurrence.due_at
        claim = DueWorkClaim(
            occurrence_id=occurrence.id,
            occurrence_public_id=occurrence.public_id,
            schedule_id=schedule.id,
            attempt_number=attempt_number,
            attempt_id=attempt_id,
            claim_token=str(occurrence.claim_token or "missed"),
            runtime_owner=runtime_owner,
            started_at=started_at,
            lease_expires_at=now,
            deadline_at=now,
        )
        _append_receipt(
            session,
            occurrence,
            schedule,
            claim,
            execution_outcome="failed",
            handler_result=None,
            error_code="missed_run_latest_only",
            safe_next_step="none",
            finished_at=now,
        )
        _release_occurrence(
            occurrence,
            state="failed",
            error_code="missed_run_latest_only",
        )


def _record_abandoned_retry(
    session: Session,
    occurrence: DueWorkOccurrence,
    schedule: DueWorkSchedule,
    *,
    now: datetime,
) -> None:
    claim = DueWorkClaim(
        occurrence_id=occurrence.id,
        occurrence_public_id=occurrence.public_id,
        schedule_id=schedule.id,
        attempt_number=occurrence.attempt_count,
        attempt_id=f"due-attempt:{occurrence.claim_token}",
        claim_token=str(occurrence.claim_token),
        runtime_owner=str(occurrence.owner),
        started_at=occurrence.claimed_at or now,
        lease_expires_at=occurrence.lease_expires_at or now,
        deadline_at=occurrence.deadline_at or now,
    )
    _append_receipt(
        session,
        occurrence,
        schedule,
        claim,
        execution_outcome="retry_due",
        handler_result=None,
        error_code="claim_abandoned",
        safe_next_step="recovered_by_runtime",
        finished_at=now,
    )
    _release_occurrence(
        occurrence,
        state="retry_due",
        error_code="claim_abandoned",
    )
    occurrence.next_attempt_at = now


def _release_occurrence(
    occurrence: DueWorkOccurrence,
    *,
    state: str,
    error_code: str | None,
) -> None:
    occurrence.state = state
    occurrence.owner = None
    occurrence.claim_token = None
    occurrence.claimed_at = None
    occurrence.lease_expires_at = None
    occurrence.deadline_at = None
    occurrence.last_error_code = error_code
    if state != "retry_due":
        occurrence.next_attempt_at = None


def _run_result(
    receipt: DueWorkReceipt,
    *,
    occurrence: DueWorkOccurrence,
    schedule: DueWorkSchedule,
) -> DueWorkRunResult:
    return DueWorkRunResult(
        occurrence_id=receipt.occurrence_id,
        receipt_id=receipt.id,
        occurrence_public_id=occurrence.public_id,
        receipt_public_id=receipt.public_id,
        job_public_id=schedule.public_id,
        attempt_id=receipt.attempt_id,
        project_id=schedule.project_id,
        handler_key=schedule.handler_key,
        configuration_version=schedule.configuration_version,
        input_identity_sha256=schedule.input_identity_sha256,
        execution_outcome=receipt.execution_outcome,
        handler_result=receipt.handler_result_json,
        error_code=receipt.error_code,
        safe_next_step=receipt.safe_next_step,
    )


def _validate_owner(owner: str) -> None:
    if not isinstance(owner, str) or _RUNTIME_OWNER.fullmatch(owner) is None:
        raise DueWorkRefusal("Due Work runtime owner must be a namespaced runtime id")


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DueWorkRefusal("Due Work clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()
