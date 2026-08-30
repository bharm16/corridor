"""Make/CLI surface for the shared supervised Due Work runtime.

Calling domain helpers from tests alone was rejected because it leaves the
production supervisor owner implicit. These tests exercise the same configured,
bounded commands operators run outside the web process.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from corridor.due_work_cli import main
from corridor.models import Project


class ControlledClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


def _configure_argv(project_slug: str) -> list[str]:
    return [
        "configure-health",
        project_slug,
        "--configuration-version=processing-health-v1",
        "--starts-at=2026-08-29T07:00:00+00:00",
        "--cadence=hourly",
        "--timezone=UTC",
        "--missed-run-policy=latest_only",
        "--retention-days=3650",
        "--max-attempts=3",
        "--backoff-seconds=60",
        "--claim-ttl-seconds=300",
        "--deadline-seconds=120",
        "--concurrency-limit=1",
        "--model-token-budget=0",
        "--notification-budget=0",
    ]


def _payload(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_configure_run_once_and_status_use_the_public_runtime(
    runtime_database, capsys
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 5, tzinfo=timezone.utc)
    clock = ControlledClock(now)
    with factory() as setup:
        project = Project(
            slug=f"due-work-cli-{uuid4().hex}",
            name="Due Work CLI",
            is_synthetic=True,
        )
        setup.add(project)
        setup.commit()

    assert main(
        _configure_argv(project.slug),
        session_factory=factory,
        clock=clock,
    ) == 0
    configured = _payload(capsys)
    assert configured["enabled"] is True

    assert main(
        ["run-once", "--owner=runtime:cli-worker"],
        session_factory=factory,
        clock=clock,
    ) == 0
    executed = _payload(capsys)
    assert executed["result"]["execution_outcome"] == "completed"
    assert executed["result"]["handler_result"]["health"] == "healthy"
    assert executed["result"]["occurrence_id"].startswith("due-occurrence:")
    assert executed["result"]["receipt_id"].startswith("due-receipt:")
    assert executed["result"]["job_id"].startswith("due-job:")
    assert executed["result"]["attempt_id"].startswith("due-attempt:")

    assert main(
        ["status", f"--project-slug={project.slug}"],
        session_factory=factory,
        clock=clock,
    ) == 0
    status = _payload(capsys)
    assert status["jobs"][0]["handler"] == "processing_health"
    assert status["jobs"][0]["input_identity"] == {
        "kind": "stored_processing_facts-v1",
        "project_id": project.id,
    }
    assert status["occurrences"][0]["state"] == "completed"
    assert status["occurrences"][0]["job_id"] == status["jobs"][0]["job_id"]
    assert status["receipts"][0]["safe_next_step"] == "none"
    assert status["receipts"][0]["occurrence_id"] == (
        status["occurrences"][0]["occurrence_id"]
    )


def test_configure_discovery_retains_a_gate_7_connected_location(
    runtime_database, capsys
):
    factory = runtime_database.session_factory
    clock = ControlledClock(datetime(2026, 8, 29, 7, 5, tzinfo=timezone.utc))
    with factory() as setup:
        project = Project(
            slug=f"loc-cli-{uuid4().hex}", name="Location CLI", is_synthetic=True
        )
        setup.add(project)
        setup.commit()

    argv = [
        "configure-discovery",
        project.slug,
        "--configuration-version=location-discovery-v1",
        "--location-id=txdot-loc",
        "--adapter-identity=http-index-v1",
        "--source-manifest-id=txdot-manifest",
        "--index-url=https://docs.example.gov/index.json",
        "--authorized-host=docs.example.gov",
        "--starts-at=2026-08-29T07:00:00+00:00",
        "--cadence=hourly",
        "--timezone=UTC",
        "--missed-run-policy=latest_only",
        "--retention-days=3650",
        "--max-attempts=3",
        "--backoff-seconds=120",
        "--claim-ttl-seconds=600",
        "--deadline-seconds=300",
        "--concurrency-limit=1",
        "--model-token-budget=0",
        "--notification-budget=0",
    ]
    assert main(argv, session_factory=factory, clock=clock) == 0
    configured = _payload(capsys)
    assert configured["enabled"] is True
    assert configured["sealed"] is False
    assert configured["location_id"] == "txdot-loc"

    assert main(
        ["status", f"--project-slug={project.slug}"],
        session_factory=factory,
        clock=clock,
    ) == 0
    status = _payload(capsys)
    assert status["jobs"][0]["handler"] == "location_discovery"
    assert status["jobs"][0]["input_identity"]["kind"] == "connected_location-v1"


def _configure_reproof_argv(project_slug: str) -> list[str]:
    return [
        "configure-reproof",
        project_slug,
        "--configuration-version=event-admission-reproof-v1",
        "--policy-version=event-admission-v3-unknown-scope",
        "--reason-version=event-admission-abstentions-v5",
        "--selection-rule=current-active-run-pending-event-candidates-v1",
        "--starts-at=2026-08-30T08:00:00+00:00",
        "--cadence=hourly",
        "--timezone=UTC",
        "--missed-run-policy=latest_only",
        "--retention-days=3650",
        "--max-attempts=2",
        "--backoff-seconds=300",
        "--claim-ttl-seconds=1800",
        "--deadline-seconds=1800",
        "--concurrency-limit=1",
        "--model-token-budget=0",
        "--notification-budget=0",
        "--clone-budget=3",
    ]


def test_configure_reproof_declares_a_bounded_recovery_schedule(
    runtime_database, capsys
):
    factory = runtime_database.session_factory
    clock = ControlledClock(datetime(2026, 8, 30, 8, 5, tzinfo=timezone.utc))
    with factory() as setup:
        project = Project(
            slug=f"reproof-cli-{uuid4().hex}",
            name="Re-proof CLI",
            is_synthetic=True,
            project_side_parties=["LJA"],
        )
        setup.add(project)
        setup.commit()

    assert main(
        _configure_reproof_argv(project.slug), session_factory=factory, clock=clock
    ) == 0
    configured = _payload(capsys)
    assert configured["enabled"] is True
    assert configured["job_id"].startswith("due-job:")

    assert main(
        ["status", f"--project-slug={project.slug}"],
        session_factory=factory,
        clock=clock,
    ) == 0
    status = _payload(capsys)
    assert status["jobs"][0]["handler"] == "event_admission_reproof"
    assert status["jobs"][0]["input_identity"] == {
        "kind": "one_project_event_admission_class-v1",
        "project_id": project.id,
        "policy_version": "event-admission-v3-unknown-scope",
        "reason_version": "event-admission-abstentions-v5",
        "selection_rule": "current-active-run-pending-event-candidates-v1",
    }
    assert status["jobs"][0]["model_token_budget"] == 0


def test_configure_reproof_refuses_a_silent_nonzero_model_budget(
    runtime_database, capsys
):
    factory = runtime_database.session_factory
    clock = ControlledClock(datetime(2026, 8, 30, 8, 5, tzinfo=timezone.utc))
    with factory() as setup:
        project = Project(
            slug=f"reproof-cli-bad-{uuid4().hex}",
            name="Re-proof CLI Bad",
            is_synthetic=True,
            project_side_parties=["LJA"],
        )
        setup.add(project)
        setup.commit()

    argv = [
        arg if arg != "--model-token-budget=0" else "--model-token-budget=1000"
        for arg in _configure_reproof_argv(project.slug)
    ]
    assert main(argv, session_factory=factory, clock=clock) == 1
    assert "resource declaration is invalid" in capsys.readouterr().err


def test_configure_refuses_an_incomplete_gate_7_declaration(capsys):
    assert main(["configure-health", "project"]) == 2
    assert "required" in capsys.readouterr().err


def _configure_notifications_argv(project_slug: str) -> list[str]:
    return [
        "configure-notifications",
        project_slug,
        "--configuration-version=assignment-notification-v1",
        "--channel=email",
        "--starts-at=2026-08-29T07:00:00+00:00",
        "--cadence=hourly",
        "--timezone=UTC",
        "--missed-run-policy=latest_only",
        "--retention-days=3650",
        "--max-attempts=3",
        "--backoff-seconds=60",
        "--claim-ttl-seconds=300",
        "--deadline-seconds=120",
        "--concurrency-limit=1",
        "--model-token-budget=0",
        "--notification-budget=500",
    ]


def test_configure_notifications_records_the_gate_7_delivery_schedule(
    runtime_database, capsys
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 5, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"notify-cli-{uuid4().hex}", name="Notify CLI", is_synthetic=True
        )
        setup.add(project)
        setup.commit()

    assert (
        main(
            _configure_notifications_argv(project.slug),
            session_factory=factory,
            clock=ControlledClock(now),
        )
        == 0
    )
    configured = _payload(capsys)
    assert configured["enabled"] is True

    assert (
        main(
            ["status", f"--project-slug={project.slug}"],
            session_factory=factory,
            clock=ControlledClock(now),
        )
        == 0
    )
    status = _payload(capsys)
    assert status["jobs"][0]["handler"] == "assignment_notification"
    assert status["jobs"][0]["notification_budget"] == 500


def test_supervisor_honors_shutdown_before_taking_work(runtime_database, capsys):
    assert main(
        ["supervise", "--owner=runtime:cli-worker", "--poll-seconds=1"],
        session_factory=runtime_database.session_factory,
        clock=ControlledClock(
            datetime(2026, 8, 29, 7, 5, tzinfo=timezone.utc)
        ),
        stop_requested=lambda: True,
        wait=lambda _seconds: pytest.fail("stopped supervisor must not wait"),
    ) == 0
    assert _payload(capsys) == {"command": "supervise", "completed_cycles": 0}
