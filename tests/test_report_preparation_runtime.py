"""The report-preparation handler through the shared Due Work runtime (#488).

The change summary and the weekly report are rendered from one reading of what
changed since the last weekly artifact. These tests prove the reading is
produced on a weekly schedule, that consecutive readings tile without counting
a resolution twice, and that ADR-0084 holds: a deferred delta is neither
resolved nor mixed into the actionable open work.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from corridor.due_work import (
    HANDLER_REPORT_PREPARATION,
    DueWorkRefusal,
    ReportPreparationDeclaration,
    configure_report_preparation,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.models import Project
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
    record_delta_deferral,
    record_delta_disposition,
)


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _accepted_revision(session, project_id: int, key: str) -> int:
    """One Project Record revision, written as the record-decision role.

    The Adopt Baseline importer that will write this in production is #509; the
    reading only needs the revision its counts are stated against to exist.
    """

    session.execute(text("set local role corridor_fact_decision_writer"))
    revision_id = session.scalar(
        text(
            "insert into project_record_revisions ("
            "project_id, command_type, human_principal, idempotency_key"
            ") values (:project_id, 'adopt_baseline', 'local:adopter', :key)"
            " returning id"
        ),
        {"project_id": project_id, "key": key},
    )
    session.execute(text("reset role"))
    return int(revision_id)


def _delta(session, project_id, subject, value, revision):
    (delta,) = create_proposed_delta_group(
        session,
        project_id=project_id,
        source_family="REV-B",
        source_revision=f"rev-{subject}",
        deltas=[
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget(
                    subject_identity=subject, field="station_from"
                ),
                accepted_value="1149+00",
                proposed_value=value,
                accepted_baseline_revision=f"revision:{revision}",
            )
        ],
    )
    return delta


def _seed_project(factory, now, *, with_revision=True):
    with factory() as setup:
        project = Project(
            slug=f"report-preparation-{uuid4().hex[:8]}",
            name="Report Preparation",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        revision = (
            _accepted_revision(setup, project.id, f"adopt-{project.id}")
            if with_revision
            else 0
        )
        schedule = configure_report_preparation(
            setup,
            ReportPreparationDeclaration.released_weekly(
                project_id=project.id,
                configuration_version="report-preparation-v1",
                starts_at=now.replace(minute=0, second=0, microsecond=0),
            ),
            now=now,
        )
        ids = (project.id, schedule.id, revision)
        setup.commit()
    return ids


def _run(factory, at, owner="runtime:preparation-worker"):
    with factory() as ticking:
        enqueue_due_work(ticking, now=at)
        ticking.commit()
    return run_due_work_once(factory, clock=ControlledClock(at), owner=owner)


def test_the_first_reading_separates_resolved_deferred_and_open(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    project_id, _, revision = _seed_project(factory, now)

    with factory() as writing:
        resolved = _delta(writing, project_id, "UC-1", "1200+00", revision)
        deferred = _delta(writing, project_id, "UC-2", "1300+00", revision)
        _delta(writing, project_id, "UC-3", "1400+00", revision)
        record_delta_disposition(
            writing,
            project_id=project_id,
            delta_id=resolved.id,
            disposition="accept",
            decided_at=now - timedelta(minutes=5),
            decided_by_principal="local:coordinator",
        )
        record_delta_deferral(
            writing,
            project_id=project_id,
            delta_id=deferred.id,
            deferred_at=now - timedelta(minutes=5),
            scheduled_by_principal="local:coordinator",
            deferred_until=now + timedelta(days=30),
            wake_condition="awaiting utility reply",
        )
        writing.commit()

    result = _run(factory, now)

    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_key == HANDLER_REPORT_PREPARATION
    body = result.handler_result
    assert body["schema_version"] == "report-preparation-result-v1"
    assert body["health"] == "healthy"
    assert body["window_start"] == ""
    assert body["accepted_revision_id"] == revision
    assert body["proposed_new"] == 3
    assert body["resolved_accepted"] == 1
    assert body["resolved_edited"] == 0
    assert body["resolved_rejected"] == 0
    # ADR-0084: a deferred delta is open, and it is not actionable work.
    assert body["open_deferred"] == 1
    assert body["open_actionable"] == 1
    assert body["superseded"] == 0
    assert result.safe_next_step == "none"

    with factory() as verify:
        status = due_work_status(verify, project_id=project_id)
        assert status["receipts"][0]["handler"] == HANDLER_REPORT_PREPARATION


def test_the_next_weekly_reading_covers_only_the_week_since_the_last(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    project_id, _, revision = _seed_project(factory, now)
    with factory() as writing:
        first = _delta(writing, project_id, "UC-1", "1200+00", revision)
        writing.commit()
        first_id = first.id

    opening = _run(factory, now)
    assert opening.handler_result["proposed_new"] == 1
    assert opening.handler_result["open_actionable"] == 1

    later = now + timedelta(days=7)
    with factory() as writing:
        record_delta_disposition(
            writing,
            project_id=project_id,
            delta_id=first_id,
            disposition="reject",
            decided_at=later - timedelta(days=1),
            decided_by_principal="local:coordinator",
        )
        writing.commit()

    second = _run(factory, later)

    assert second.execution_outcome == "completed"
    body = second.handler_result
    assert body["window_start"] == opening.handler_result["observed_at"]
    # The delta itself was proposed in the previous window, so only its
    # resolution belongs to this one.
    assert body["proposed_new"] == 0
    assert body["resolved_rejected"] == 1
    assert body["open_actionable"] == 0


def test_a_project_with_no_accepted_revision_asks_for_a_baseline(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    _seed_project(factory, now, with_revision=False)

    result = _run(factory, now)

    assert result.execution_outcome == "completed"
    assert result.handler_result["health"] == "preparation_attention_required"
    assert result.handler_result["accepted_revision_id"] == 0
    assert result.safe_next_step == "adopt_project_baseline"


def test_gate7_refuses_an_hourly_change_summary_reading(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"preparation-gate7-{uuid4().hex[:8]}",
            name="Preparation Gate 7",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        base = ReportPreparationDeclaration.released_weekly(
            project_id=project.id,
            configuration_version="report-preparation-v1",
            starts_at=now,
        )
        with pytest.raises(DueWorkRefusal, match="weekly UTC latest-only"):
            configure_report_preparation(setup, replace(base, cadence="hourly"), now=now)
        with pytest.raises(DueWorkRefusal, match="resource declaration is invalid"):
            configure_report_preparation(
                setup, replace(base, model_token_budget=1), now=now
            )
