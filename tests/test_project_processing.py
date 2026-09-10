"""The public project-processing pass over real PostgreSQL.

Extraction and Record Inclusion are exercised together through their committed
transactions, with the model boundary and clock controlled. The scenarios are
the hostile ones #342 requires: sole and mixed inputs, a successful zero-row
read, a failed document beside a clean sibling, a no-work pass, a duplicate
trigger, competing reconciliation, and — the load-bearing case — a completed
extraction whose Record Inclusion is finished only after a restart, with no
second model read and no duplicate project fact.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import hashlib
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.extract_project import extract_project
from corridor.models import (
    Candidate,
    Dependency,
    DocPage,
    Document,
    DocumentQuarantine,
    ExternalOrg,
    ExtractionRun,
    PolicyRun,
    Project,
)
from corridor.pipeline import EXTRACTED_PROPOSALS, ExtractionRoute
from corridor.project_processing import (
    ProcessingScopeRefused,
    process_project,
    summarize_pass,
)
from corridor.admission import reconcile_record_inclusion
from corridor.record_inclusion import record_inclusion_pending

PIPELINE = "Tejas Pipeline Co"
PROMPT_VERSION = "matrix_v1"
SCHEMA_VERSION = "matrix_candidate_shape_v1"
MODEL = "gpt-test"


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


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


class ScriptedRoute:
    """A per-document reader whose script and invocation count the test owns."""

    def __init__(self, script: dict[str, object]):
        # filename -> list[str] of conflict uids, or an Exception to raise.
        self._script = script
        self.extract_calls: dict[str, int] = {}

    def __call__(self, document: Document) -> ExtractionRoute:
        return ExtractionRoute(
            output=EXTRACTED_PROPOSALS,
            effective_prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            extract=self._extract,
            model=MODEL,
            allow_unsealed_legacy=True,
        )

    def _extract(self, session, document):
        self.extract_calls[document.filename] = (
            self.extract_calls.get(document.filename, 0) + 1
        )
        planned = self._script[document.filename]
        if isinstance(planned, Exception):
            raise planned
        candidates = [
            _conflict(document.id, document.project_id, uid) for uid in planned
        ]
        for candidate in candidates:
            session.add(candidate)
        session.flush()
        return candidates


def _project(factory, **overrides) -> int:
    with factory() as setup:
        project = Project(
            slug=f"proc-{uuid4().hex}",
            name="Processing",
            is_synthetic=True,
            project_side_parties=["LJA Engineering"],
            **overrides,
        )
        setup.add(project)
        setup.flush([project])
        # Record Inclusion refuses to mint an External Organization (#345), so
        # the owner every conflict candidate cites must already be registered.
        setup.add(ExternalOrg(name=PIPELINE, aliases=[]))
        project_id = project.id
        setup.commit()
    return project_id


def _matrix(
    factory, project_id, filename, *, parse_status="parsed", doc_date=None
) -> int:
    with factory() as setup:
        document = Document(
            project_id=project_id,
            sha256=hashlib.sha256(f"{project_id}:{filename}".encode()).hexdigest(),
            filename=filename,
            doc_type="matrix",
            parse_status=parse_status,
            pages=1,
            doc_date=doc_date,
        )
        setup.add(document)
        setup.flush([document])
        setup.add(DocPage(document_id=document.id, page_no=1, text="rows"))
        document_id = document.id
        setup.commit()
    return document_id


def _dependencies(session, project_id) -> set[str]:
    return {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project_id)
        )
    }


def _policy_run_count(session, project_id) -> int:
    return session.scalar(
        select(func.count()).select_from(PolicyRun).where(
            PolicyRun.project_id == project_id
        )
    )


def _completed_runs(session, project_id) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ExtractionRun)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project_id, ExtractionRun.outcome == "completed")
    )


NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


def test_sole_matrix_processes_and_lands_its_conflicts(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1", "PL2"]})

    result = process_project(
        factory,
        project_id=project_id,
        select_route=route,
        clock=ControlledClock(NOW),
    )

    assert result.extracted == 1
    assert result.eligible_document_count == 1
    assert result.reconciled is True
    assert result.admitted == 2
    assert result.processing_failures == []
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}


def test_a_failed_document_does_not_stop_a_clean_sibling(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "good.pdf", doc_date=date(2025, 1, 1))
    _matrix(factory, project_id, "bad.pdf", doc_date=date(2025, 1, 2))
    route = ScriptedRoute(
        {"good.pdf": ["PL1"], "bad.pdf": RuntimeError("reader exploded")}
    )

    result = process_project(
        factory,
        project_id=project_id,
        select_route=route,
        clock=ControlledClock(NOW),
    )

    assert result.extracted == 1
    assert len(result.processing_failures) == 1
    assert "bad.pdf" in result.processing_failures[0] or "RuntimeError" in (
        result.processing_failures[0]
    )
    # The clean sibling still reached the record.
    assert result.admitted == 1
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1"}


def test_successful_zero_row_read_is_completed_and_reconciled(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "empty.pdf")
    route = ScriptedRoute({"empty.pdf": []})

    first = process_project(
        factory,
        project_id=project_id,
        select_route=route,
        clock=ControlledClock(NOW),
    )
    assert first.extracted == 1
    assert first.admitted == 0
    assert first.reconciled is True  # a completed zero-row read still reconciles
    with factory() as verify:
        completed = _completed_runs(verify, project_id)
        assert completed == 1
        baseline_runs = _policy_run_count(verify, project_id)

    # A second pass skips the completed zero-row document, reconciles nothing,
    # and appends no new Policy Runs.
    second = process_project(
        factory,
        project_id=project_id,
        select_route=route,
        clock=ControlledClock(NOW),
    )
    assert second.skipped == 1
    assert second.extracted == 0
    assert second.reconciled is False
    with factory() as verify:
        assert _completed_runs(verify, project_id) == 1
        assert _policy_run_count(verify, project_id) == baseline_runs
        assert route.extract_calls["empty.pdf"] == 1  # no second model read


def test_no_work_pass_on_a_clean_project_appends_no_policy_runs(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1"]})
    process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )
    with factory() as verify:
        baseline = _policy_run_count(verify, project_id)

    for _ in range(3):
        result = process_project(
            factory,
            project_id=project_id,
            select_route=route,
            clock=ControlledClock(NOW),
        )
        assert result.reconciled is False
    with factory() as verify:
        assert _policy_run_count(verify, project_id) == baseline
        assert _dependencies(verify, project_id) == {"PL1"}


def test_a_duplicate_trigger_adds_no_second_project_fact(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1", "PL2"]})

    first = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )
    second = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )

    assert first.admitted == 2
    assert second.admitted == 0
    assert second.skipped == 1
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}
        assert _completed_runs(verify, project_id) == 1


def test_restart_after_extraction_finishes_the_load_without_a_second_read(
    runtime_database,
):
    """The load-bearing invariant: a crash after extraction commits but before
    Record Inclusion finishes must, on restart, reconcile the load without a
    second completed model read and without a duplicate project fact."""

    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1", "PL2"]})

    # Simulate the crash: run only extraction to a committed completion. The
    # producer coupling dirties the watermark; the load never ran.
    with factory() as extracting:
        project = extracting.get(Project, project_id)
        extract_project(
            extracting,
            project,
            select_route=route,
            commit=True,
        )
    with factory() as verify:
        assert record_inclusion_pending(verify, project_id) is True
        assert _dependencies(verify, project_id) == set()
        assert _completed_runs(verify, project_id) == 1
    assert route.extract_calls["ucm.pdf"] == 1

    # Restart: the full pass skips the completed extraction (no second read) and
    # finishes the pending load.
    restart = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )
    assert restart.skipped == 1
    assert restart.extracted == 0
    assert restart.reconciled is True
    assert restart.admitted == 2
    assert route.extract_calls["ucm.pdf"] == 1  # still exactly one model read
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}
        assert _completed_runs(verify, project_id) == 1
        runs_after_restart = _policy_run_count(verify, project_id)

    # And a further pass is an idle no-op.
    idle = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )
    assert idle.reconciled is False
    with factory() as verify:
        assert _policy_run_count(verify, project_id) == runs_after_restart
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}


def test_competing_reconciliation_loads_once_with_no_duplicate_fact(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1"]})
    with factory() as extracting:
        project = extracting.get(Project, project_id)
        extract_project(extracting, project, select_route=route, commit=True)

    ready = Barrier(2)

    def reconcile(_index):
        ready.wait(timeout=5)
        with factory() as session:
            with session.begin():
                result = reconcile_record_inclusion(session, project_id)
            return result.did_load

    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = list(pool.map(reconcile, range(2)))

    assert sorted(loaded) == [False, True]  # exactly one load; the other a no-op
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1"}


def test_held_superseded_and_unparsed_documents_are_excluded_before_model_work(
    runtime_database,
):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "good.pdf", doc_date=date(2025, 1, 1))
    quarantined = _matrix(factory, project_id, "seq.pdf", doc_date=date(2025, 1, 2))
    _matrix(factory, project_id, "unparsed.pdf", parse_status="pending",
            doc_date=date(2025, 1, 3))
    with factory() as hold:
        hold.add(DocumentQuarantine(document_id=quarantined, reason="sequencing"))
        hold.commit()

    route = ScriptedRoute(
        {
            "good.pdf": ["PL1"],
            # These must never be read; a call would raise KeyError here.
        }
    )
    result = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )

    assert result.eligible_document_count == 1
    assert result.extracted == 1
    assert result.excluded["held_quarantined"] == 1
    assert result.excluded["failed_parse"] == 1
    assert result.held_out == 2
    assert set(route.extract_calls) == {"good.pdf"}
    # Held-out documents are reported but are a steady state, not a failure of
    # this pass, so the pass is still healthy.
    receipt = summarize_pass(
        result, configuration_version="v", observed_at=NOW
    )
    assert receipt["health"] == "healthy"
    assert receipt["held_out"] == 2
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1"}


def test_an_unknown_project_is_refused_before_any_model_work(runtime_database):
    factory = runtime_database.session_factory
    route = ScriptedRoute({})
    with pytest.raises(ProcessingScopeRefused):
        process_project(
            factory,
            project_id=987654321,
            select_route=route,
            clock=ControlledClock(NOW),
        )
    assert route.extract_calls == {}
