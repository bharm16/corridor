"""Committed transactions compete for one explicit scoped selection successor."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, func, select

from corridor.models import (
    ActiveExtractionRun, Candidate, ExtractionRun, Fact, PipelineObservation,
    PipelineSelection, Project, RecordInclusionRequest, SourceSegment,
)
from corridor.native_pipeline import run_native_matrix_shadow
from corridor.pipeline_qualification import (
    PipelineQualificationRefused, pipeline_receipt, select_qualified_pipeline,
)
from test_native_matrix import _document, matrix_source, project, session
from test_native_pipeline import ACTOR, _client, _gate_fixture, _plan, _qualify, _scope


def test_competing_maintainers_get_one_successor_and_a_stale_refusal(runtime_database, matrix_source, tmp_path):
    with runtime_database.session_factory() as session, session.begin():
        project = Project(slug="pipeline-concurrency", name="Synthetic selection concurrency", is_synthetic=True)
        session.add(project)
        session.flush()
        gate = _qualify(session, _gate_fixture(session, project, matrix_source, tmp_path))
        qualification_id = gate.id
    ready = Barrier(2)

    def select_once():
        with runtime_database.session_factory() as session, session.begin():
            ready.wait(timeout=10)
            try:
                row = select_qualified_pipeline(session, qualification_id, actor=ACTOR,
                    reason="One explicit synthetic selection", expected_selection_id=None)
                return ("selected", row.id)
            except PipelineQualificationRefused as exc:
                return ("refused", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: select_once(), range(2)))
    assert sorted(item[0] for item in results) == ["refused", "selected"]
    assert "predecessor changed" in next(item[1] for item in results if item[0] == "refused")
    with runtime_database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PipelineSelection)) == 1
        assert session.scalar(select(func.count()).select_from(ActiveExtractionRun)) == 0
        assert session.scalar(select(func.count()).select_from(RecordInclusionRequest)) == 0


def test_failed_selection_transaction_leaves_no_successor(session, project, matrix_source, tmp_path):
    gate = _qualify(session, _gate_fixture(session, project, matrix_source, tmp_path))
    with pytest.raises(RuntimeError, match="abort fixture"), session.begin_nested():
        select_qualified_pipeline(session, gate.id, actor=ACTOR, reason="Will roll back", expected_selection_id=None)
        raise RuntimeError("abort fixture")
    assert session.scalar(select(func.count()).select_from(PipelineSelection)) == 0
    final = select_qualified_pipeline(session, gate.id, actor=ACTOR,
                                     reason="Still the initial selection", expected_selection_id=None)
    assert pipeline_receipt(final)["previous_selection_id"] is None


def test_final_observation_failure_rolls_back_its_entire_source_capture(session, project, matrix_source, tmp_path):
    document = _document(session, project, matrix_source)

    def fail_receipt(scoped, context, instances):
        if any(isinstance(row, PipelineObservation) for row in scoped.new):
            raise RuntimeError("injected final observation failure")

    event.listen(session, "before_flush", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="final observation"):
            run_native_matrix_shadow(session, document, source_path=matrix_source.path,
                scope=_scope(matrix_source), client=_client(matrix_source), plan=_plan(source=matrix_source),
                output_dir=tmp_path / "failed-receipt", document_label=matrix_source.path.name)
    finally:
        event.remove(session, "before_flush", fail_receipt)
    for model in (SourceSegment, Fact, Candidate, ExtractionRun, PipelineObservation):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_selection_cannot_use_a_free_text_actor(session, project, matrix_source, tmp_path):
    gate = _qualify(session, _gate_fixture(session, project, matrix_source, tmp_path))
    with pytest.raises(ValueError, match="HumanPrincipal"):
        select_qualified_pipeline(session, gate.id, actor="pipeline:test-recorder",
                                  reason="Measurement identity is not human approval", expected_selection_id=None)
