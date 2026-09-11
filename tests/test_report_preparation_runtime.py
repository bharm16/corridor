"""The report-preparation handler through the shared Due Work runtime (#488).

The change summary and the weekly report are rendered from one reading of what
changed since the last weekly artifact. These tests prove the reading is
produced on a weekly schedule, that consecutive readings tile without counting
a change twice, and that ADR-0084 holds: a deferred delta is neither resolved
nor mixed into the actionable open work.

They also pin the fix for a defect that passed by coincidence: the window used
to be ``created_at > previous observed_at``, which compares PostgreSQL's
insertion clock against the runtime's logical one. The reading now covers a
range of append-only identifiers, and one test takes the same reading under
absurd observation times to prove no clock can change its counts.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from corridor.due_work import (
    HANDLER_REPORT_PREPARATION,
    DueWorkRefusal,
    ReportPreparationDeclaration,
    configure_due_work,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.delta_resolution import (
    ChildDecisionRequest,
    RecordEffect,
    resolve_delta,
)
from corridor.models import (
    DueWorkSchedule,
    Fact,
    Project,
)
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
    record_delta_deferral,
)
from corridor.support_assessments import FactProposition, record_support_assessment
from corridor.report_preparation import execute_report_preparation
from clock_support import ControlledClock
from harness_support import accepted_revision
from source_capture_support import Rendition


COORDINATOR = HumanPrincipal("local:coordinator")


def _supported_fact(session, project_id: int, subject: str, value: str):
    """One captured Source Fact for a subject, with its value support.

    A #519 accept names the Source Fact it makes effective and the effective
    Support Assessment it relied on, so a reading that counts accepted deltas
    needs both to exist.
    """

    rendition = Rendition(
        session,
        session.get_one(Project, project_id),
        f"{subject}.xlsx",
        subject_key=subject,
    )
    fact, segment = rendition.capture(
        fact_type="station_from", value=value, cell="A2"
    )
    assessment = record_support_assessment(
        session,
        project_id=project_id,
        proposition=FactProposition(fact.id),
        source_segment_ids=[segment.id],
        evidence_role="value_support",
        assessment="supported",
        authority=COORDINATOR,
        assessed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    return fact, assessment


def _accept(session, project_id: int, delta, subject: str, value: str, at):
    fact, assessment = _supported_fact(session, project_id, subject, value)
    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project_id,
            delta_id=delta,
            action="accept",
            principal=COORDINATOR,
            idempotency_key=f"accept:{delta}",
            decided_at=at,
            record_effects=(RecordEffect(fact_id=fact.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )
    assert outcome.status == "resolved", outcome.refusal
    return outcome


def _reject(session, project_id: int, delta, at):
    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project_id,
            delta_id=delta,
            action="reject",
            principal=COORDINATOR,
            idempotency_key=f"reject:{delta}",
            decided_at=at,
        ),
    )
    assert outcome.status == "resolved", outcome.refusal
    return outcome


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
            accepted_revision(setup, project.id, key=f"adopt-{project.id}")
            if with_revision
            else 0
        )
        schedule = configure_due_work(
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
        accepted = _accept(
            writing,
            project_id,
            resolved.id,
            "UC-1",
            "1200+00",
            now - timedelta(minutes=5),
        )
        accepted_revision = accepted.revision_id
        record_delta_deferral(
            writing,
            project_id=project_id,
            delta_id=deferred.id,
            deferred_at=now - timedelta(minutes=5),
            scheduled_by_principal="local:coordinator",
            request_identity="schedule:report-preparation",
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
    assert body["through_delta_id"] > 0
    assert body["through_disposition_id"] > 0
    # Accepting a delta writes the next Project Record revision (#519), so the
    # reading stands on that one rather than on the adopted baseline.
    assert accepted_revision > revision
    assert body["accepted_revision_id"] == accepted_revision
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
        _reject(writing, project_id, first_id, later - timedelta(days=1))
        writing.commit()

    second = _run(factory, later)

    assert second.execution_outcome == "completed"
    body = second.handler_result
    # `window_start` is the sentence the summary prints, not the filter.
    assert body["window_start"] == opening.handler_result["observed_at"]
    # The delta itself was proposed in the previous window, so only its
    # resolution belongs to this one. The boundary is the retained watermark.
    assert body["through_delta_id"] == opening.handler_result["through_delta_id"]
    assert (
        body["through_disposition_id"]
        > opening.handler_result["through_disposition_id"]
    )
    assert body["proposed_new"] == 0
    assert body["resolved_rejected"] == 1
    assert body["open_actionable"] == 0


def test_the_window_is_a_watermark_and_not_a_clock_comparison(runtime_database):
    """The same reading, taken under absurd clocks, counts the same things.

    `ProposedDelta.created_at` is assigned by PostgreSQL's `now()` while the
    handler's `observed_at` comes from the runtime's clock. Bounding the week
    with one against the other was only ever right while the two happened to
    agree, so this takes the second reading a decade early and a decade late
    and requires both to agree with each other.
    """

    factory = runtime_database.session_factory
    now = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    project_id, schedule_id, revision = _seed_project(factory, now)
    with factory() as writing:
        first = _delta(writing, project_id, "UC-1", "1200+00", revision)
        writing.commit()
        first_id = first.id

    opening = _run(factory, now)
    assert opening.handler_result["proposed_new"] == 1

    # One more proposal and one resolution land after that retained reading.
    with factory() as writing:
        _delta(writing, project_id, "UC-2", "1300+00", revision)
        _accept(
            writing,
            project_id,
            first_id,
            "UC-1",
            "1200+00",
            now + timedelta(days=3),
        )
        writing.commit()

    readings = []
    for observed_at in (
        datetime(2016, 1, 1, tzinfo=timezone.utc),
        datetime(2036, 1, 1, tzinfo=timezone.utc),
    ):
        with factory() as reading:
            schedule = reading.get(DueWorkSchedule, schedule_id)
            readings.append(
                execute_report_preparation(reading, schedule, observed_at)
            )

    long_past, far_future = readings
    assert long_past["proposed_new"] == 1
    assert long_past["resolved_accepted"] == 1
    assert long_past["open_actionable"] == 1
    # Only the observation stamp itself may differ between the two.
    assert {
        key: value for key, value in long_past.items() if key != "observed_at"
    } == {key: value for key, value in far_future.items() if key != "observed_at"}


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
            configure_due_work(setup, replace(base, cadence="hourly"), now=now)
        with pytest.raises(DueWorkRefusal, match="resource declaration is invalid"):
            configure_due_work(
                setup, replace(base, model_token_budget=1), now=now
            )
