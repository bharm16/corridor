"""Actual native shadow execution and independently scoped maintenance authority."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from corridor.admission import load_project
from corridor.config import Settings
from corridor.models import (
    ActiveExtractionRun, Dependency, Fact, FactDisposition, PipelineComparison,
    PipelineObservation, PipelineQualification, PipelineSelection, ProcessingArtifact,
    Project, RecordInclusionRequest, RevisionReconciliationRequest,
)
from corridor.native_pipeline import (
    RecordedPipelineClient, register_pipeline_configuration, run_native_matrix_shadow,
    run_selected_native_matrix,
)
from corridor.pipeline_contracts import MeasuredEvidence, MetricRequirement, ObservationPlan, PipelineScope, QualificationPolicy, canonical_text, content_digest
from corridor.pipeline_qualification import (
    PipelineQualificationRefused, pipeline_receipt, record_quality, record_qualification,
    record_repeatability, select_qualified_pipeline, selected_pipeline_configuration,
    register_qualification_policy,
)
from corridor.principals import HumanPrincipal
from corridor_pdf_reader.replacement import semantics
from corridor_pdf_reader.replacement.pages import slim_page
from test_native_matrix import _document, matrix_source, project, session


ACTOR = HumanPrincipal("local:pipeline-test-maintainer")

PROTECTED_MODELS = (
    ActiveExtractionRun, Dependency, FactDisposition, PipelineSelection,
    RecordInclusionRequest, RevisionReconciliationRequest,
)


def _protected_rows(session):
    """Read every protected row and column, without project or ORM-cache filtering."""
    # Core column selects do not autoflush pending ORM writes. Include those
    # writes in both snapshots so returning with queued side effects also fails.
    session.flush()
    return {
        model.__tablename__: deepcopy([
            dict(row) for row in session.execute(
                select(*model.__table__.c).order_by(*model.__table__.primary_key.columns)
            ).mappings()
        ])
        for model in PROTECTED_MODELS
    }


def _assert_protected_rows_unchanged(session, before, *, new_selection_ids=()):
    after = _protected_rows(session)
    selections = after["pipeline_selections"]
    allowed = set(new_selection_ids)
    assert len(allowed) == len(new_selection_ids)
    assert allowed.isdisjoint(row["id"] for row in before["pipeline_selections"])
    assert {row["id"] for row in selections if row["id"] in allowed} == allowed
    # Only the explicit calls' returned selections may be new. Every old
    # selection and all protected state, including other projects, stays exact.
    after["pipeline_selections"] = [row for row in selections if row["id"] not in allowed]
    for table, rows in before.items():
        assert after[table] == rows, f"{table} changed outside the explicit selection append"


@pytest.fixture
def preexisting_pipeline_state(session, project, matrix_source, tmp_path):
    unrelated = Project(slug=f"native-pipeline-unrelated-{project.id}",
                        name="Unrelated pending reconciliation", is_synthetic=True)
    session.add(unrelated)
    session.flush()
    request = RecordInclusionRequest(
        project_id=unrelated.id, dirty_seq=7, reconciled_seq=3,
        last_reason="Existing work from another producer",
        requested_at=datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
        reconciled_at=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
    )
    session.add(request)
    session.flush()
    selection_directory = tmp_path / "unrelated-selection"
    selection_directory.mkdir()
    gate = _qualify(session, _gate_fixture(session, unrelated, matrix_source, selection_directory))
    selection = select_qualified_pipeline(
        session, gate.id, actor=ACTOR, reason="Earlier unrelated synthetic selection",
        expected_selection_id=None,
    )
    return request, selection


@pytest.mark.parametrize("operation", ["insert", "update", "delete"])
def test_protected_snapshot_includes_pending_orm_changes(session, project, preexisting_pipeline_state, operation):
    before = _protected_rows(session)
    existing = preexisting_pipeline_state[0]
    if operation == "insert":
        pending = RecordInclusionRequest(project_id=project.id, dirty_seq=1,
                                         reconciled_seq=0, last_reason="Unflushed new request")
        session.add(pending)
        assert pending in session.new
    elif operation == "update":
        existing.dirty_seq += 1
        existing.last_reason = "Unflushed changed request"
        assert existing in session.dirty
    else:
        session.delete(existing)
        assert existing in session.deleted
    # Deliberately no explicit or query-triggered flush in the injection.
    with pytest.raises(AssertionError, match="record_inclusion_requests changed"):
        _assert_protected_rows_unchanged(session, before)


def _plan(mode="synthetic", *, source=None):
    return ObservationPlan(mode=mode, origin_sha256=_client(source).origin_sha256 if source else "f" * 64,
                           source_permission="synthetic" if mode == "synthetic" else "public",
                           description="Source-authored offline fixture; no provider observation")


def _scope(source, *, purpose="synthetic_validation"):
    return PipelineScope(deployment="test-fixture", source_sha256s=(source.reading.rendition_sha256,),
                         corpus_version="source-authored-fixture-v1", corpus_sha256="c" * 64,
                         purpose=purpose)


def _client(source):
    return RecordedPipelineClient([{
        "system_sha256": sha256(semantics.PROMPT_PATH.read_bytes()).hexdigest(),
        "schema_sha256": content_digest(semantics.STRUCTURE_SCHEMA),
        "user": semantics.listing(slim_page(page), source.path.name),
        "image_sha256s": [sha256(source.images[page["number"]].read_bytes()).hexdigest()],
        "answer": source.answers[index],
    } for index, page in enumerate(source.reading.pages)])


def test_actual_native_pipeline_renders_maps_and_appends_without_selection(session, project, matrix_source, tmp_path, preexisting_pipeline_state):
    document = _document(session, project, matrix_source)
    scope = _scope(matrix_source)
    protected_before = _protected_rows(session)
    first = run_native_matrix_shadow(
        session, document, source_path=matrix_source.path, scope=scope, client=_client(matrix_source),
        plan=_plan(source=matrix_source), output_dir=tmp_path / "first", document_label=matrix_source.path.name,
    )
    _assert_protected_rows_unchanged(session, protected_before)
    record = pipeline_receipt(first.observation)
    assert record["disposition"] == "completed", record["output"]
    assert first.extraction is not None
    # Four body rows are in the frozen reader's table; its final wholly blank
    # ruled row is outside that detected table, not an extra cohort outcome.
    assert len(record["output"]["rows"]) == 4
    assert first.extraction.facts
    assert record["chain"]["model_context"]["1"]["dpi"] == 110
    assert record["chain"]["model_context"]["1"]["sha256"] == sha256(matrix_source.images[1].read_bytes()).hexdigest()
    assert record["chain"]["geometry"][0]["derivative"]["rasterizer"] == "pdfium"
    assert record["chain"]["geometry"][0]["crops"]
    assert record["metrics"]["outbound_attempts"] == 0
    assert record["metrics"]["observed_new_provider_cost_usd"] == 0
    assert record["metrics"]["peak_process_tree_rss_bytes"] is None
    assert record["metrics"]["processing_cost_usd"] is None
    assert record["metrics"]["latency_ms"] > 0
    assert session.scalars(select(ProcessingArtifact)).all()
    assert all(row.retention_class == "class_b" for row in session.scalars(select(ProcessingArtifact)))
    second = run_native_matrix_shadow(
        session, document, source_path=matrix_source.path, scope=scope, client=_client(matrix_source),
        plan=_plan(source=matrix_source), output_dir=tmp_path / "second", document_label=matrix_source.path.name,
    )
    _assert_protected_rows_unchanged(session, protected_before)
    assert second.extraction.run.id == first.extraction.run.id
    repeated = record_repeatability(session, first.observation.id, second.observation.id, actor=ACTOR)
    assert pipeline_receipt(repeated)["passed"]
    gate = record_qualification(session, observation_ids=[first.observation.id], repeatability_ids=[repeated.id], actor=ACTOR)
    assert gate.status == "incomplete"
    assert "predeclared_metric_policy" in pipeline_receipt(gate)["missing"]
    with pytest.raises(PipelineQualificationRefused, match="not complete"):
        select_qualified_pipeline(session, gate.id, actor=ACTOR, reason="Must refuse", expected_selection_id=None)
    _assert_protected_rows_unchanged(session, protected_before)
    load_project(session, project.id)
    _assert_protected_rows_unchanged(session, protected_before)
    settings = Settings()
    assert not settings.native_reader_token_layer and not settings.pdfium_render_worker and not settings.reader_page_inventory


def test_native_minutes_and_unselected_configuration_refuse_before_pipeline_work(session, project, matrix_source, tmp_path, monkeypatch):
    document = _document(session, project, matrix_source, doc_type="minutes")
    def forbidden(*args, **kwargs):
        raise AssertionError("excluded scope must not read, render or call a model")
    monkeypatch.setattr("corridor.native_pipeline.read_reader_page_inventories", forbidden)
    result = run_native_matrix_shadow(
        session, document, source_path=matrix_source.path, scope=_scope(matrix_source),
        client=_client(matrix_source), plan=_plan(source=matrix_source), output_dir=tmp_path / "excluded",
        document_label=matrix_source.path.name,
    )
    record = pipeline_receipt(result.observation)
    assert record["disposition"] == "excluded" and record["metrics"]["client_calls"] == 0
    assert "native Minutes" in record["outcome"]["reason"]
    assert session.scalar(select(func.count()).select_from(Fact)) == 0
    with pytest.raises(PipelineQualificationRefused, match="no enabled"):
        run_selected_native_matrix(session, document, deployment="test-fixture", client=_client(matrix_source),
            plan=_plan(), source_path=matrix_source.path, output_dir=tmp_path / "unselected",
            document_label=matrix_source.path.name)
    assert not (tmp_path / "unselected").exists()


def _gate_fixture(session, project, source, tmp_path, *, purpose="synthetic_validation", mode="synthetic"):
    """Hand-authored source and metric fixtures; not claimed as measured production evidence."""
    document = _document(session, project, source)
    scope = _scope(source, purpose=purpose)
    contract = tmp_path / "synthetic-metric-contract.json"
    contract.write_text(canonical_text({"fixture": "predeclared synthetic proof", "measured_only": ["latency", "memory", "processing_cost"],
                                      "correct_over_total_minimum": 1.0}))
    contract_sha = sha256(contract.read_bytes()).hexdigest()
    criteria = tuple(MetricRequirement(name=name, contract=name, contract_sha256=contract_sha,
        rule="measured" if name in {"latency", "memory", "processing_cost"} else "minimum_ratio",
        limit=None if name in {"latency", "memory", "processing_cost"} else 1.0)
        for name in ("paired_corpus", "pdf_gold", "render_geometry", "raster_source_effects", "latency", "memory", "processing_cost"))
    policy = QualificationPolicy(scope_sha256=scope.identity, name="Synthetic rule fixtures only", criteria=criteria)
    register_qualification_policy(session, policy, actor=ACTOR, contract_paths=[contract])
    configuration = register_pipeline_configuration(session, {"fixture": "explicit source-authored gate contract", "qualification_policy_sha256": policy.identity})
    # This expected field/physical location is authored independently of an
    # extractor/proposal. The separate end-to-end test exercises the real data.
    geometry = {"crop_box": [0, 0, 900, 480], "media_box": [0, 0, 900, 480],
                "width": 900, "height": 480, "rotation": 0, "box_convention": "PDF bottom-left"}
    segment = {"source_sha256": document.sha256, "kind": "pdf_cell", "page": 1,
               "exact_text": "UC-1", "content_sha256": sha256(b"UC-1").hexdigest(),
               "location": {"geometry": geometry,
                            "glyphs": [{"text": "UC-1", "display_box": [28, 138, 50, 148]}]}}
    expected = {"schema_version": "corridor.native-pipeline-output.v1", "source_sha256": document.sha256,
                "pages": [{"page": 1, "geometry": geometry, "structure": {}, "reading": {"refused": [], "is_utility_matrix": True}}],
                "rows": [{"location": {"page": 1, "table": 0, "row": 1},
                    "geometry": {"table_box": [20, 70, 870, 340], "cells": [{"row": 1, "column": 0, "row_span": 1, "column_span": 1, "box": [20, 115, 100, 160]}]},
                    "disposition": "extracted", "reason": "candidate_recorded",
                    "confidence": 1.0, "unmapped": [], "proposal": {"fields": {"utility_id": "UC-1"}},
                    "fields": [{"name": "utility_id", "text": "UC-1", "sources": {"value_source": [segment], "context": []},
                                "materialization": {"status": "materialized", "reason": "source_materialized"}}]}],
                "facts": [{"location": {"page": 1, "table": 0, "row": 1}, "field": "utility_id", "value": "UC-1",
                           "transformation": "collapse_pdf_whitespace_v1", "sources": [{"role": "value_source", "ordinal": 1, "segment": segment}]}]}
    observations = []
    for attempt in (1, 2):
        body = {"project_id": project.id, "configuration_sha256": configuration.configuration_sha256,
                "scope_sha256": scope.identity, "scope": scope.model_dump(mode="json"), "source_sha256": document.sha256,
                "scope_text": canonical_text(scope.model_dump(mode="json")),
                "plan": {**_plan(mode).model_dump(mode="json"), "qualification_policy_sha256": policy.identity},
                "disposition": "completed", "outcome": {"disposition": "completed"},
                "started_at": datetime.now(timezone.utc).isoformat(), "input_sha256": "a" * 64,
                "output": deepcopy(expected), "metrics": {"rows": 1, "latency_ms": 1}, "fixture_attempt": attempt}
        row = PipelineObservation(project_id=project.id, document_id=document.id,
            configuration_sha256=configuration.configuration_sha256, scope_sha256=scope.identity,
            receipt_text=canonical_text(body), receipt_sha256=content_digest(body))
        session.add(row)
        session.flush()
        observations.append(row)
    repeated = record_repeatability(session, observations[0].id, observations[1].id, actor=ACTOR)
    reference = {"schema": "pipeline-quality-reference-v1", "source_sha256": document.sha256,
                 "corpus_sha256": scope.corpus_sha256, "corpus_version": scope.corpus_version,
                 "origin": {"method": "synthetic_source_authored", "artifact_sha256": document.sha256,
                            "description": "Hand-authored UC-1 field and page anchor from the synthetic PDF fixture"},
                 "output": expected}
    path = tmp_path / "authored-reference.json"
    path.write_text(canonical_text(reference))
    quality = record_quality(session, observations[0].id, reference_path=path, actor=ACTOR)
    evidence = []
    for name in (criterion.name for criterion in criteria):
        raw = tmp_path / f"{name}.json"
        values = dict(name=name, contract_sha256=contract_sha, configuration_sha256=configuration.configuration_sha256,
            scope_sha256=scope.identity, observation_sha256s=[observations[0].receipt_sha256], value=1.0, denominator=1,
            method="Hand-authored rule fixture; not real qualification data", measurement="measured")
        raw.write_text(canonical_text({"fixture": "gate rule test", "measurement": values}))
        evidence.append((MeasuredEvidence(**values, artifact_sha256=sha256(raw.read_bytes()).hexdigest()), raw))
    return document, scope, observations, repeated, quality, evidence


def _qualify(session, fixture):
    _, _, observations, repeated, quality, evidence = fixture
    return record_qualification(session, observation_ids=[observations[0].id],
        repeatability_ids=[repeated.id], quality_ids=[quality.id], evidence=evidence, actor=ACTOR)


def test_synthetic_gate_selection_disable_restore_and_stale_actor_have_no_record_side_effects(session, project, matrix_source, tmp_path, preexisting_pipeline_state):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    protected_before = _protected_rows(session)
    document, scope, *_ = fixture
    gate = _qualify(session, fixture)
    assert gate.status == "passed"
    first = select_qualified_pipeline(session, gate.id, actor=ACTOR, reason="Explicit synthetic fixture selection", expected_selection_id=None)
    _assert_protected_rows_unchanged(session, protected_before, new_selection_ids=(first.id,))
    protected_after_first = _protected_rows(session)
    assert selected_pipeline_configuration(session, document, deployment=scope.deployment, configuration_sha256=gate.configuration_sha256) == scope
    with pytest.raises(PipelineQualificationRefused, match="predecessor"):
        select_qualified_pipeline(session, gate.id, actor=ACTOR, reason="Stale act", expected_selection_id=None)
    with pytest.raises(PipelineQualificationRefused):
        selected_pipeline_configuration(session, document, deployment="different-deployment", configuration_sha256=gate.configuration_sha256)
    with pytest.raises(PipelineQualificationRefused, match="exact source"):
        selected_pipeline_configuration(session, document, deployment=scope.deployment, configuration_sha256="e" * 64)
    disabled = select_qualified_pipeline(session, gate.id, actor=ACTOR, reason="Explicit rollback to intact incumbent routing", expected_selection_id=first.id, enabled=False)
    _assert_protected_rows_unchanged(session, protected_after_first, new_selection_ids=(disabled.id,))
    protected_after_disable = _protected_rows(session)
    with pytest.raises(PipelineQualificationRefused, match="no enabled"):
        selected_pipeline_configuration(session, document, deployment=scope.deployment, configuration_sha256=gate.configuration_sha256)
    restored = select_qualified_pipeline(session, gate.id, actor=ACTOR, reason="Restore earlier qualified configuration", expected_selection_id=disabled.id)
    assert restored.configuration_sha256 == first.configuration_sha256
    _assert_protected_rows_unchanged(session, protected_after_disable, new_selection_ids=(restored.id,))
    _assert_protected_rows_unchanged(session, protected_before,
                                     new_selection_ids=(first.id, disabled.id, restored.id))


def test_replay_and_unbound_or_estimated_cost_cannot_pass_production_gate(session, project, matrix_source, tmp_path):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path, purpose="prospective_production", mode="retained_replay")
    gate = _qualify(session, fixture)
    assert gate.status == "incomplete"
    assert any("fresh_model_observation" in value for value in pipeline_receipt(gate)["missing"])
    fixture[-1][0] = (fixture[-1][0][0].model_copy(update={"scope_sha256": "f" * 64}), fixture[-1][0][1])
    with pytest.raises(PipelineQualificationRefused, match="not bound"):
        _qualify(session, fixture)


def test_pipeline_receipts_and_selection_are_immutable_and_runtime_cannot_select(session, project, matrix_source, tmp_path):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    gate = _qualify(session, fixture)
    for model in (PipelineObservation, PipelineComparison, PipelineQualification):
        with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
            session.execute(text(f"delete from {model.__tablename__}"))
    with session.begin_nested():
        session.execute(text("set local role corridor_worker"))
        with pytest.raises(PipelineQualificationRefused, match="maintenance"):
            select_qualified_pipeline(session, gate.id, actor=ACTOR, reason="Unprivileged", expected_selection_id=None)
        assert session.scalar(text("select has_table_privilege(current_user, 'pipeline_selections', 'INSERT')")) is False
        assert session.scalar(text("select has_table_privilege(current_user, 'pipeline_qualifications', 'INSERT')")) is False
        session.execute(text("reset role"))


def test_exact_replay_checks_new_image_bytes_before_reusing_answer(matrix_source, tmp_path):
    client = _client(matrix_source)
    wrong = tmp_path / "different.png"
    wrong.write_bytes(b"changed pixels")
    with pytest.raises(ValueError, match="exact retained request"):
        client.complete(system=semantics.PROMPT_PATH.read_text(),
            user=semantics.listing(slim_page(matrix_source.reading.pages[0]), matrix_source.path.name),
            schema=semantics.STRUCTURE_SCHEMA, images=[wrong])
    assert client.consumed == 0


@pytest.mark.parametrize("mode,permission", [("retained_replay", "public"), ("synthetic", "synthetic"), ("fresh_provider", "public"), ("retained_replay", "customer")])
def test_mode_or_digest_strings_cannot_authorize_an_unverified_transport(session, project, matrix_source, tmp_path, mode, permission):
    document = _document(session, project, matrix_source)
    class Transport:
        calls = 0
        def complete(self, **kwargs):
            self.calls += 1
            raise AssertionError("no outbound authorization")
    client = Transport()
    plan = ObservationPlan(mode=mode, source_permission=permission, origin_sha256="a" * 64,
        provider_posture_sha256="b" * 64, customer_authorization_sha256="c" * 64,
        description="Digest strings and a mode label are not authorization")
    with pytest.raises(ValueError, match="authorization boundary|sealed recorded client"):
        run_native_matrix_shadow(session, document, source_path=matrix_source.path,
            scope=_scope(matrix_source), client=client, plan=plan,
            output_dir=tmp_path / "refused", document_label=matrix_source.path.name)
    assert client.calls == 0 and not (tmp_path / "refused").exists()


def test_different_pre_mapping_refusals_are_not_repeatable_empty_successes(session, project, matrix_source, tmp_path):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    original = fixture[2][0]
    others = []
    for reason in ("native_minutes_excluded", "image_region_excluded"):
        record = pipeline_receipt(original)
        record.update(output={**record["output"], "pages": [], "rows": [], "facts": []},
            disposition="refused", outcome={"disposition": "refused", "stage": "scope", "reason": reason})
        row = PipelineObservation(project_id=project.id, document_id=original.document_id,
            configuration_sha256=original.configuration_sha256, scope_sha256=original.scope_sha256,
            receipt_text=canonical_text(record), receipt_sha256=content_digest(record))
        session.add(row)
        session.flush()
        others.append(row)
    compared = pipeline_receipt(record_repeatability(session, others[0].id, others[1].id, actor="pipeline:test-recorder"))
    assert compared["comparison"]["equal"] and not compared["same_outcome"] and not compared["passed"]


def test_numeric_measurement_cannot_disagree_with_its_retained_bytes(session, project, matrix_source, tmp_path):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    item, path = fixture[-1][0]
    fixture[-1][0] = (item.model_copy(update={"value": 0.0}), path)
    with pytest.raises(PipelineQualificationRefused, match="value or denominator"):
        _qualify(session, fixture)


def test_policy_registered_after_observation_cannot_qualify_it(session, project, matrix_source, tmp_path):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    original = fixture[2][0]
    record = pipeline_receipt(original)
    record["started_at"] = "2020-01-01T00:00:00+00:00"
    row = PipelineObservation(project_id=project.id, document_id=original.document_id,
        configuration_sha256=original.configuration_sha256, scope_sha256=original.scope_sha256,
        receipt_text=canonical_text(record), receipt_sha256=content_digest(record))
    session.add(row)
    session.flush()
    gate = record_qualification(session, observation_ids=[row.id], actor="pipeline:test-recorder")
    assert gate.status == "failed" and "metric_policy_not_predeclared" in pipeline_receipt(gate)["failed"]


def test_same_reader_source_reference_is_not_independent_field_gold(session, project, matrix_source, tmp_path):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    path = tmp_path / "authored-reference.json"
    reference = json.loads(path.read_text())
    reference["origin"]["method"] = "source_authored_native_reference"
    other = tmp_path / "semi-independent.json"
    other.write_text(canonical_text(reference))
    compared = pipeline_receipt(record_quality(session, fixture[2][0].id, reference_path=other, actor="pipeline:test-recorder"))
    assert compared["passed"] and compared["source_only_authoring"]
    assert not compared["independent_reference"] and not compared["synthetic_reference"]


@pytest.mark.parametrize("actor", [":", "pipeline:", ":subject", "x:subject", "pipe line:subject"])
def test_measurement_actor_requires_a_real_namespace_and_subject(session, project, matrix_source, tmp_path, actor):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    with pytest.raises(PipelineQualificationRefused, match="actor"):
        record_repeatability(session, fixture[2][0].id, fixture[2][1].id, actor=actor)


def test_quality_refuses_file_change_between_scoring_and_registration(session, project, matrix_source, tmp_path, monkeypatch):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    path = tmp_path / "racy-reference.json"
    path.write_bytes((tmp_path / "authored-reference.json").read_bytes())
    import corridor.pipeline_qualification as qualification
    register = qualification.register_processing_artifact

    def swap(scoped, **kwargs):
        if kwargs["path"] == path:
            path.write_bytes(path.read_bytes() + b"\n")
        return register(scoped, **kwargs)

    before = session.scalar(select(func.count()).select_from(PipelineComparison))
    monkeypatch.setattr(qualification, "register_processing_artifact", swap)
    with pytest.raises(PipelineQualificationRefused, match="changed between scoring"), session.begin_nested():
        record_quality(session, fixture[2][0].id, reference_path=path, actor="pipeline:test-recorder")
    assert session.scalar(select(func.count()).select_from(PipelineComparison)) == before


def test_numeric_evidence_refuses_file_change_at_registration(session, project, matrix_source, tmp_path, monkeypatch):
    fixture = _gate_fixture(session, project, matrix_source, tmp_path)
    path = fixture[-1][0][1]
    import corridor.pipeline_qualification as qualification
    register = qualification.register_processing_artifact

    def swap(scoped, **kwargs):
        if kwargs["path"] == path:
            path.write_bytes(path.read_bytes() + b"\n")
        return register(scoped, **kwargs)

    monkeypatch.setattr(qualification, "register_processing_artifact", swap)
    with pytest.raises(PipelineQualificationRefused, match="changed between measurement"), session.begin_nested():
        _qualify(session, fixture)
    assert session.scalar(select(func.count()).select_from(PipelineQualification)) == 0
