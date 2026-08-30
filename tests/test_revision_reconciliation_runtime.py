"""The revision-reconciliation handler through the shared Due Work runtime.

These tests exercise the additive deterministic handler: a gate-7 declaration
enables one project's revision reconciliation, the runtime claims and finalizes
it with a bounded receipt, idle hourly ticks append no new Policy Runs, a silent
non-zero model budget is refused with nothing written (the handler authorizes no
model spending), and the handler stays disabled until it is declared.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.adjudicate import accept_candidate
from corridor.automatic_carry_forward import POLICY_VERSION
from corridor.due_work import (
    DueWorkRefusal,
    HANDLER_REVISION_RECONCILIATION,
    RevisionReconciliationDeclaration,
    configure_revision_reconciliation,
    due_work_status,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    DueWorkSchedule,
    EvidenceLink,
    PolicyRun,
    Project,
    RevisionComparisonRun,
)
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import DEFAULT_MATCHER_VERSION
from corridor.revision_reconciliation_request import revision_reconciliation_pending
from corridor.supersession import SupersessionDeclaration, register_supersessions


REVIEWER = HumanPrincipal("local:revision-runtime")


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _document(session, project, *, registry_id, sha_character, filename, page_text):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha_character * 64,
        filename=filename,
        doc_type="other" if registry_id == "INDEX" else "matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    session.add(
        DocPage(document_id=document.id, page_no=1, text=page_text, image_path="/tmp/p")
    )
    session.flush()
    return document


def _fields():
    return {
        "utility_id": "FOC1-1",
        "external_org": "AT&T",
        "utility_type": "Telecom",
        "station_from": "100+00",
    }


def _quote(fields):
    return " ".join(fields[k] for k in ("utility_id", "external_org", "utility_type", "station_from"))


def _candidate(project, document, fields):
    return Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": dict(fields),
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": _quote(fields),
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="matrix-v1",
        model="test-model",
        citations_verified=True,
    )


def _completed_run(session, document, *candidates):
    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix-v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=list(candidates),
        model="test-model",
        schema_version="candidate-v1",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=REVIEWER)
    session.flush()
    return run


def _seed_committed_transition(factory, now):
    with factory() as setup:
        project = Project(
            slug=f"rev-runtime-{uuid4().hex}",
            name="Revision Runtime",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        fields = _fields()
        predecessor = _document(
            setup, project, registry_id="REV-A", sha_character="a",
            filename="a.pdf", page_text=_quote(fields),
        )
        successor = _document(
            setup, project, registry_id="REV-B", sha_character="b",
            filename="b.pdf", page_text=_quote(fields),
        )
        _document(
            setup, project, registry_id="INDEX", sha_character="c",
            filename="index.pdf", page_text="REV-A superseded by REV-B",
        )
        predecessor_candidate = _candidate(project, predecessor, fields)
        _completed_run(setup, predecessor, predecessor_candidate)
        dependency = accept_candidate(setup, predecessor_candidate, principal=REVIEWER)
        evidence = setup.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.dependency_id == dependency.id)
            .order_by(EvidenceLink.id)
        ).one()
        mark_satisfies(setup, dependency.id, evidence.id, principal=REVIEWER)
        register_supersessions(
            setup,
            [
                SupersessionDeclaration(
                    predecessor_registry_id="REV-A",
                    successor_registry_id="REV-B",
                    replacement_date=date(2026, 8, 1),
                    source_registry_id="INDEX",
                    source_page=1,
                )
            ],
            project_id=project.id,
        )
        _completed_run(setup, successor, _candidate(project, successor, _fields()))

        declaration = RevisionReconciliationDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="revision-reconciliation-v1",
            matcher_identity=DEFAULT_MATCHER_VERSION,
            support_rule_identity=POLICY_VERSION,
            starts_at=now.replace(minute=0, second=0, microsecond=0),
        )
        schedule = configure_revision_reconciliation(setup, declaration, now=now)
        ids = (project.id, schedule.id)
        setup.commit()
    return ids


def _policy_runs(session, project_id):
    return session.scalar(
        select(func.count()).select_from(PolicyRun).where(
            PolicyRun.project_id == project_id
        )
    )


def test_handler_reconciles_a_project_to_a_completed_receipt(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    project_id, schedule_id = _seed_committed_transition(factory, now)

    with factory() as ticking:
        [occurrence] = enqueue_due_work(ticking, now=now)
        assert occurrence.scheduled_job_id == schedule_id
        ticking.commit()

    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:revision-worker"
    )
    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_key == HANDLER_REVISION_RECONCILIATION
    assert result.handler_result["schema_version"] == "revision-reconciliation-result-v1"
    assert result.handler_result["did_reconcile"] is True
    assert result.handler_result["comparisons_created"] == 1
    assert result.handler_result["carried"] == 1
    assert result.handler_result["requested_record_inclusion"] is True
    assert result.safe_next_step == "none"

    with factory() as verify:
        assert revision_reconciliation_pending(verify, project_id) is False
        assert (
            verify.scalar(
                select(func.count()).select_from(RevisionComparisonRun).where(
                    RevisionComparisonRun.project_id == project_id
                )
            )
            == 1
        )
        status = due_work_status(verify, project_id=project_id)
        assert status["occurrences"][0]["state"] == "completed"
        assert status["receipts"][0]["handler"] == HANDLER_REVISION_RECONCILIATION


def test_idle_hourly_ticks_append_no_new_policy_runs(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    project_id, _ = _seed_committed_transition(factory, now)

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    first = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:revision-idle"
    )
    assert first.handler_result["carried"] == 1
    with factory() as verify:
        baseline = _policy_runs(verify, project_id)

    # Later hours claim real occurrences, but the watermark is clean and no
    # support is newly eligible, so the pass reconciles nothing and appends no
    # Carry-Forward Policy Run.
    for hour in (9, 10, 11):
        tick_at = now.replace(hour=hour)
        with factory() as ticking:
            enqueue_due_work(ticking, now=tick_at)
            ticking.commit()
        result = run_due_work_once(
            factory, clock=ControlledClock(tick_at), owner="runtime:revision-idle"
        )
        assert result.execution_outcome == "completed"
        assert result.handler_result["did_reconcile"] is False
        assert result.handler_result["carried"] == 0

    with factory() as verify:
        assert _policy_runs(verify, project_id) == baseline


def test_gate7_refuses_a_nonzero_model_budget_without_writing_a_schedule(
    runtime_database,
):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"rev-gate7-{uuid4().hex}", name="Rev Gate 7", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        base = RevisionReconciliationDeclaration.released_hourly(
            project_id=project.id,
            configuration_version="revision-reconciliation-v1",
            matcher_identity=DEFAULT_MATCHER_VERSION,
            support_rule_identity=POLICY_VERSION,
            starts_at=now,
        )
        # A deterministic handler authorizes no model spending: any positive
        # budget is not a valid declaration.
        with pytest.raises(DueWorkRefusal, match="resource declaration is invalid"):
            configure_revision_reconciliation(
                setup, replace(base, model_token_budget=1), now=now
            )
        # A malformed matcher identity is refused too.
        with pytest.raises(DueWorkRefusal, match="matcher identity"):
            configure_revision_reconciliation(
                setup, replace(base, matcher_identity="Bad Matcher!"), now=now
            )
        assert setup.scalars(
            select(DueWorkSchedule).where(DueWorkSchedule.project_id == project.id)
        ).all() == []
        setup.commit()


def test_revision_reconciliation_is_disabled_until_gate7_is_declared(runtime_database):
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
    with factory() as setup:
        project = Project(
            slug=f"rev-undeclared-{uuid4().hex}", name="Undeclared", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        project_id = project.id
        setup.commit()

    # No gate-7 declaration exists, so nothing enqueues and nothing runs.
    with factory() as ticking:
        assert enqueue_due_work(ticking, now=now) == ()
        ticking.commit()
    result = run_due_work_once(
        factory, clock=ControlledClock(now), owner="runtime:revision-idle"
    )
    assert result is None
    with factory() as verify:
        assert verify.scalars(
            select(DueWorkSchedule).where(DueWorkSchedule.project_id == project_id)
        ).all() == []
