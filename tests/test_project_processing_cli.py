"""The operator gate-7 surface for project processing.

`configure-processing` is the only way to enable a project sweep; run-once and
recover already claim any registered handler, so the operator recovery entry
point is `make due-work ARGS="recover --owner=runtime:<id>"`. These tests cover
the configuration command and its refusal of an incomplete declaration; the
effectful run itself is exercised against a controlled route in
`test_project_processing_runtime.py` rather than the production model here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from corridor.due_work_cli import main
from corridor.models import Project
from clock_support import ControlledClock


def _configure_argv(project_slug: str, **overrides) -> list[str]:
    values = {
        "configuration-version": "project-processing-v1",
        "extractor-identity": "deployed-matrix-v1",
        "starts-at": "2026-08-29T07:00:00+00:00",
        "cadence": "hourly",
        "timezone": "UTC",
        "missed-run-policy": "latest_only",
        "retention-days": "3650",
        "max-attempts": "3",
        "backoff-seconds": "120",
        "claim-ttl-seconds": "1800",
        "deadline-seconds": "1800",
        "concurrency-limit": "1",
        "model-token-budget": "2000000",
        "notification-budget": "0",
    }
    values.update(overrides)
    return ["configure-processing", project_slug] + [
        f"--{name}={value}" for name, value in values.items()
    ]


def _payload(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_configure_processing_enables_and_status_reports_the_sweep(
    runtime_database, capsys
):
    factory = runtime_database.session_factory
    clock = ControlledClock(datetime(2026, 8, 29, 7, 5, tzinfo=timezone.utc))
    with factory() as setup:
        project = Project(
            slug=f"proc-cli-{uuid4().hex}",
            name="Processing CLI",
            is_synthetic=True,
        )
        setup.add(project)
        setup.commit()

    assert (
        main(_configure_argv(project.slug), session_factory=factory, clock=clock) == 0
    )
    configured = _payload(capsys)
    assert configured["enabled"] is True
    assert configured["job_id"].startswith("due-job:")

    assert (
        main(
            ["status", f"--project-slug={project.slug}"],
            session_factory=factory,
            clock=clock,
        )
        == 0
    )
    status = _payload(capsys)
    assert status["jobs"][0]["handler"] == "project_processing"
    assert status["jobs"][0]["input_identity"] == {
        "kind": "registered_project_extraction-v1",
        "project_id": project.id,
        "extractor_identity": "deployed-matrix-v1",
    }
    assert status["jobs"][0]["model_token_budget"] == 2000000
    assert status["jobs"][0]["enabled"] is True


def test_configure_processing_refuses_a_silent_zero_budget(runtime_database, capsys):
    factory = runtime_database.session_factory
    clock = ControlledClock(datetime(2026, 8, 29, 7, 5, tzinfo=timezone.utc))
    with factory() as setup:
        project = Project(
            slug=f"proc-cli-zero-{uuid4().hex}",
            name="Processing CLI Zero",
            is_synthetic=True,
        )
        setup.add(project)
        setup.commit()

    assert (
        main(
            _configure_argv(project.slug, **{"model-token-budget": "0"}),
            session_factory=factory,
            clock=clock,
        )
        == 1
    )
    assert "resource declaration is invalid" in capsys.readouterr().err


def test_configure_processing_refuses_an_incomplete_declaration(capsys):
    assert main(["configure-processing", "project"]) == 2
    assert "required" in capsys.readouterr().err
