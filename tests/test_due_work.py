"""Public supervised Due Work behavior through real PostgreSQL transactions.

Private queue-layout tests were rejected because they cannot prove competing
processes, committed leases, recovery, or the public status contract. These
tests use the harness-owned disposable database and only public runtime seams.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError

from corridor.due_work import (
    ProcessingHealthDeclaration,
    DueWorkRefusal,
    HANDLER_PROCESSING_HEALTH,
    HandlerContract,
    StaleDueWorkClaim,
    claim_due_work,
    complete_due_work,
    configure_due_work,
    configure_processing_health,
    due_work_status,
    enqueue_due_work,
    fail_due_work,
    run_due_work_once,
    supervise_due_work,
)
from corridor.models import (
    Dependency,
    Document,
    EventAdmissionAcceptanceReceipt,
    PolicyRun,
    Project,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
)


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _domain_counts(session, project_id: int) -> dict[str, int]:
    return {
        "dependencies": session.scalar(
            select(func.count()).select_from(Dependency).where(
                Dependency.project_id == project_id
            )
        ),
        "policy_runs": session.scalar(
            select(func.count()).select_from(PolicyRun).where(
                PolicyRun.project_id == project_id
            )
        ),
        "event_acceptance_receipts": session.scalar(
            select(func.count())
            .select_from(EventAdmissionAcceptanceReceipt)
            .where(EventAdmissionAcceptanceReceipt.project_id == project_id)
        ),
    }


def _scheduled_project(factory, now: datetime, **overrides):
    with factory() as setup:
        project = Project(
            slug=f"due-work-{uuid4().hex}",
            name="Due Work",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        declaration = ProcessingHealthDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="processing-health-v1",
            starts_at=now.replace(minute=0, second=0, microsecond=0),
        )
        declaration = replace(declaration, **overrides)
        schedule = configure_due_work(
            setup,
            declaration,
            now=now,
        )
        ids = (project.id, schedule.id)
        setup.commit()
        return ids


def test_processing_health_runs_due_to_receipt_to_status_without_domain_writes(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 5, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"due-work-health-{uuid4().hex}",
            name="Due Work Health",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        setup.add(
            Document(
                project_id=project.id,
                registry_id=f"failed-{uuid4().hex}",
                sha256="f" * 64,
                filename="failed.pdf",
                doc_type="matrix",
                parse_status="failed",
                pages=1,
            )
        )
        setup.flush()
        before = _domain_counts(setup, project.id)
        schedule = configure_processing_health(
            setup,
            ProcessingHealthDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="processing-health-v1",
                starts_at=datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc),
            ),
            now=now,
        )
        project_id = project.id
        setup.commit()

    with factory() as ticking:
        [occurrence] = enqueue_due_work(ticking, now=now)
        ticking.commit()
    assert occurrence.scheduled_job_id == schedule.id

    result = run_due_work_once(
        factory,
        clock=ControlledClock(now),
        owner="runtime:test-worker",
    )
    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_result["health"] == "processing_failures_observed"
    assert result.handler_result["failed_document_count"] == 1

    with factory() as verification:
        status = due_work_status(verification, project_id=project_id)
        assert status["jobs"][0]["configuration_version"] == "processing-health-v1"
        assert status["occurrences"][0]["state"] == "completed"
        assert status["receipts"][0]["execution_outcome"] == "completed"
        assert status["receipts"][0]["handler_result"]["health"] == (
            "processing_failures_observed"
        )
        assert status["receipts"][0]["safe_next_step"] == (
            "inspect_failed_document_processing"
        )
        assert _domain_counts(verification, project_id) == before
        with pytest.raises(ProgrammingError), verification.begin_nested():
            verification.execute(
                update(DueWorkReceipt)
                .where(DueWorkReceipt.id == result.receipt_id)
                .values(safe_next_step="rewritten")
            )


def test_repeated_ticks_use_latest_only_utc_occurrences(runtime_database):
    factory = runtime_database.session_factory
    starts_at = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    project_id, schedule_id = _scheduled_project(factory, starts_at)

    with factory() as before_start:
        assert enqueue_due_work(
            before_start,
            now=datetime(2026, 8, 29, 6, 59, tzinfo=timezone.utc),
        ) == ()
        before_start.commit()
    with factory() as first_tick:
        [first] = enqueue_due_work(first_tick, now=starts_at)
        first_id = first.id
        first_tick.commit()
    with factory() as repeated_tick:
        [repeated] = enqueue_due_work(
            repeated_tick,
            now=datetime(2026, 8, 29, 7, 59, tzinfo=timezone.utc),
        )
        assert repeated.id == first_id
        repeated_tick.commit()
    with factory() as missed_tick:
        [latest] = enqueue_due_work(
            missed_tick,
            now=datetime(2026, 8, 29, 9, 37, tzinfo=timezone.utc),
        )
        assert latest.due_at == datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
        missed_tick.commit()

    with factory() as claiming:
        claim = claim_due_work(
            claiming,
            now=datetime(2026, 8, 29, 9, 37, tzinfo=timezone.utc),
            owner="runtime:restart-worker",
        )
        assert claim is not None
        claimed_occurrence = claiming.get(DueWorkOccurrence, claim.occurrence_id)
        assert claimed_occurrence.due_at.hour == 9
        claiming.commit()

    with factory() as verification:
        occurrences = verification.scalars(
            select(DueWorkOccurrence)
            .where(DueWorkOccurrence.scheduled_job_id == schedule_id)
            .order_by(DueWorkOccurrence.due_at)
        ).all()
        assert [item.due_at.hour for item in occurrences] == [7, 9]
        assert [item.state for item in occurrences] == ["failed", "claimed"]
        assert all(item.scheduled_job_id == schedule_id for item in occurrences)
        [missed_receipt] = verification.scalars(
            select(DueWorkReceipt).where(
                DueWorkReceipt.occurrence_id == first_id
            )
        ).all()
        assert missed_receipt.error_code == "missed_run_latest_only"


def test_abandoned_claim_is_retained_and_stale_worker_cannot_finalize(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    with factory() as first_claiming:
        first = claim_due_work(
            first_claiming,
            now=now,
            owner="runtime:first-worker",
        )
        assert first is not None
        first_claiming.commit()

    recovered_at = datetime(2026, 8, 29, 8, 6, tzinfo=timezone.utc)
    with factory() as recovering:
        recovered = claim_due_work(
            recovering,
            now=recovered_at,
            owner="runtime:recovery-worker",
        )
        assert recovered is not None
        assert recovered.attempt_number == 2
        recovering.commit()

    with factory() as stale_finalizing:
        with pytest.raises(StaleDueWorkClaim):
            complete_due_work(
                stale_finalizing,
                first,
                handler_result={"health": "healthy"},
                now=recovered_at,
            )
    with factory() as recovered_finalizing:
        result = fail_due_work(
            recovered_finalizing,
            recovered,
            error_code="controlled_stop",
            now=recovered_at,
            retryable=False,
        )
        assert result.execution_outcome == "failed"
        recovered_finalizing.commit()

    with factory() as verification:
        status = due_work_status(verification, project_id=project_id)
        assert [item["execution_outcome"] for item in status["receipts"]] == [
            "retry_due",
            "failed",
        ]
        assert status["receipts"][0]["error_code"] == "claim_abandoned"


def test_retry_backoff_and_attempt_exhaustion_are_retained_failures(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    current = now
    for attempt, expected in ((1, "retry_due"), (2, "retry_due"), (3, "failed")):
        with factory() as claiming:
            claim = claim_due_work(
                claiming,
                now=current,
                owner="runtime:failing-worker",
            )
            assert claim is not None
            assert claim.attempt_number == attempt
            claiming.commit()
        with factory() as failing:
            result = fail_due_work(
                failing,
                claim,
                error_code="handler_execution_failed",
                now=current,
            )
            assert result.execution_outcome == expected
            failing.commit()
        current += timedelta(seconds=60 * (2 ** (attempt - 1)))

    with factory() as verification:
        status = due_work_status(verification, project_id=project_id)
        assert status["occurrences"][0]["state"] == "failed"
        assert status["occurrences"][0]["attempt_count"] == 3
        assert [item["attempt_number"] for item in status["receipts"]] == [1, 2, 3]
        assert all(item["execution_outcome"] != "completed" for item in status["receipts"])


def test_competing_workers_coalesce_on_one_claim(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc)
    _scheduled_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    ready = Barrier(2)

    def compete(owner):
        ready.wait(timeout=2)
        with factory() as session:
            claim = claim_due_work(session, now=now, owner=owner)
            session.commit()
            return claim

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(
            pool.map(compete, ("runtime:worker-a", "runtime:worker-b"))
        )
    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1


def test_invalid_gate_configuration_is_refused_without_a_job(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"invalid-due-work-{uuid4().hex}",
            name="Invalid Due Work",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        declaration = replace(
            ProcessingHealthDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="processing-health-v1",
                starts_at=now,
            ),
            timezone_name="America/Chicago",
        )
        with pytest.raises(DueWorkRefusal, match="hourly UTC"):
            configure_processing_health(setup, declaration, now=now)
        assert setup.scalars(
            select(DueWorkSchedule).where(DueWorkSchedule.project_id == project.id)
        ).all() == []


def test_shutdown_stops_before_taking_new_work(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
    _scheduled_project(factory, now)

    assert supervise_due_work(
        factory,
        clock=ControlledClock(now),
        owner="runtime:supervisor",
        stop_requested=lambda: True,
        wait=lambda _seconds: pytest.fail("shutdown must not sleep"),
        poll_seconds=1,
    ) == 0
    with factory() as verification:
        assert verification.scalars(select(DueWorkOccurrence)).all() == []


def test_changed_configuration_disables_prior_schedule_but_retains_context(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"changed-due-work-{uuid4().hex}",
            name="Changed Due Work",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        first = configure_processing_health(
            setup,
            ProcessingHealthDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="processing-health-v1",
                starts_at=now,
            ),
            now=now,
        )
        second = configure_processing_health(
            setup,
            ProcessingHealthDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="processing-health-v2",
                starts_at=now,
            ),
            now=now + timedelta(minutes=1),
        )
        project_id = project.id
        setup.commit()

    assert first.id != second.id
    assert first.configuration_sha256 != second.configuration_sha256
    with factory() as ticking:
        [occurrence] = enqueue_due_work(ticking, now=now + timedelta(minutes=2))
        assert occurrence.scheduled_job_id == second.id
        ticking.commit()
    with factory() as verification:
        status = due_work_status(verification, project_id=project_id)
        assert [item["enabled"] for item in status["jobs"]] == [False, True]


def test_persisted_job_data_cannot_select_an_arbitrary_handler(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project_id, schedule_id = _scheduled_project(factory, now)
        source = setup.get(DueWorkSchedule, schedule_id)
        assert source is not None
        configuration = {**source.configuration_json, "handler": "os.system"}
        digest = hashlib.sha256(
            json.dumps(
                configuration,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        malicious = DueWorkSchedule(
            public_id=f"due-job:{digest[:24]}",
            project_id=project_id,
            handler_key="os.system",
            configuration_version="malicious-handler-v1",
            scope_json={"project_id": project_id},
            configuration_json=configuration,
            configuration_sha256=digest,
            input_identity_sha256="b" * 64,
            starts_at=now,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=1,
            backoff_seconds=0,
            claim_ttl_seconds=60,
            deadline_seconds=30,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
            enabled_at=now,
        )
        setup.add(malicious)
        setup.flush([malicious])

        with pytest.raises(DueWorkRefusal, match="not server-owned"):
            enqueue_due_work(setup, now=now)


def test_persisted_unsupported_schedule_rules_are_refused(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 15, 30, tzinfo=timezone.utc)
    project_id, schedule_id = _scheduled_project(
        factory,
        now.replace(minute=0),
    )
    with factory() as setup:
        source = setup.get(DueWorkSchedule, schedule_id)
        assert source is not None
        configuration = {
            **source.configuration_json,
            "configuration_version": "unsupported-cadence-v1",
            "cadence": "daily",
        }
        digest = hashlib.sha256(
            json.dumps(
                configuration,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        unsupported = DueWorkSchedule(
            public_id=f"due-job:{digest[:24]}",
            project_id=project_id,
            handler_key=HANDLER_PROCESSING_HEALTH,
            configuration_version="unsupported-cadence-v1",
            scope_json={"project_id": project_id},
            configuration_json=configuration,
            configuration_sha256=digest,
            input_identity_sha256=source.input_identity_sha256,
            starts_at=source.starts_at,
            cadence="daily",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=1,
            backoff_seconds=0,
            claim_ttl_seconds=60,
            deadline_seconds=30,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
            enabled_at=now,
        )
        setup.add(unsupported)
        setup.flush([unsupported])

        with pytest.raises(DueWorkRefusal, match="hourly UTC"):
            enqueue_due_work(setup, now=now)


def test_deadline_exhaustion_is_a_retained_processing_failure(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now, max_attempts=1)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    class AdvancingClock:
        def __init__(self):
            self.values = iter(
                (
                    now,
                    now + timedelta(seconds=121),
                    now + timedelta(seconds=121),
                )
            )

        def now(self):
            return next(self.values)

    registry = {
        HANDLER_PROCESSING_HEALTH: HandlerContract(
            key=HANDLER_PROCESSING_HEALTH,
            scope_kind="one_project_stored_processing_facts",
            idempotency_contract="read_only_reconcilable",
            max_result_bytes=4096,
            model_token_budget=0,
            notification_budget=0,
            run=lambda _session, _schedule, _now: {},
        )
    }
    result = run_due_work_once(
        factory,
        clock=AdvancingClock(),
        owner="runtime:deadline-worker",
        registry=registry,
    )

    assert result is not None
    assert result.execution_outcome == "failed"
    assert result.error_code == "deadline_exceeded"
    with factory() as verification:
        status = due_work_status(verification, project_id=project_id)
        assert status["occurrences"][0]["state"] == "failed"


def test_receipt_finalization_rollback_leaves_claim_recoverable(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 17, 0, tzinfo=timezone.utc)
    project_id, _ = _scheduled_project(factory, now)
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    with factory() as claiming:
        claim = claim_due_work(
            claiming,
            now=now,
            owner="runtime:rollback-worker",
        )
        assert claim is not None
        claiming.commit()

    with pytest.raises(RuntimeError):
        with factory() as interrupted:
            with interrupted.begin():
                complete_due_work(
                    interrupted,
                    claim,
                    handler_result={
                        "schema_version": "processing-health-result-v1",
                        "project_id": project_id,
                        "configuration_version": "processing-health-v1",
                        "observed_at": now.isoformat(),
                        "health": "healthy",
                        "document_count": 0,
                        "failed_document_count": 0,
                        "failed_extraction_count": 0,
                    },
                    now=now,
                )
                raise RuntimeError("crash before finalization commit")

    with factory() as verification:
        occurrence = verification.get(DueWorkOccurrence, claim.occurrence_id)
        assert occurrence.state == "claimed"
        assert occurrence.claim_token == claim.claim_token
        assert verification.scalars(
            select(DueWorkReceipt).where(
                DueWorkReceipt.occurrence_id == claim.occurrence_id
            )
        ).all() == []
