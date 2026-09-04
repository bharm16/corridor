"""Operational telemetry through its real seams (#491A).

The runtime's operational questions — is the database reachable, is the store
reachable, is the worker beating, how far behind is the queue, when did
anything last succeed — are answered here through the same public interfaces
an operator reaches: the structured log stream, the health endpoint, and the
signal reader over the durable Due Work tables. Nothing is asserted against a
private formatter or a hand-built log string, because a log shape nobody emits
proves nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import logging
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session as SessionType

from corridor import telemetry
from corridor.analytics import HighCardinalityMetricError
from corridor.db import Session, engine
from corridor.due_work import (
    HANDLER_CONNECTOR_POLLING,
    HANDLER_DELTA_GENERATION,
    HANDLER_PROCESSING_HEALTH,
    HANDLER_REGISTRY,
    HANDLER_REPORT_PREPARATION,
    HANDLER_RETENTION_SWEEP,
    ConnectorPollingDeclaration,
    DeltaGenerationDeclaration,
    ProcessingHealthDeclaration,
    ReportPreparationDeclaration,
    RetentionSweepDeclaration,
    claim_due_work,
    complete_due_work,
    configure_connector_polling,
    configure_delta_generation,
    configure_processing_health,
    configure_report_preparation,
    configure_retention_sweep,
    enqueue_due_work,
    fail_due_work,
    run_due_work_once,
)
from corridor.models import (
    Document,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    Project,
    ScheduledReportPublication,
)
from corridor.object_storage import LocalFilesystemStore
from corridor.operational_health import (
    ComponentHealth,
    OperationalSignal,
    check_database,
    check_object_storage,
    check_worker_heartbeat,
    due_work_signals,
    runtime_report,
)
from corridor.web.app import (
    app,
    get_content_store,
    get_machine_session,
    get_session,
)


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


@pytest.fixture
def session():
    """A rolled-back transaction that owns the deployment-wide Due Work state.

    A heartbeat, a backlog, and a last-success age are deployment-wide
    readings, so a stale row left in the development database by an earlier
    non-transactional run would decide the answer instead of the test. The
    rows are removed inside the transaction and come back when it rolls back.
    """

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    session.execute(delete(ScheduledReportPublication))
    session.execute(delete(DueWorkReceipt))
    session.execute(delete(DueWorkOccurrence))
    session.execute(delete(DueWorkSchedule))
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def logs():
    """Capture the process log stream, then restore the deployed default."""

    stream = io.StringIO()
    telemetry.configure_logging(
        role=telemetry.ROLE_WORKER, environment="test", stream=stream
    )
    yield stream
    telemetry.configure_logging(role=telemetry.ROLE_WEB)


def _records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _project(session, slug_prefix: str) -> Project:
    project = Project(
        slug=f"{slug_prefix}-{uuid4().hex[:12]}",
        name="Operational Telemetry",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])
    return project


def _hourly_schedule(session, project: Project, *, now: datetime) -> DueWorkSchedule:
    return configure_processing_health(
        session,
        ProcessingHealthDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="processing-health-v1",
            starts_at=now.replace(minute=0, second=0, microsecond=0),
        ),
        now=now,
    )


# Structured application and worker logs.


def test_a_log_line_is_one_json_object_naming_role_environment_and_event(logs):
    log = logging.getLogger("corridor.test")

    telemetry.log_event(log, "storage_probe_failed", backend="filesystem")

    [record] = _records(logs)
    assert record["event"] == "storage_probe_failed"
    assert record["role"] == telemetry.ROLE_WORKER
    assert record["environment"] == "test"
    assert record["level"] == "INFO"
    assert record["logger"] == "corridor.test"
    assert record["backend"] == "filesystem"
    assert datetime.fromisoformat(record["timestamp"]).tzinfo is timezone.utc


def test_a_log_line_keeps_its_reserved_shape_when_a_field_collides(logs):
    log = logging.getLogger("corridor.test")

    with telemetry.correlation_scope(event="attacker", environment="attacker"):
        telemetry.log_event(log, "collision", role="attacker", timestamp="attacker")

    [record] = _records(logs)
    assert record["role"] == telemetry.ROLE_WORKER
    assert record["event"] == "collision"
    assert record["environment"] == "test"
    assert datetime.fromisoformat(record["timestamp"]).year >= 2024


# Request and job correlation identifiers.


def test_every_line_inside_one_correlation_scope_carries_the_same_identifier(logs):
    log = logging.getLogger("corridor.test")

    with telemetry.correlation_scope(job_id="due-occurrence:abc", project_id=7):
        telemetry.log_event(log, "job_started")
        telemetry.log_event(log, "job_finished")
    telemetry.log_event(log, "idle")

    started, finished, idle = _records(logs)
    assert started["job_id"] == finished["job_id"] == "due-occurrence:abc"
    assert started["project_id"] == finished["project_id"] == 7
    assert "job_id" not in idle


def test_a_web_request_logs_one_line_with_a_request_id_and_the_route_template(
    session, logs, tmp_path
):
    client = _health_client(session, LocalFilesystemStore(tmp_path))

    response = client.get("/health")

    [record] = [item for item in _records(logs) if item["event"] == "http_request"]
    assert record["request_id"] == response.headers["x-request-id"]
    assert record["route"] == "/health"
    assert record["method"] == "GET"
    assert record["status"] == response.status_code
    assert isinstance(record["duration_ms"], (int, float))


def test_a_due_work_attempt_logs_carry_the_occurrence_identifier(
    runtime_database, logs
):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 7, 5, tzinfo=timezone.utc)
    with factory() as setup:
        project = _project(setup, "telemetry-job")
        setup.add(
            Document(
                project_id=project.id,
                registry_id=f"telemetry-{uuid4().hex}",
                sha256="a" * 64,
                filename="source.pdf",
                doc_type="matrix",
                parse_status="parsed",
                pages=1,
            )
        )
        _hourly_schedule(setup, project, now=now)
        project_id = project.id
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:telemetry-worker"
    )

    assert result is not None
    finished = [item for item in _records(logs) if item["event"] == "due_work_attempt"]
    [record] = finished
    assert record["job_id"] == result.occurrence_public_id
    assert record["attempt_id"] == result.attempt_id
    assert record["handler"] == HANDLER_PROCESSING_HEALTH
    assert record["project_id"] == project_id
    assert record["execution_outcome"] == "completed"
    assert record["role"] == telemetry.ROLE_WORKER


# Database and object-storage health.


def test_database_health_reports_a_reachable_connection(session):
    assert check_database(session) == ComponentHealth(
        component="database", healthy=True, detail="reachable"
    )


def test_database_health_reports_unreachable_without_leaking_the_credential():
    unreachable = create_engine(
        "postgresql+psycopg://corridor:corridor@127.0.0.1:1/corridor"
    )
    with SessionType(bind=unreachable) as session:
        health = check_database(session)

    assert health == ComponentHealth(
        component="database", healthy=False, detail="unreachable"
    )


def test_object_storage_health_follows_the_store_probe(tmp_path):
    reachable = LocalFilesystemStore(tmp_path)
    missing = LocalFilesystemStore(tmp_path / "not-provisioned")

    assert check_object_storage(reachable) == ComponentHealth(
        component="object_storage", healthy=True, detail="reachable"
    )
    assert check_object_storage(missing) == ComponentHealth(
        component="object_storage", healthy=False, detail="unreachable"
    )


# Worker heartbeat.


def test_worker_heartbeat_is_beating_before_the_first_slot_and_stale_after_it(session):
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    project = _project(session, "telemetry-heartbeat")
    _hourly_schedule(session, project, now=now)

    assert check_worker_heartbeat(session, now=now).healthy
    stale = check_worker_heartbeat(session, now=now + timedelta(hours=4))
    assert stale == ComponentHealth(
        component="worker_heartbeat", healthy=False, detail="stale"
    )


def test_worker_heartbeat_beats_again_after_a_retained_attempt(session):
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    project = _project(session, "telemetry-beat")
    _hourly_schedule(session, project, now=now)
    enqueue_due_work(session, now=now)
    later = now + timedelta(hours=4)
    claim = claim_due_work(session, now=later, owner="runtime:telemetry")
    fail_due_work(session, claim, error_code="handler_execution_failed", now=later)

    assert check_worker_heartbeat(session, now=later) == ComponentHealth(
        component="worker_heartbeat", healthy=True, detail="beating"
    )


def test_worker_heartbeat_is_not_stale_when_no_schedule_is_enabled(session):
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)

    assert check_worker_heartbeat(session, now=now) == ComponentHealth(
        component="worker_heartbeat", healthy=True, detail="no_enabled_schedule"
    )


# Retries, queue lag, failures, and last-success age.


def test_signals_report_backlog_lag_retries_failures_and_last_success_age(session):
    ten = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    project = _project(session, "telemetry-signals")
    schedule = _hourly_schedule(session, project, now=ten)
    contract = HANDLER_REGISTRY[HANDLER_PROCESSING_HEALTH]

    enqueue_due_work(session, now=ten)
    first = claim_due_work(session, now=ten + timedelta(minutes=5), owner="runtime:signals")
    fail_due_work(
        session,
        first,
        error_code="handler_execution_failed",
        now=ten + timedelta(minutes=6),
    )
    second = claim_due_work(session, now=ten + timedelta(minutes=20), owner="runtime:signals")
    complete_due_work(
        session,
        second,
        handler_result=contract.run(session, schedule, ten + timedelta(minutes=21)),
        now=ten + timedelta(minutes=21),
    )
    eleven = ten + timedelta(hours=1)
    enqueue_due_work(session, now=eleven)
    third = claim_due_work(session, now=eleven + timedelta(minutes=1), owner="runtime:signals")
    fail_due_work(
        session,
        third,
        error_code="deadline_exceeded",
        now=eleven + timedelta(minutes=2),
        retryable=False,
    )
    twelve = ten + timedelta(hours=2)
    enqueue_due_work(session, now=twelve)

    now = twelve + timedelta(minutes=30)
    signals = {
        (item.name, tuple(sorted(item.labels.items()))): item.value
        for item in due_work_signals(session, now=now)
    }

    queue = (("queue", HANDLER_PROCESSING_HEALTH),)
    assert signals[("corridor_processing_backlog", queue)] == 1
    assert signals[("corridor_due_work_queue_lag_seconds", queue)] == 1800
    assert signals[("corridor_due_work_retries_total", queue)] == 1
    assert (
        signals[
            (
                "corridor_due_work_failures_total",
                (("queue", HANDLER_PROCESSING_HEALTH), ("reason", "deadline_exceeded")),
            )
        ]
        == 1
    )
    assert signals[("corridor_due_work_last_success_age_seconds", queue)] == (
        now - (ten + timedelta(minutes=21))
    ).total_seconds()


def _pilot_schedules(session, project, *, now: datetime) -> None:
    """The four recurring declarations a live pilot deployment stands up (#488)."""

    configure_connector_polling(
        session,
        ConnectorPollingDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="connector-polling-v1",
            customer="acme-utilities",
            channel="shared-files",
            connector_identity="txdot-rid-box-v1",
            source_url="https://example.test/shared/index",
            starts_at=now,
        ),
        now=now,
    )
    configure_delta_generation(
        session,
        DeltaGenerationDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="delta-generation-v1",
            comparison_rule_version="structured-cell-value-comparison-v1",
            starts_at=now,
        ),
        now=now,
    )
    configure_report_preparation(
        session,
        ReportPreparationDeclaration.released_weekly(
            project_id=project.id,
            configuration_version="report-preparation-v1",
            starts_at=now,
        ),
        now=now,
    )
    configure_retention_sweep(
        session,
        RetentionSweepDeclaration.released_weekly(
            project_id=project.id,
            configuration_version="retention-sweep-v1",
            authorized_by="local:retention-operator",
            starts_at=now,
        ),
        now=now,
    )


def test_the_pilot_handlers_report_through_the_same_queue_signals(session):
    """#488's handlers add queues to #491A's readings, not a second set of them."""

    now = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
    project = _project(session, "telemetry-pilot")
    _pilot_schedules(session, project, now=now)

    enqueue_due_work(session, now=now)
    later = now + timedelta(minutes=30)
    signals = {
        (item.name, tuple(sorted(item.labels.items()))): item.value
        for item in due_work_signals(session, now=later)
    }

    for handler in (
        HANDLER_CONNECTOR_POLLING,
        HANDLER_DELTA_GENERATION,
        HANDLER_REPORT_PREPARATION,
        HANDLER_RETENTION_SWEEP,
    ):
        queue = (("queue", handler),)
        assert signals[("corridor_processing_backlog", queue)] == 1
        assert signals[("corridor_due_work_queue_lag_seconds", queue)] == 1800
    # The hourly declarations are the tightest cadence, so they, not the weekly
    # ones, set the window the worker's freshness is judged against.
    assert check_worker_heartbeat(session, now=later).detail == "beating"


def test_a_pilot_handler_attempt_logs_the_same_structured_line(
    runtime_database, logs
):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = _project(setup, "telemetry-sweep")
        configure_retention_sweep(
            setup,
            RetentionSweepDeclaration.released_weekly(
                project_id=project.id,
                configuration_version="retention-sweep-v1",
                authorized_by="local:retention-operator",
                starts_at=now,
            ),
            now=now,
        )
        project_id = project.id
        setup.commit()
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:telemetry-sweep"
    )

    assert result is not None
    [record] = [item for item in _records(logs) if item["event"] == "due_work_attempt"]
    assert record["handler"] == HANDLER_RETENTION_SWEEP
    assert record["project_id"] == project_id
    assert record["job_id"] == result.occurrence_public_id
    assert record["attempt_id"] == result.attempt_id
    assert record["execution_outcome"] == "completed"
    assert record["role"] == telemetry.ROLE_WORKER


# Low-cardinality labels only.


def test_a_signal_label_refuses_a_customer_or_project_identifier():
    with pytest.raises(HighCardinalityMetricError):
        OperationalSignal(
            name="corridor_processing_backlog", value=1, labels={"project_id": "7"}
        )


def test_project_identity_stays_in_the_log_line_and_out_of_every_signal_label(
    session, logs
):
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    project = _project(session, "telemetry-labels")
    _hourly_schedule(session, project, now=now)
    enqueue_due_work(session, now=now)
    with telemetry.correlation_scope(project_id=project.id):
        telemetry.log_event(logging.getLogger("corridor.test"), "job_started")

    labels = {
        key for item in due_work_signals(session, now=now) for key in item.labels
    }

    assert labels <= {"queue", "reason"}
    [record] = _records(logs)
    assert record["project_id"] == project.id


# The health endpoint.


def _health_client(session, store) -> TestClient:
    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    # `/health` is a platform probe, not human web traffic, so #680 moved it
    # onto the operations capability; the substitute stands in for both.
    app.dependency_overrides[get_machine_session] = override_session
    app.dependency_overrides[get_content_store] = lambda: store
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_the_health_endpoint_reports_every_component_and_the_due_work_signals(
    session, tmp_path
):
    now = datetime.now(timezone.utc)
    project = _project(session, "telemetry-health")
    _hourly_schedule(session, project, now=now)
    client = _health_client(session, LocalFilesystemStore(tmp_path))

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["role"] == telemetry.ROLE_WEB
    assert [check["component"] for check in body["checks"]] == [
        "application",
        "database",
        "object_storage",
        "worker_heartbeat",
    ]
    assert all(check["healthy"] for check in body["checks"])
    assert {signal["name"] for signal in body["signals"]} >= {
        "corridor_worker_heartbeat_age_seconds"
    }


def test_the_health_endpoint_answers_degraded_when_the_store_is_unreachable(
    session, tmp_path
):
    client = _health_client(session, LocalFilesystemStore(tmp_path / "missing"))

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    unhealthy = [check for check in body["checks"] if not check["healthy"]]
    assert unhealthy == [
        {"component": "object_storage", "healthy": False, "detail": "unreachable"}
    ]


def test_the_health_endpoint_names_no_project_and_no_credential(session, tmp_path):
    now = datetime.now(timezone.utc)
    project = _project(session, "telemetry-quiet")
    _hourly_schedule(session, project, now=now)
    client = _health_client(session, LocalFilesystemStore(tmp_path))

    body = client.get("/health").text

    assert project.slug not in body
    assert "corridor:corridor" not in body
