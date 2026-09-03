"""The Class B retention sweep through the shared Due Work runtime (#488, ADR-0080).

ADR-0080 keeps automatic expiry for Class B intermediaries, with the dry-run
manifest and the reachability check. These tests prove the schedule performs
exactly the boundary ``retention`` already owns — nothing wider — that it stays
inside its own project, that a hold or a still-reachable citation deletes
nothing, and that a refusal comes back as an attention reading in the ordinary
receipt family rather than as a crashed attempt.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.due_work import (
    HANDLER_RETENTION_SWEEP,
    DueWorkRefusal,
    RetentionSweepDeclaration,
    configure_retention_sweep,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.models import (
    CoordinationSummaryConfiguration,
    CoordinationSummaryRequest,
    Project,
    RetentionManifest,
)
from corridor.principals import HumanPrincipal
from corridor.retention import open_reference, place_hold


OPERATOR = HumanPrincipal("local:retention-operator")
NOW = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _project_with_due_content(session, *, due=True, slug_prefix="retention-sweep"):
    project = Project(
        slug=f"{slug_prefix}-{uuid4().hex[:8]}",
        name="Retention Sweep",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])
    configuration = CoordinationSummaryConfiguration(
        project_id=project.id,
        source_scope="all_sources",
        model="test-model",
        prompt_version="coordination_summary_v1",
        max_input_tokens=100,
        max_output_tokens=100,
        timeout_seconds=10,
        max_requests=1,
        retry_policy="none",
        retention_policy="class_b_30_days",
        observation_context="internal_working_view",
        created_by=OPERATOR.subject,
    )
    session.add(configuration)
    session.flush([configuration])
    request = CoordinationSummaryRequest(
        public_id=f"receipt-{uuid4().hex[:12]}",
        project_id=project.id,
        configuration_id=configuration.id,
        requested_by=OPERATOR.subject,
        reading_sha256="a" * 64,
        project_reading_json={"project_id": project.id, "facts": ["copied"]},
        evaluated_on=NOW.date(),
        ruleset_version="v1",
        statement_publication_fingerprint="b" * 64,
        provenance_mode="all-supported-sources",
        status="completed",
        summary_markdown="Working draft",
        completed_at=NOW - timedelta(days=31 if due else 1),
    )
    session.add(request)
    session.flush([request])
    return project, request


def _seed(factory, *, due=True):
    with factory() as setup:
        project, request = _project_with_due_content(setup, due=due)
        configure_retention_sweep(
            setup,
            RetentionSweepDeclaration.released_weekly(
                project_id=project.id,
                configuration_version="retention-sweep-v1",
                authorized_by=OPERATOR.subject,
                starts_at=NOW,
            ),
            now=NOW,
        )
        ids = (project.id, request.id)
        setup.commit()
    return ids


def _run(factory, owner="runtime:retention-worker"):
    with factory() as ticking:
        enqueue_due_work(ticking, now=NOW)
        ticking.commit()
    return run_due_work_once(factory, clock=ControlledClock(NOW), owner=owner)


def _request(session, request_id):
    return session.get(CoordinationSummaryRequest, request_id)


def test_the_sweep_expires_due_class_b_content_and_leaves_its_digest(
    runtime_database,
):
    factory = runtime_database.session_factory
    project_id, request_id = _seed(factory)

    result = _run(factory)

    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_key == HANDLER_RETENTION_SWEEP
    body = result.handler_result
    assert body["schema_version"] == "retention-sweep-result-v1"
    assert body["health"] == "healthy"
    assert body["planned"] == 1
    assert body["deleted"] == 1
    assert body["authorized_by"] == OPERATOR.subject
    assert body["manifest_public_id"]
    assert result.safe_next_step == "none"

    with factory() as verify:
        request = _request(verify, request_id)
        assert request.project_reading_json is None
        assert request.summary_markdown is None
        assert len(request.retention_content_sha256) == 64
        assert request.retention_deleted_at is not None
        status = due_work_status(verify, project_id=project_id)
        assert status["receipts"][0]["handler"] == HANDLER_RETENTION_SWEEP


def test_an_idle_sweep_retains_no_empty_manifest(runtime_database):
    factory = runtime_database.session_factory
    _seed(factory, due=False)

    result = _run(factory)

    assert result.execution_outcome == "completed"
    assert result.handler_result["planned"] == 0
    assert result.handler_result["deleted"] == 0
    assert result.handler_result["manifest_public_id"] == ""
    with factory() as verify:
        assert (
            verify.scalar(select(func.count()).select_from(RetentionManifest)) == 0
        )


def test_a_second_sweep_after_expiry_finds_nothing_left_to_do(runtime_database):
    factory = runtime_database.session_factory
    _seed(factory)

    first = _run(factory)
    assert first.handler_result["deleted"] == 1
    second = _run(factory)

    assert second is None or second.handler_result["planned"] == 0


def test_a_hold_suspends_the_sweep_and_deletes_nothing(runtime_database):
    factory = runtime_database.session_factory
    project_id, request_id = _seed(factory)
    with factory() as holding:
        place_hold(
            holding,
            project_id=project_id,
            reason="open-records request",
            principal=OPERATOR,
        )
        holding.commit()

    result = _run(factory)

    assert result.execution_outcome == "completed"
    assert result.handler_result["planned"] == 0
    assert result.handler_result["deleted"] == 0
    with factory() as verify:
        assert _request(verify, request_id).summary_markdown == "Working draft"


def test_a_still_reachable_intermediary_is_an_attention_reading(runtime_database):
    factory = runtime_database.session_factory
    project_id, request_id = _seed(factory)
    with factory() as citing:
        open_reference(
            citing,
            project_id=project_id,
            family="coordination_summary",
            source_row_id=request_id,
            kind="decision",
            referenced_by="decision:17",
        )
        citing.commit()

    result = _run(factory)

    assert result.execution_outcome == "completed"
    assert result.handler_result["health"] == "retention_attention_required"
    assert result.handler_result["refusal"] == "content_still_reachable"
    assert result.handler_result["deleted"] == 0
    assert result.safe_next_step == "inspect_retention_refusal"
    with factory() as verify:
        assert _request(verify, request_id).summary_markdown == "Working draft"


def test_the_sweep_stays_inside_the_project_it_was_declared_for(runtime_database):
    factory = runtime_database.session_factory
    _, scheduled_request_id = _seed(factory)
    with factory() as other:
        _, other_request = _project_with_due_content(other, slug_prefix="other-project")
        other_request_id = other_request.id
        other.commit()

    result = _run(factory)

    assert result.handler_result["deleted"] == 1
    with factory() as verify:
        assert _request(verify, scheduled_request_id).summary_markdown is None
        assert _request(verify, other_request_id).summary_markdown == "Working draft"


def test_gate7_refuses_a_sweep_that_names_no_human_authority(runtime_database):
    factory = runtime_database.session_factory
    with factory() as setup:
        project = Project(
            slug=f"sweep-gate7-{uuid4().hex[:8]}",
            name="Sweep Gate 7",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        base = RetentionSweepDeclaration.released_weekly(
            project_id=project.id,
            configuration_version="retention-sweep-v1",
            authorized_by=OPERATOR.subject,
            starts_at=NOW,
        )
        with pytest.raises(DueWorkRefusal, match="human principal"):
            configure_retention_sweep(
                setup, replace(base, authorized_by="local:system"), now=NOW
            )
        with pytest.raises(DueWorkRefusal, match="human principal"):
            configure_retention_sweep(
                setup, replace(base, authorized_by="operations"), now=NOW
            )
        with pytest.raises(DueWorkRefusal, match="weekly UTC latest-only"):
            configure_retention_sweep(setup, replace(base, cadence="hourly"), now=NOW)
