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


def test_configure_refuses_an_incomplete_gate_7_declaration(capsys):
    assert main(["configure-health", "project"]) == 2
    assert "required" in capsys.readouterr().err


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
