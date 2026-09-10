"""One supervised runtime for bounded operational work and crash recovery.

Feature-owned schedulers were rejected because they duplicate leases, retries,
clocks, and failure semantics while hiding production ownership. This module
keeps a small interface over one durable occurrence/claim/receipt lifecycle.
Persisted rows select only handlers in the server-owned registry below; they can
never name imports, commands, arbitrary destinations, or model tools.

The runtime keeps the lease; a handler owns its declaration. Each handler
publishes one ``HandlerRegistration`` (``corridor.due_work_contract``) next to
its effectful function, saying how its declaration is validated, how a persisted
row is rebuilt and re-checked, and what to run. The table in
``_SERVER_OWNED_HANDLERS`` is the closed set of keys a persisted row may select,
and it is the only place a handler module is named; it is resolved lazily so
importing the runtime does not import fifteen feature modules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import import_module
import logging
import re
from types import MappingProxyType
from typing import Any, ClassVar, Protocol
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor import digests
from corridor.document_notifications import DOCUMENT_NOTIFICATION_HANDLER
from corridor.due_work_contract import (
    COHORT_IDENTITY,
    DECLARED_IDENTITY,
    DueWorkRefusal,
    DueWorkScheduling,
    HandlerRegistration,
    ResolvedSchedule,
    SHA256,
    ValidatedDeclaration,
    aware_utc,
    gate7_configuration,
    iso_timestamp,
    previous_completed_reading,
    validate_scheduling,
)
from corridor.models import (
    Document,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    ExtractionRun,
    Project,
)
from corridor.notifications import (
    ASSIGNMENT_NOTIFICATION_HANDLER,
    DUE_ACTION_NOTIFICATION_HANDLER,
)
from corridor.telemetry import correlation_scope, log_event


_LOG = logging.getLogger("corridor.due_work")

# The nominal interval between one schedule's occurrence slots. An operational
# reader judges worker freshness against it (`corridor.operational_health`);
# where a slot actually falls stays with `_due_slot`.
CADENCE_INTERVAL_SECONDS: Mapping[str, int] = MappingProxyType(
    {"hourly": 3600, "weekly": 604_800}
)

HANDLER_PROCESSING_HEALTH = "processing_health"
HANDLER_PROJECT_PROCESSING = "project_processing"
HANDLER_REVISION_RECONCILIATION = "revision_reconciliation"
HANDLER_LOCATION_DISCOVERY = "location_discovery"
# Deliver the one new-assignment interruption category through this runtime
# (#351). The delivery logic, this key, and the declaration live in
# ``corridor.notifications``; the runtime depends on that module, never the
# reverse.
HANDLER_ASSIGNMENT_NOTIFICATION = ASSIGNMENT_NOTIFICATION_HANDLER
# Derive and deliver the due-action categories — soon/past-due Next Action
# reminders, urgent-overdue escalation, and daily summaries — through this same
# runtime (#352). Same ownership rule as the assignment handler above.
HANDLER_DUE_ACTION_NOTIFICATION = DUE_ACTION_NOTIFICATION_HANDLER
HANDLER_EVENT_ADMISSION_REPROOF = "event_admission_reproof"
HANDLER_EVIDENCE_OUTCOME_CAPTURE = "evidence_outcome_capture"
# Retain a weekly Coordination Report reading and prepare its external PDF. The
# string matches ``report_publication.HANDLER_KEY``; the execution and the
# declaration live in that lower module, which the runtime imports lazily so no
# import cycle forms.
HANDLER_REPORT_PUBLICATION = "report_publication"
# Discover and deliver the two document-related interruption categories (#353):
# a Documentation Review that lost applicable support, and a source transition
# affecting a current Commitment or a relocation/removal/abandonment Constraint.
# The discovery and delivery logic, this key, and the declaration live in
# ``corridor.document_notifications``; the runtime depends on that module.
HANDLER_DOCUMENT_NOTIFICATION = DOCUMENT_NOTIFICATION_HANDLER
# The four recurring passes a live pilot needs (#488). Each string matches its
# lower module's ``HANDLER_KEY``; the execution lives there and the runtime
# imports it lazily so no import cycle forms.
# Run one confirmed preparation request through #529's three phases (#690).
# Unlike every handler above it, this one is *published* rather than
# scheduled: a cadence slot cannot say which request it is for, and one
# opaque occurrence processing fifty requests would give the fifty one shared
# lease, one shared retry budget and one shared receipt. Its execution lives
# in ``release_preparation_supervisor``, which the runtime imports lazily so
# no import cycle forms.
HANDLER_RELEASE_PREPARATION = "release_preparation"
HANDLER_CONNECTOR_POLLING = "connector_polling"
HANDLER_DELTA_GENERATION = "delta_generation"
HANDLER_REPORT_PREPARATION = "report_preparation"
HANDLER_RETENTION_SWEEP = "retention_sweep"
# Whole-customer-environment export-and-destroy disposition (#514, ADR-0080/0083).
# Unlike every handler above, this runs against the control plane, not a
# per-project customer-DB schedule: a Due Work schedule cannot live inside the
# database being destroyed. Its effectful contract (an effectful-shaped
# orchestrator and this idempotency key) therefore lives in
# ``environment_disposition`` rather than in the registry below, which enqueues
# per-project customer-database occurrences.
HANDLER_ENVIRONMENT_DISPOSITION = "environment_disposition"
# The upper safety ceiling for a declared model spend. A processing schedule
# must declare a positive budget (never a silent zero); it may not exceed this.
_PROJECT_PROCESSING_TOKEN_CEILING = 100_000_000
_RUNTIME_OWNER = re.compile(r"^runtime:[A-Za-z0-9][A-Za-z0-9._:-]{1,119}$")


class StaleDueWorkClaim(DueWorkRefusal):
    """A worker tried to finalize an occurrence it no longer owns."""


class Clock(Protocol):
    def now(self) -> datetime: ...

@dataclass(frozen=True)
class ProcessingHealthDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables the read-only health pass.

    It adds no scope of its own: the project and the stored processing facts it
    counts are the whole of it, so every field it carries is the shared
    scheduling envelope the runtime owns.
    """

    handler_key: ClassVar[str] = HANDLER_PROCESSING_HEALTH

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
class ProjectProcessingDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables project processing.

    Processing reads models, so its bounds differ from the read-only health
    handler: the claim lease and deadline are longer, and a positive model
    spending budget must be declared explicitly rather than left at a silent
    zero. Scope names the exact project and the deployed extractor identity;
    authorized destinations stay empty and concurrency stays one.
    """

    handler_key: ClassVar[str] = HANDLER_PROJECT_PROCESSING

    extractor_identity: str

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
class RevisionReconciliationDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables automatic revision work.

    Revision reconciliation is deterministic — it converges on an already-produced
    exact comparison and applies the released Automatic Support Update Rules — so
    it authorizes no model spending: its ``model_token_budget`` must be a declared
    zero, never a silent one, and its resource bounds match the read-only health
    handler rather than the model-spending processing pass. Scope names the exact
    project and the two policy identities the pass is bound to: the comparison
    ``matcher_identity`` and the support-transfer ``support_rule_identity``.
    Authorized destinations stay empty and concurrency stays one.
    """

    handler_key: ClassVar[str] = HANDLER_REVISION_RECONCILIATION

    matcher_identity: str
    support_rule_identity: str

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        matcher_identity: str,
        support_rule_identity: str,
        starts_at: datetime,
    ) -> "RevisionReconciliationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            matcher_identity=matcher_identity,
            support_rule_identity=support_rule_identity,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


@dataclass(frozen=True)
class EvidenceOutcomeCaptureDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables cutoff-correct capture.

    The domain half of the contract — the exact frozen cohort, the sealed model
    and prompt identities, the observation window, cutoff, protection end, and
    retained-history coverage — is declared and validated in
    ``evidence_investigator_capture`` and content-addressed by
    ``observation_contract_sha256``. This schedule carries only the operational
    envelope plus that pointer, so the occurrence identity binds the project,
    cohort, and observation contract (and therefore its intended cutoff and
    membership). Capture reads no model, so ``model_token_budget`` must be a
    declared zero; it commits only its own association records, so authorized
    destinations stay empty and concurrency stays one.
    """

    handler_key: ClassVar[str] = HANDLER_EVIDENCE_OUTCOME_CAPTURE

    cohort_id: str
    observation_contract_sha256: str

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        cohort_id: str,
        observation_contract_sha256: str,
        starts_at: datetime,
    ) -> "EvidenceOutcomeCaptureDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            cohort_id=cohort_id,
            observation_contract_sha256=observation_contract_sha256,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
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
class EffectfulContext:
    """What an effectful handler needs to own its durable work and be recovered.

    The handler commits its domain work through ``session_factory`` before the
    runtime finalizes the claim, so a stale-claim or deadline failure at finalize
    only re-runs work that is already durable and idempotent. ``schedule`` is the
    resolved schedule as data — the project, the configuration version, the
    validated scope and the declared bounds — so a handler never re-opens a
    session to read the row it was claimed for.
    """

    session_factory: Any
    schedule: ResolvedSchedule
    claim: "DueWorkClaim"
    clock: "Clock"
    registry: Mapping[str, HandlerRegistration]


# The registration value every handler publishes. It was called
# ``HandlerContract`` while it carried only the resource envelope and the
# callable; the old name stays as an alias because in-process callers build
# their own registry and pass it to `run_due_work_once`.
HandlerContract = HandlerRegistration

# Every handler a persisted row may select, and the module that publishes its
# registration. This table is the server-owned registry: a row names a key in
# it and nothing else, so it can never reach an arbitrary import. The modules
# are resolved on first use rather than at import, so the runtime does not drag
# fifteen feature modules (and their model clients) into every process that
# enqueues work — and the four registrations the runtime publishes itself are
# named the same way as the eleven that live with their handler.
_SERVER_OWNED_HANDLERS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        HANDLER_PROCESSING_HEALTH: (
            "corridor.due_work",
            "PROCESSING_HEALTH_REGISTRATION",
        ),
        HANDLER_PROJECT_PROCESSING: (
            "corridor.due_work",
            "PROJECT_PROCESSING_REGISTRATION",
        ),
        HANDLER_REVISION_RECONCILIATION: (
            "corridor.due_work",
            "REVISION_RECONCILIATION_REGISTRATION",
        ),
        HANDLER_EVIDENCE_OUTCOME_CAPTURE: (
            "corridor.due_work",
            "EVIDENCE_OUTCOME_CAPTURE_REGISTRATION",
        ),
        HANDLER_LOCATION_DISCOVERY: (
            "corridor.location_discovery",
            "DUE_WORK_REGISTRATION",
        ),
        HANDLER_ASSIGNMENT_NOTIFICATION: (
            "corridor.notifications",
            "ASSIGNMENT_DUE_WORK_REGISTRATION",
        ),
        HANDLER_DUE_ACTION_NOTIFICATION: (
            "corridor.notifications",
            "DUE_ACTION_DUE_WORK_REGISTRATION",
        ),
        HANDLER_DOCUMENT_NOTIFICATION: (
            "corridor.document_notifications",
            "DUE_WORK_REGISTRATION",
        ),
        HANDLER_EVENT_ADMISSION_REPROOF: (
            "corridor.event_admission_reproof",
            "DUE_WORK_REGISTRATION",
        ),
        HANDLER_REPORT_PUBLICATION: (
            "corridor.report_publication",
            "DUE_WORK_REGISTRATION",
        ),
        HANDLER_CONNECTOR_POLLING: (
            "corridor.connector_polling",
            "DUE_WORK_REGISTRATION",
        ),
        HANDLER_DELTA_GENERATION: (
            "corridor.delta_generation",
            "DUE_WORK_REGISTRATION",
        ),
        HANDLER_REPORT_PREPARATION: (
            "corridor.report_preparation",
            "DUE_WORK_REGISTRATION",
        ),
        HANDLER_RELEASE_PREPARATION: (
            "corridor.release_preparation_supervisor",
            "DUE_WORK_REGISTRATION",
        ),
        HANDLER_RETENTION_SWEEP: (
            "corridor.retention_sweep",
            "DUE_WORK_REGISTRATION",
        ),
    }
)


class _ServerOwnedRegistry(Mapping[str, HandlerRegistration]):
    """The registry as a mapping, resolving each registration once, on demand."""

    def __init__(self) -> None:
        self._resolved: dict[str, HandlerRegistration] = {}

    def __getitem__(self, key: str) -> HandlerRegistration:
        registration = self._resolved.get(key)
        if registration is None:
            if key not in _SERVER_OWNED_HANDLERS:
                raise KeyError(key)
            module_name, attribute = _SERVER_OWNED_HANDLERS[key]
            registration = getattr(import_module(module_name), attribute)
            if registration.key != key:
                raise DueWorkRefusal(
                    "server-owned Due Work registration does not match its key"
                )
            self._resolved[key] = registration
        return registration

    def __iter__(self) -> Iterator[str]:
        return iter(_SERVER_OWNED_HANDLERS)

    def __len__(self) -> int:
        return len(_SERVER_OWNED_HANDLERS)


HANDLER_REGISTRY: Mapping[str, HandlerRegistration] = _ServerOwnedRegistry()

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
        "observed_at": iso_timestamp(observed_at),
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

    from corridor.pipeline import production_extraction_routes
    from corridor.project_processing import process_project, summarize_pass

    project_id = context.schedule.project_id
    configuration_version = context.schedule.configuration_version
    with production_extraction_routes() as select_route:
        result = process_project(
            context.session_factory,
            project_id=project_id,
            select_route=select_route,
            clock=context.clock,
        )
    return summarize_pass(
        result,
        configuration_version=configuration_version,
        observed_at=aware_utc(context.clock.now()),
    )


def _revision_reconciliation_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Run the bounded revision reconciliation for a claimed occurrence.

    The pass commits its Revision Comparisons, released support updates, and the
    downstream Record Inclusion handoff durably through the session factory; this
    wrapper only adapts the runtime's claim into a ``reconcile_project_revisions``
    call over the declared matcher identity and summarizes the result into a
    bounded receipt. It reads no model.
    """

    from corridor.revision_reconciliation import (
        reconcile_project_revisions,
        summarize_revision_reconciliation,
    )

    project_id = context.schedule.project_id
    configuration_version = context.schedule.configuration_version
    matcher_identity = context.schedule.scope.get("matcher_identity", "")
    result = reconcile_project_revisions(
        context.session_factory,
        project_id=project_id,
        matcher_version=matcher_identity,
        clock=context.clock,
    )
    return summarize_revision_reconciliation(
        result,
        configuration_version=configuration_version,
        observed_at=aware_utc(context.clock.now()),
    )


def _evidence_outcome_capture_effectful(context: EffectfulContext) -> dict[str, Any]:
    """Run the bounded cutoff-correct outcome capture for a claimed occurrence.

    The pass commits its per-case association records durably through the session
    factory before the runtime finalizes the claim, so recovering and re-running
    this occurrence only re-derives associations that are already durable and
    idempotent. It adapts the runtime's claim into a ``run_outcome_capture`` call
    over the schedule's declared contract and summarizes the pass into a bounded
    receipt. It reads no model and releases no protection.
    """

    from corridor.evidence_investigator_capture import run_outcome_capture

    return run_outcome_capture(
        context.session_factory,
        project_id=context.schedule.project_id,
        contract_sha256=context.schedule.scope.get("observation_contract_sha256", ""),
        configuration_version=context.schedule.configuration_version,
        clock=context.clock,
    )


def configure_processing_health(
    session: Session,
    declaration: ProcessingHealthDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 processing-health declaration."""

    return _configure(session, declaration, HANDLER_PROCESSING_HEALTH, now=now)


def configure_project_processing(
    session: Session,
    declaration: ProjectProcessingDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 project-processing declaration."""

    return _configure(session, declaration, HANDLER_PROJECT_PROCESSING, now=now)


def configure_revision_reconciliation(
    session: Session,
    declaration: RevisionReconciliationDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 revision-reconciliation declaration."""

    return _configure(session, declaration, HANDLER_REVISION_RECONCILIATION, now=now)


def configure_location_discovery(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 connected-location declaration (#350)."""

    return _configure(session, declaration, HANDLER_LOCATION_DISCOVERY, now=now)


def configure_assignment_notification(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 assignment-notification declaration."""

    return _configure(session, declaration, HANDLER_ASSIGNMENT_NOTIFICATION, now=now)


def configure_due_action_notification(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 due-action-notification declaration."""

    return _configure(session, declaration, HANDLER_DUE_ACTION_NOTIFICATION, now=now)


def configure_document_notification(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 document-notification declaration."""

    return _configure(session, declaration, HANDLER_DOCUMENT_NOTIFICATION, now=now)


def configure_event_admission_reproof(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 stale-class re-proof declaration."""

    return _configure(session, declaration, HANDLER_EVENT_ADMISSION_REPROOF, now=now)


def configure_evidence_outcome_capture(
    session: Session,
    declaration: EvidenceOutcomeCaptureDeclaration,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 outcome-capture declaration."""

    return _configure(session, declaration, HANDLER_EVIDENCE_OUTCOME_CAPTURE, now=now)


def configure_report_publication(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 report-publication declaration."""

    return _configure(session, declaration, HANDLER_REPORT_PUBLICATION, now=now)


def configure_connector_polling(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 connector-polling declaration."""

    return _configure(session, declaration, HANDLER_CONNECTOR_POLLING, now=now)


def configure_delta_generation(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 delta-generation declaration."""

    return _configure(session, declaration, HANDLER_DELTA_GENERATION, now=now)


def configure_report_preparation(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 report-preparation declaration."""

    return _configure(session, declaration, HANDLER_REPORT_PREPARATION, now=now)


def configure_release_preparation(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one project's enabled preparation supervisor."""

    return _configure(session, declaration, HANDLER_RELEASE_PREPARATION, now=now)


def configure_retention_sweep(
    session: Session,
    declaration: object,
    *,
    now: datetime,
) -> DueWorkSchedule:
    """Validate and retain one enabled gate-7 Class B retention-sweep declaration."""

    return _configure(session, declaration, HANDLER_RETENTION_SWEEP, now=now)


def configure_due_work(
    session: Session,
    declaration: object,
    *,
    now: datetime,
    registry: Mapping[str, HandlerRegistration] = HANDLER_REGISTRY,
) -> DueWorkSchedule:
    """Validate and retain any server-owned Due Work declaration.

    A declaration says which handler it is for, and the registry says whether
    that handler is server-owned; nothing else about the object is trusted.
    """

    return _configure(
        session,
        declaration,
        getattr(declaration, "handler_key", ""),
        now=now,
        registry=registry,
    )


def _configure(
    session: Session,
    declaration: object,
    handler_key: str,
    *,
    now: datetime,
    registry: Mapping[str, HandlerRegistration] = HANDLER_REGISTRY,
) -> DueWorkSchedule:
    """Validate one declaration through its own registration, and retain it.

    Every ``configure_`` seam above is this one path: the handler owns what its
    declaration means, and the runtime owns idempotency, supersession, and
    persistence. A refused declaration writes no schedule.
    """

    registration = _handler(handler_key, registry)
    if (
        registration.declaration_type is None
        or not isinstance(declaration, registration.declaration_type)
        or getattr(declaration, "handler_key", "") != handler_key
        or registration.validate is None
    ):
        raise DueWorkRefusal("Due Work declaration is not server-owned")
    validated = registration.validate(declaration)
    if registration.configure_check is not None:
        registration.configure_check(session, declaration)
    return _retain_due_work_schedule(
        session,
        declaration,
        now=now,
        handler_key=handler_key,
        validated=validated,
        disable_same_input_only=registration.disable_same_input_only,
    )


def _retain_due_work_schedule(
    session: Session,
    declaration: DueWorkScheduling,
    *,
    now: datetime,
    handler_key: str,
    validated: ValidatedDeclaration,
    disable_same_input_only: bool = False,
) -> DueWorkSchedule:
    """Own idempotency, supersession, and persistence for every declaration."""
    now = aware_utc(now)
    project_id = declaration.project_id
    if session.get(Project, project_id) is None:
        raise DueWorkRefusal(f"project {project_id} does not exist")
    configuration_sha256 = _sha256(validated.configuration)
    input_identity_sha256 = _sha256(validated.input_identity)
    existing = session.scalar(
        select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == project_id,
            DueWorkSchedule.handler_key == handler_key,
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

    active_query = select(DueWorkSchedule).where(
        DueWorkSchedule.project_id == project_id,
        DueWorkSchedule.handler_key == handler_key,
        DueWorkSchedule.disabled_at.is_(None),
    )
    if disable_same_input_only:
        active_query = active_query.where(
            DueWorkSchedule.input_identity_sha256 == input_identity_sha256
        )
    for prior in session.scalars(active_query).all():
        prior.disabled_at = now

    schedule = DueWorkSchedule(
        public_id=f"due-job:{configuration_sha256[:24]}",
        project_id=project_id,
        handler_key=handler_key,
        configuration_version=declaration.configuration_version,
        scope_json=validated.scope,
        configuration_json=validated.configuration,
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


def enqueue_due_work(
    session: Session,
    *,
    now: datetime,
    registry: Mapping[str, HandlerRegistration] = HANDLER_REGISTRY,
) -> tuple[DueWorkOccurrence, ...]:
    """Coalesce the latest due occurrence for every active validated schedule."""

    now = aware_utc(now)
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
        _validate_stored_schedule(schedule, registry)
        contract = registry[schedule.handler_key]
        if contract.publish is not None:
            # A published handler has no slot to coalesce onto: it creates one
            # occurrence per durable request, so a request that arrived while
            # an earlier one was running is worked rather than folded into it.
            keys.extend(contract.publish(session, schedule, now))
            continue
        due_at = _due_slot(schedule, now)
        if due_at is None:
            continue
        occurrence_key = _sha256(
            {
                "handler": schedule.handler_key,
                "scope": schedule.scope_json,
                "configuration_version": schedule.configuration_version,
                "configuration_sha256": schedule.configuration_sha256,
                "input_identity_sha256": schedule.input_identity_sha256,
                "due_at": iso_timestamp(due_at),
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
    registry: Mapping[str, HandlerRegistration] = HANDLER_REGISTRY,
) -> DueWorkClaim | None:
    """Claim one ready occurrence, recovering or retaining abandoned attempts."""

    now = aware_utc(now)
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
        _validate_stored_schedule(schedule, registry)
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
    registry: Mapping[str, HandlerRegistration] = HANDLER_REGISTRY,
) -> DueWorkRunResult:
    """Finalize a live claim with one validated append-only completed receipt."""

    now = aware_utc(now)
    occurrence, schedule = _locked_live_claim(session, claim, now=now)
    contract = _handler(schedule.handler_key, registry)
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

    now = aware_utc(now)
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
    registry: Mapping[str, HandlerRegistration] = HANDLER_REGISTRY,
) -> DueWorkRunResult | None:
    """Claim, execute outside the claim transaction, and reconcile one attempt."""

    started_at = aware_utc(clock.now())
    with session_factory() as claiming:
        with claiming.begin():
            claim = claim_due_work(
                claiming, now=started_at, owner=owner, registry=registry
            )
    if claim is None:
        return None

    # Everything this attempt logs — the handler's own lines included — is
    # bound to the occurrence and attempt the receipt will name, so a log line
    # and the durable receipt can be read together (#491A).
    with correlation_scope(
        job_id=claim.occurrence_public_id,
        attempt_id=claim.attempt_id,
        attempt_number=claim.attempt_number,
        runtime_owner=claim.runtime_owner,
    ):
        try:
            with session_factory() as reading:
                schedule = reading.get(DueWorkSchedule, claim.schedule_id)
                if schedule is None or schedule.handler_key not in registry:
                    raise DueWorkRefusal("claimed handler is not server-owned")
                contract = registry[schedule.handler_key]
                resolved = resolved_schedule(schedule)
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
                        schedule=resolved,
                        claim=claim,
                        clock=clock,
                        registry=registry,
                    )
                )
            finished_at = aware_utc(clock.now())
            with session_factory() as finalizing:
                with finalizing.begin():
                    if finished_at > claim.deadline_at:
                        # The domain work already committed durably and is
                        # idempotent, so recovering and re-running this
                        # occurrence is safe.
                        return _logged_attempt(
                            fail_due_work(
                                finalizing,
                                claim,
                                error_code="deadline_exceeded",
                                now=finished_at,
                            )
                        )
                    return _logged_attempt(
                        complete_due_work(
                            finalizing,
                            claim,
                            handler_result=handler_result,
                            now=finished_at,
                            registry=registry,
                        )
                    )
        except StaleDueWorkClaim:
            raise
        except Exception:
            failed_at = aware_utc(clock.now())
            with session_factory() as finalizing:
                with finalizing.begin():
                    return _logged_attempt(
                        fail_due_work(
                            finalizing,
                            claim,
                            error_code="handler_execution_failed",
                            now=failed_at,
                        )
                    )


def _logged_attempt(result: DueWorkRunResult) -> DueWorkRunResult:
    """Emit the one operational line for a finalized attempt, and pass it on."""

    log_event(
        _LOG,
        "due_work_attempt",
        level=(
            logging.INFO
            if result.execution_outcome == "completed"
            else logging.WARNING
        ),
        handler=result.handler_key,
        project_id=result.project_id,
        configuration_version=result.configuration_version,
        execution_outcome=result.execution_outcome,
        error_code=result.error_code,
        safe_next_step=result.safe_next_step,
        receipt_id=result.receipt_public_id,
    )
    return result


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
                "due_at": iso_timestamp(item.due_at),
                "state": item.state,
                "attempt_count": item.attempt_count,
                "next_attempt_at": (
                    iso_timestamp(item.next_attempt_at) if item.next_attempt_at else None
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
        tick_at = aware_utc(clock.now())
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
) -> ValidatedDeclaration:
    starts_at = validate_scheduling(
        declaration,
        subject="processing-health",
        claim_ttl_seconds=(30, 900),
    )
    scope = {"project_id": declaration.project_id}
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_PROCESSING_HEALTH,
            scope=scope,
            input_identity={
                "kind": "stored_processing_facts-v1",
                "project_id": declaration.project_id,
            },
            idempotency_contract="read_only_reconcilable",
            starts_at=starts_at,
        ),
        input_identity={
            "handler": HANDLER_PROCESSING_HEALTH,
            "project_id": declaration.project_id,
            "source": "stored_processing_facts-v1",
        },
    )


def _stored_health_declaration(
    stored: ResolvedSchedule,
) -> ProcessingHealthDeclaration:
    return ProcessingHealthDeclaration(**stored.scheduling_fields())


PROCESSING_HEALTH_REGISTRATION = HandlerRegistration(
    key=HANDLER_PROCESSING_HEALTH,
    scope_kind="one_project_stored_processing_facts",
    idempotency_contract="read_only_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=0,
    declaration_type=ProcessingHealthDeclaration,
    validate=_validated_health_declaration,
    stored_declaration=_stored_health_declaration,
    run=_processing_health,
)


def _validated_processing_declaration(
    declaration: ProjectProcessingDeclaration,
) -> ValidatedDeclaration:
    """Validate one project-processing declaration.

    Missing or invalid configuration leaves the handler refused and no schedule
    written; model credentials alone never enable a project sweep. Processing
    reads models, so a positive spending budget must be declared explicitly
    rather than left at a silent zero.
    """

    if not DECLARED_IDENTITY.fullmatch(declaration.extractor_identity):
        raise DueWorkRefusal("project-processing extractor identity is invalid")
    starts_at = validate_scheduling(
        declaration,
        subject="project-processing",
        model_token_budget=(1, _PROJECT_PROCESSING_TOKEN_CEILING),
    )
    scope = {
        "project_id": declaration.project_id,
        "extractor_identity": declaration.extractor_identity,
    }
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_PROJECT_PROCESSING,
            scope=scope,
            input_identity={
                "kind": "registered_project_extraction-v1",
                "project_id": declaration.project_id,
                "extractor_identity": declaration.extractor_identity,
            },
            idempotency_contract="at_least_once_reconcilable",
            starts_at=starts_at,
        ),
        input_identity={
            "handler": HANDLER_PROJECT_PROCESSING,
            "project_id": declaration.project_id,
            "extractor_identity": declaration.extractor_identity,
        },
    )


def _stored_processing_declaration(
    stored: ResolvedSchedule,
) -> ProjectProcessingDeclaration:
    return ProjectProcessingDeclaration(
        **stored.scheduling_fields(),
        extractor_identity=stored.scope.get("extractor_identity", ""),
    )


PROJECT_PROCESSING_REGISTRATION = HandlerRegistration(
    key=HANDLER_PROJECT_PROCESSING,
    scope_kind="one_registered_project_extraction",
    idempotency_contract="at_least_once_reconcilable",
    max_result_bytes=8192,
    model_token_budget=_PROJECT_PROCESSING_TOKEN_CEILING,
    notification_budget=0,
    declaration_type=ProjectProcessingDeclaration,
    validate=_validated_processing_declaration,
    stored_declaration=_stored_processing_declaration,
    run_effectful=_project_processing_effectful,
)


def _validated_revision_reconciliation_declaration(
    declaration: RevisionReconciliationDeclaration,
) -> ValidatedDeclaration:
    """Validate one revision-reconciliation declaration.

    A deterministic handler still requires an explicit gate-7 declaration
    before it may run, and it authorizes no model spending: its budget must be
    a declared zero, never a silent one.
    """

    if not DECLARED_IDENTITY.fullmatch(declaration.matcher_identity):
        raise DueWorkRefusal("revision-reconciliation matcher identity is invalid")
    if not DECLARED_IDENTITY.fullmatch(declaration.support_rule_identity):
        raise DueWorkRefusal(
            "revision-reconciliation support rule identity is invalid"
        )
    starts_at = validate_scheduling(
        declaration, subject="revision-reconciliation"
    )
    scope = {
        "project_id": declaration.project_id,
        "matcher_identity": declaration.matcher_identity,
        "support_rule_identity": declaration.support_rule_identity,
    }
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_REVISION_RECONCILIATION,
            scope=scope,
            input_identity={
                "kind": "registered_project_revision-v1",
                "project_id": declaration.project_id,
                "matcher_identity": declaration.matcher_identity,
                "support_rule_identity": declaration.support_rule_identity,
            },
            idempotency_contract="at_least_once_reconcilable",
            starts_at=starts_at,
        ),
        input_identity={
            "handler": HANDLER_REVISION_RECONCILIATION,
            "project_id": declaration.project_id,
            "matcher_identity": declaration.matcher_identity,
            "support_rule_identity": declaration.support_rule_identity,
        },
    )


def _stored_revision_reconciliation_declaration(
    stored: ResolvedSchedule,
) -> RevisionReconciliationDeclaration:
    return RevisionReconciliationDeclaration(
        **stored.scheduling_fields(),
        matcher_identity=stored.scope.get("matcher_identity", ""),
        support_rule_identity=stored.scope.get("support_rule_identity", ""),
    )


REVISION_RECONCILIATION_REGISTRATION = HandlerRegistration(
    key=HANDLER_REVISION_RECONCILIATION,
    scope_kind="one_registered_project_revision",
    idempotency_contract="at_least_once_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=0,
    declaration_type=RevisionReconciliationDeclaration,
    validate=_validated_revision_reconciliation_declaration,
    stored_declaration=_stored_revision_reconciliation_declaration,
    run_effectful=_revision_reconciliation_effectful,
)


def _validated_evidence_outcome_capture_declaration(
    declaration: EvidenceOutcomeCaptureDeclaration,
) -> ValidatedDeclaration:
    """Validate one outcome-capture declaration.

    An elapsed interval or the mere existence of frozen cases never enables
    capture. The referenced observation contract is declared and validated
    separately in ``evidence_investigator_capture``; this seam only binds the
    operational envelope to the project, cohort, and contract identity.
    """

    if not COHORT_IDENTITY.fullmatch(declaration.cohort_id):
        raise DueWorkRefusal("outcome-capture cohort identity is invalid")
    if not SHA256.fullmatch(declaration.observation_contract_sha256):
        raise DueWorkRefusal("outcome-capture observation contract must be SHA-256")
    starts_at = validate_scheduling(declaration, subject="outcome-capture")
    scope = {
        "project_id": declaration.project_id,
        "cohort_id": declaration.cohort_id,
        "observation_contract_sha256": declaration.observation_contract_sha256,
    }
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_EVIDENCE_OUTCOME_CAPTURE,
            scope=scope,
            input_identity={
                "kind": "declared_capture_cohort-v1",
                "project_id": declaration.project_id,
                "cohort_id": declaration.cohort_id,
                "observation_contract_sha256": (
                    declaration.observation_contract_sha256
                ),
            },
            idempotency_contract="at_least_once_reconcilable",
            starts_at=starts_at,
        ),
        input_identity={
            "handler": HANDLER_EVIDENCE_OUTCOME_CAPTURE,
            "project_id": declaration.project_id,
            "cohort_id": declaration.cohort_id,
            "observation_contract_sha256": declaration.observation_contract_sha256,
        },
    )


def _stored_evidence_outcome_capture_declaration(
    stored: ResolvedSchedule,
) -> EvidenceOutcomeCaptureDeclaration:
    return EvidenceOutcomeCaptureDeclaration(
        **stored.scheduling_fields(),
        cohort_id=stored.scope.get("cohort_id", ""),
        observation_contract_sha256=stored.scope.get(
            "observation_contract_sha256", ""
        ),
    )


EVIDENCE_OUTCOME_CAPTURE_REGISTRATION = HandlerRegistration(
    key=HANDLER_EVIDENCE_OUTCOME_CAPTURE,
    scope_kind="one_declared_capture_cohort",
    idempotency_contract="at_least_once_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=0,
    declaration_type=EvidenceOutcomeCaptureDeclaration,
    validate=_validated_evidence_outcome_capture_declaration,
    stored_declaration=_stored_evidence_outcome_capture_declaration,
    run_effectful=_evidence_outcome_capture_effectful,
    disable_same_input_only=True,
)


def _due_slot(schedule: DueWorkSchedule, now: datetime) -> datetime | None:
    """The current occurrence slot for one schedule's cadence, or ``None``.

    Hourly handlers keep their existing UTC-hour slot.  A weekly publication
    aligns to its declared ``starts_at``, so every trigger within one week
    resolves to the same slot and coalesces onto one occurrence rather than
    producing a snapshot each hour; ``latest_only`` recovery then measures a
    missed week from that same weekly boundary.
    """

    now = aware_utc(now)
    starts_at = aware_utc(schedule.starts_at)
    if schedule.cadence == "weekly":
        if now < starts_at:
            return None
        weeks = (now - starts_at) // timedelta(days=7)
        return starts_at + weeks * timedelta(days=7)
    slot = now.replace(minute=0, second=0, microsecond=0)
    return slot if slot >= starts_at else None


def resolved_schedule(schedule: DueWorkSchedule) -> ResolvedSchedule:
    """One persisted schedule as data, resolved once by the runtime.

    Revalidation rebuilds a declaration from this, and an effectful handler is
    handed it in place of the row, so neither has to re-open a session to learn
    what it was claimed for.
    """

    return ResolvedSchedule(
        schedule_id=schedule.id,
        handler_key=schedule.handler_key,
        project_id=schedule.project_id,
        configuration_version=schedule.configuration_version,
        scope=dict(schedule.scope_json),
        configuration=dict(schedule.configuration_json),
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


def _validate_stored_schedule(
    schedule: DueWorkSchedule,
    registry: Mapping[str, HandlerRegistration] = HANDLER_REGISTRY,
) -> None:
    """Ask this row's own registration to rebuild its declaration and re-check it.

    A persisted row is only ever trusted to name a server-owned handler key and
    the values that handler's declaration would have produced: the runtime
    rebuilds the declaration from the stored columns, revalidates it, and
    compares the retained configuration, scope, and input identity. Anything
    edited in the database — a cadence, a scope, a budget — fails here rather
    than reaching a handler.
    """

    if schedule.handler_key not in registry:
        raise DueWorkRefusal("persisted Due Work handler is not server-owned")
    if schedule.configuration_sha256 != _sha256(schedule.configuration_json):
        raise DueWorkRefusal("persisted Due Work configuration digest does not match")
    if schedule.configuration_json.get("handler") != schedule.handler_key:
        raise DueWorkRefusal("persisted Due Work configuration handler does not match")
    registration = registry[schedule.handler_key]
    validated = registration.revalidate(resolved_schedule(schedule))
    if schedule.configuration_json != validated.configuration:
        raise DueWorkRefusal("persisted Due Work configuration is unsupported")
    if (
        schedule.scope_json != validated.scope
        or schedule.input_identity_sha256 != _sha256(validated.input_identity)
    ):
        raise DueWorkRefusal("persisted Due Work scope or input identity is invalid")
    if (
        schedule.model_token_budget > registration.model_token_budget
        or schedule.notification_budget > registration.notification_budget
    ):
        raise DueWorkRefusal("persisted Due Work budgets exceed the handler contract")


def _handler(
    key: str,
    registry: Mapping[str, HandlerRegistration] = HANDLER_REGISTRY,
) -> HandlerRegistration:
    registration = registry.get(key)
    if registration is None:
        raise DueWorkRefusal("Due Work handler is not server-owned")
    return registration


def _safe_next_step(contract: HandlerContract, handler_result: dict[str, Any]) -> str:
    """The recovery pointer a completed receipt records, per handler."""

    if contract.key == HANDLER_REPORT_PUBLICATION:
        return (
            "review_prepared_report"
            if handler_result.get("prepared")
            else "review_retained_snapshot"
        )
    if handler_result.get("health") in ("healthy", "held_sealed"):
        return "none"
    if contract.key == HANDLER_PROJECT_PROCESSING:
        return "inspect_processing_attention"
    if contract.key == HANDLER_REVISION_RECONCILIATION:
        return "inspect_revision_attention"
    if contract.key == HANDLER_LOCATION_DISCOVERY:
        return "inspect_location_discovery_attention"
    if contract.key == HANDLER_ASSIGNMENT_NOTIFICATION:
        return "inspect_notification_delivery"
    if contract.key == HANDLER_EVENT_ADMISSION_REPROOF:
        return "inspect_event_admission_reproof_attention"
    if contract.key == HANDLER_EVIDENCE_OUTCOME_CAPTURE:
        return "inspect_capture_attention"
    if contract.key == HANDLER_CONNECTOR_POLLING:
        return "inspect_connector_checkpoint"
    if contract.key == HANDLER_DELTA_GENERATION:
        return "adopt_project_baseline"
    if contract.key == HANDLER_REPORT_PREPARATION:
        return "adopt_project_baseline"
    if contract.key == HANDLER_RELEASE_PREPARATION:
        return "inspect_preparation_attempt"
    if contract.key == HANDLER_RETENTION_SWEEP:
        return "inspect_retention_refusal"
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
    if contract.key == HANDLER_REVISION_RECONCILIATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "did_reconcile",
            "pairs_discovered",
            "comparisons_created",
            "comparisons_reused",
            "comparison_failures",
            "carried",
            "abstained",
            "requested_record_inclusion",
        }
        or result.get("health") not in {"healthy", "revision_attention_required"}
    ):
        raise DueWorkRefusal("revision-reconciliation handler result is invalid")
    if contract.key == HANDLER_LOCATION_DISCOVERY and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "location_id",
            "sealed_excluded",
            "references_observed",
            "new_references",
            "repeat_references",
            "new_archive_urls",
            "authorized_selected",
            "registered",
            "unchanged",
            "drifted",
            "fetch_failures",
            "budget_exhausted",
        }
        or result.get("health")
        not in {"healthy", "attention_required", "held_sealed"}
    ):
        raise DueWorkRefusal("location-discovery handler result is invalid")
    if contract.key == HANDLER_ASSIGNMENT_NOTIFICATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "delivery_enabled",
            "considered",
            "completed",
            "retry_due",
            "failed",
            "uncertain",
            "skipped",
        }
        or result.get("health") not in {"healthy", "delivery_attention_required"}
    ):
        raise DueWorkRefusal("assignment-notification handler result is invalid")
    if contract.key == HANDLER_EVENT_ADMISSION_REPROOF and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "policy_version",
            "proof_status",
            "was_stale",
            "trigger",
            "did_reprove",
            "proof_outcome",
            "activated",
            "acceptance_receipt_id",
            "effective_status",
            "effective_policy_version",
            "detail",
        }
        or result.get("health") not in {"healthy", "recovery_attention_required"}
        or result.get("proof_outcome") not in {"passed", "failed", "not_run"}
    ):
        raise DueWorkRefusal("event-admission-reproof handler result is invalid")
    if contract.key == HANDLER_EVIDENCE_OUTCOME_CAPTURE and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "contract_sha256",
            "cutoff_at",
            "observed_at",
            "health",
            "cases_in_scope",
            "captured_complete",
            "captured_incomplete",
            "before_cutoff_pending",
            "excluded",
            "already_captured",
        }
        or result.get("health") not in {"healthy", "capture_attention_required"}
    ):
        raise DueWorkRefusal("outcome-capture handler result is invalid")
    if contract.key == HANDLER_REPORT_PUBLICATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "provenance_mode",
            "observed_at",
            "evaluated_on",
            "outcome",
            "snapshot_public_id",
            # The accepted Project Record revision the retained reading was
            # produced against (#602), added with the result schema's v2.
            "accepted_revision_id",
            "prepared",
            "prepared_artifact_id",
            "has_prior_release",
            "comparison_window_days",
        }
        or result.get("outcome") != "retained"
        or not isinstance(result.get("prepared"), bool)
    ):
        raise DueWorkRefusal("report-publication handler result is invalid")
    if contract.key == HANDLER_CONNECTOR_POLLING and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "channel",
            "connector_identity",
            "cursor",
            "checkpoint_token",
            "advanced",
            "changes_taken",
            "dispositions",
            "blocked_by",
        }
        or result.get("health") not in {"healthy", "polling_attention_required"}
        or not isinstance(result.get("advanced"), bool)
        # The cursor is the connector configuration's own durable record since
        # ADR-0089, and this is the pass reporting where it stands, so a result
        # that cannot state a token is not a completed pass.
        or not isinstance(result.get("checkpoint_token"), str)
        # What the pass took delivery of, by ADR-0089 disposition, and what
        # held the cursor back.
        or not isinstance(result.get("dispositions"), dict)
        or not isinstance(result.get("blocked_by"), list)
    ):
        raise DueWorkRefusal("connector-polling handler result is invalid")
    if contract.key == HANDLER_DELTA_GENERATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "operating_mode",
            "accepted_baseline_revision",
            "facts_considered",
            "facts_agreed",
            "groups_created",
            "deltas_created",
            "deltas_already_present",
            "through_fact_id",
        }
        or result.get("health")
        not in {"healthy", "delta_generation_attention_required"}
        or result.get("operating_mode") not in {"legacy", "adopted_baseline"}
    ):
        raise DueWorkRefusal("delta-generation handler result is invalid")
    if contract.key == HANDLER_REPORT_PREPARATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "window_start",
            "through_delta_id",
            "through_disposition_id",
            "accepted_revision_id",
            "resolved_accepted",
            "resolved_edited",
            "resolved_rejected",
            "proposed_new",
            "open_actionable",
            "open_deferred",
            "superseded",
        }
        or result.get("health") not in {"healthy", "preparation_attention_required"}
    ):
        raise DueWorkRefusal("report-preparation handler result is invalid")
    if contract.key == HANDLER_RETENTION_SWEEP and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "authorized_by",
            "manifest_public_id",
            "planned",
            "deleted",
            "refusal",
        }
        or result.get("health") not in {"healthy", "retention_attention_required"}
        # A sweep that deleted more than it planned deleted something the
        # dry-run manifest never named.
        or int(result.get("deleted", 0)) > int(result.get("planned", 0))
    ):
        raise DueWorkRefusal("retention-sweep handler result is invalid")
    if contract.key == HANDLER_RELEASE_PREPARATION and (
        set(result)
        != {
            "schema_version",
            "project_id",
            "configuration_version",
            "observed_at",
            "health",
            "request_id",
            "attempt_id",
            "outcome",
            "candidate_id",
            "refusal_code",
            "report_receipt_id",
        }
        or result.get("health")
        not in {"healthy", "preparation_attention_required"}
        # #529's three outcomes and nothing else, and a receipt that named a
        # candidate for anything but a prepared attempt would be a fourth.
        or result.get("outcome") not in {"prepared", "refused", "failed"}
        or (result.get("candidate_id") is not None)
        != (result.get("outcome") == "prepared")
    ):
        raise DueWorkRefusal("release-preparation handler result is invalid")


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
        "started_at": iso_timestamp(started_at),
        "finished_at": iso_timestamp(finished_at),
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


_canonical_json = digests.canonical_json
_sha256 = digests.canonical_sha256
