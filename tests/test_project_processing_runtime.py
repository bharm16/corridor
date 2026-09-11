"""The effectful project-processing handler through the shared Due Work runtime.

The read-only health handler keeps its own suite; these tests exercise the
additive effectful path: a gate-7 declaration enables one project sweep, the
runtime claims and finalizes it with a bounded receipt, idle hourly ticks append
no new Policy Runs, invalid configuration is refused with nothing written, the
handler stays disabled until it is declared, and a finalize that misses its
deadline still leaves the domain work durable and the occurrence recoverable.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.due_work import (
    DueWorkRefusal,
    HANDLER_PROJECT_PROCESSING,
    HandlerContract,
    ProjectProcessingDeclaration,
    configure_due_work,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.models import (
    Candidate,
    Dependency,
    DocPage,
    Document,
    DueWorkSchedule,
    ExternalOrg,
    PolicyRun,
    Project,
)
from corridor.pipeline import EXTRACTED_PROPOSALS, ExtractionRoute
from corridor.project_processing import process_project, summarize_pass
from clock_support import ControlledClock

PIPELINE = "Tejas Pipeline Co"
PROMPT_VERSION = "matrix_v1"
SCHEMA_VERSION = "matrix_candidate_shape_v1"
MODEL = "gpt-test"
EXTRACTOR_IDENTITY = "deployed-matrix-v1"


class SteppingClock:
    def __init__(self, values):
        self.values = list(values)
        self.index = 0

    def now(self) -> datetime:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


def _conflict(document_id, project_id, uid):
    fields = {
        "utility_id": uid,
        "external_org": PIPELINE,
        "utility_type": "Petroleum and Gaseous Materials",
        "station_from": "1102+20",
        "station_to": "1102+80",
    }
    quote = " | ".join(fields.values())
    return Candidate(
        project_id=project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields,
            "citations": [
                {
                    "document_id": document_id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": quote,
            "text_source": "text_layer",
        },
        source_document_id=document_id,
        source_pages=[1],
        confidence=0.99,
        prompt_version=PROMPT_VERSION,
        model=MODEL,
        citations_verified=True,
    )


def _scripted_route(script):
    calls = {"count": 0}

    def select_route(document):
        def extract(session, target):
            calls["count"] += 1
            planned = script[target.filename]
            candidates = [
                _conflict(target.id, target.project_id, uid) for uid in planned
            ]
            for candidate in candidates:
                session.add(candidate)
            session.flush()
            return candidates

        return ExtractionRoute(
            output=EXTRACTED_PROPOSALS,
            effective_prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            extract=extract,
            model=MODEL,
            allow_unsealed_legacy=True,
        )

    return select_route, calls


def _registry(select_route):
    """A handler registry whose effectful handler uses the injected route."""

    def run_effectful(context):
        with context.session_factory() as reading:
            schedule = reading.get(DueWorkSchedule, context.claim.schedule_id)
            project_id = schedule.project_id
            configuration_version = schedule.configuration_version
        result = process_project(
            context.session_factory,
            project_id=project_id,
            select_route=select_route,
            clock=context.clock,
        )
        return summarize_pass(
            result,
            configuration_version=configuration_version,
            observed_at=context.clock.now(),
        )

    return {
        HANDLER_PROJECT_PROCESSING: HandlerContract(
            key=HANDLER_PROJECT_PROCESSING,
            scope_kind="one_registered_project_extraction",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=8192,
            model_token_budget=100_000_000,
            notification_budget=0,
            run_effectful=run_effectful,
        )
    }


def _project_with_matrix(factory, now, filename="ucm.pdf", **declaration_overrides):
    with factory() as setup:
        project = Project(
            slug=f"proc-rt-{uuid4().hex}",
            name="Processing Runtime",
            is_synthetic=True,
            project_side_parties=["LJA Engineering"],
        )
        setup.add(project)
        setup.flush([project])
        # Record Inclusion refuses to mint an External Organization (#345), so
        # the owner every conflict candidate cites must already be registered.
        setup.add(ExternalOrg(name=PIPELINE, aliases=[]))
        document = Document(
            project_id=project.id,
            sha256=hashlib.sha256(f"{project.id}:{filename}".encode()).hexdigest(),
            filename=filename,
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        setup.add(document)
        setup.flush([document])
        setup.add(DocPage(document_id=document.id, page_no=1, text="rows"))
        declaration = ProjectProcessingDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="project-processing-v1",
            extractor_identity=EXTRACTOR_IDENTITY,
            starts_at=now.replace(minute=0, second=0, microsecond=0),
        )
        if declaration_overrides:
            declaration = replace(declaration, **declaration_overrides)
        schedule = configure_due_work(setup, declaration, now=now)
        ids = (project.id, schedule.id)
        setup.commit()
    return ids


def _policy_runs(session, project_id):
    return session.scalar(
        select(func.count()).select_from(PolicyRun).where(
            PolicyRun.project_id == project_id
        )
    )


def _dependencies(session, project_id):
    return {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project_id)
        )
    }


def test_effectful_handler_processes_a_project_to_a_completed_receipt(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    project_id, schedule_id = _project_with_matrix(factory, now)
    select_route, calls = _scripted_route({"ucm.pdf": ["PL1", "PL2"]})

    with factory() as ticking:
        [occurrence] = enqueue_due_work(ticking, now=now)
        assert occurrence.scheduled_job_id == schedule_id
        ticking.commit()

    result = run_due_work_once(
        factory,
        clock=ControlledClock(now),
        owner="runtime:processing-worker",
        registry=_registry(select_route),
    )
    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_key == HANDLER_PROJECT_PROCESSING
    assert result.handler_result["schema_version"] == "project-processing-result-v3"
    assert result.handler_result["admitted"] == 2
    assert result.handler_result["reconciled"] is True
    assert result.safe_next_step == "none"
    assert calls["count"] == 1

    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}
        status = due_work_status(verify, project_id=project_id)
        assert status["occurrences"][0]["state"] == "completed"
        assert status["receipts"][0]["handler"] == HANDLER_PROJECT_PROCESSING


def test_idle_hourly_ticks_append_no_new_policy_runs(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    project_id, _ = _project_with_matrix(factory, now)
    select_route, calls = _scripted_route({"ucm.pdf": ["PL1"]})
    registry = _registry(select_route)

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    first = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:idle-worker", registry=registry
    )
    assert first.handler_result["admitted"] == 1
    with factory() as verify:
        baseline = _policy_runs(verify, project_id)
    assert calls["count"] == 1

    # Three later hours: each is a real claimed occurrence, but the watermark is
    # clean and every extraction is skipped, so no Policy Run is appended and the
    # model is never read again.
    for hour in (9, 10, 11):
        tick_at = now.replace(hour=hour)
        with factory() as ticking:
            enqueue_due_work(ticking, now=tick_at)
            ticking.commit()
        result = run_due_work_once(
            factory,
            clock=ControlledClock(tick_at),
            owner="runtime:idle-worker",
            registry=registry,
        )
        assert result.execution_outcome == "completed"
        assert result.handler_result["reconciled"] is False
        assert result.handler_result["skipped"] == 1

    with factory() as verify:
        assert _policy_runs(verify, project_id) == baseline
        assert _dependencies(verify, project_id) == {"PL1"}
    assert calls["count"] == 1


def test_gate7_refuses_a_silent_zero_budget_without_writing_a_schedule(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"gate7-{uuid4().hex}", name="Gate 7", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        base = ProjectProcessingDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="project-processing-v1",
            extractor_identity=EXTRACTOR_IDENTITY,
            starts_at=now,
        )
        # A silent zero model budget is not a valid production declaration.
        with pytest.raises(DueWorkRefusal, match="resource declaration is invalid"):
            configure_due_work(
                setup, replace(base, model_token_budget=0), now=now
            )
        # An unsupported cadence is refused too.
        with pytest.raises(DueWorkRefusal, match="hourly UTC"):
            configure_due_work(
                setup, replace(base, cadence="daily"), now=now
            )
        # A malformed extractor identity is refused.
        with pytest.raises(DueWorkRefusal, match="extractor identity"):
            configure_due_work(
                setup, replace(base, extractor_identity="Bad Identity!"), now=now
            )
        assert setup.scalars(
            select(DueWorkSchedule).where(
                DueWorkSchedule.project_id == project.id
            )
        ).all() == []


def test_project_processing_is_disabled_until_gate7_is_declared(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"undeclared-{uuid4().hex}", name="Undeclared", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        document = Document(
            project_id=project.id,
            sha256="a" * 64,
            filename="ucm.pdf",
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        setup.add(document)
        project_id = project.id
        setup.commit()

    # No gate-7 declaration exists, so nothing enqueues and nothing runs — model
    # credentials alone never authorize a project sweep.
    with factory() as ticking:
        assert enqueue_due_work(ticking, now=now) == ()
        ticking.commit()
    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:idle-worker"
    )
    assert result is None
    with factory() as verify:
        assert verify.scalars(
            select(DueWorkSchedule).where(DueWorkSchedule.project_id == project_id)
        ).all() == []


def test_a_deadline_finalize_keeps_domain_work_and_recovers_the_occurrence(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc)
    # A short deadline inside a long claim lease leaves a window where the
    # finalize is past the deadline but the claim is still owned.
    project_id, _ = _project_with_matrix(
        factory, now, deadline_seconds=60, claim_ttl_seconds=1800
    )
    select_route, calls = _scripted_route({"ucm.pdf": ["PL1"]})
    registry = _registry(select_route)

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()

    # started_at and the handler's observed_at land on time; the finalize clock
    # is past the 60s deadline but well within the 1800s lease.
    late = now + timedelta(seconds=120)
    deadline_clock = SteppingClock([now, now, late])
    failed = run_due_work_once(
        factory,
        clock=deadline_clock,
        owner="runtime:deadline-worker",
        registry=registry,
    )
    assert failed.execution_outcome in {"retry_due", "failed"}
    assert failed.error_code == "deadline_exceeded"
    # The domain work committed before finalize is durable regardless.
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1"}
    assert calls["count"] == 1

    # Recovery: a later claim re-runs the occurrence. Extraction is skipped and
    # Record Inclusion is already reconciled, so the re-run completes with no
    # duplicate fact and no second model read.
    recover_at = now + timedelta(hours=3)
    with factory() as ticking:
        enqueue_due_work(ticking, now=recover_at)
        ticking.commit()
    recovered = run_due_work_once(
        factory,
        clock=ControlledClock(recover_at),
        owner="runtime:recovery-worker",
        registry=registry,
    )
    assert recovered is not None
    assert recovered.execution_outcome == "completed"
    assert calls["count"] == 1
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1"}
