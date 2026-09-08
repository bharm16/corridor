"""A missing JSON member cannot turn SQL's NULL into selection permission."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from corridor.models import PipelineAcceptance, PipelineQualification, PipelineSelection
from corridor.pipeline_contracts import canonical_text, content_digest
from corridor.pipeline_qualification import pipeline_receipt, record_acceptance
from corridor.pipeline_qualification import PipelineQualificationRefused, select_qualified_pipeline
from test_native_matrix import matrix_source, project, session
from test_native_pipeline import ACTOR, _acceptance_fixture, _gate_fixture, _qualify


@pytest.mark.parametrize("omitted", ["missing", "failed", "deployment"])
def test_direct_selection_refuses_incomplete_gate_json(session, project, matrix_source, tmp_path, omitted):
    gate = _qualify(session, _gate_fixture(session, project, matrix_source, tmp_path))
    gate_body = pipeline_receipt(gate)
    scope = dict(gate_body["scope"])
    if omitted == "deployment":
        del gate_body["scope"][omitted]
    else:
        del gate_body[omitted]
    with pytest.raises(DBAPIError), session.begin_nested():
        malformed = PipelineQualification(project_id=project.id, configuration_sha256=gate.configuration_sha256,
            scope_sha256=gate.scope_sha256, status="passed", receipt_text=canonical_text(gate_body),
            receipt_sha256=content_digest(gate_body))
        session.add(malformed)
        session.flush()
        record = {"schema": "pipeline-selection-v1", "project_id": project.id,
                  "configuration_sha256": gate.configuration_sha256, "scope_sha256": gate.scope_sha256,
                  "qualification_id": malformed.id, "qualification_sha256": malformed.receipt_sha256,
                  "scope": scope, "previous_selection_id": None, "actor": ACTOR.subject,
                  "reason": "Malformed gate must refuse", "enabled": True,
                  "selected_at": datetime.now(timezone.utc).isoformat()}
        selection = PipelineSelection(project_id=project.id, configuration_sha256=gate.configuration_sha256,
            scope_sha256=gate.scope_sha256, deployment=scope["deployment"], qualification_id=malformed.id,
            previous_selection_id=None, actor=ACTOR.subject, reason=record["reason"], enabled=True,
            receipt_text=canonical_text(record), receipt_sha256=content_digest(record))
        session.add(selection)
        session.flush()
    assert session.scalar(select(func.count()).select_from(PipelineSelection)) == 0


def test_selection_reader_rejects_expanded_scope_with_the_old_digest(session, project, matrix_source, tmp_path):
    gate = _qualify(session, _gate_fixture(session, project, matrix_source, tmp_path))
    selected = select_qualified_pipeline(session, gate.id, actor=ACTOR, reason="Synthetic selection", expected_selection_id=None)
    record = pipeline_receipt(selected)
    record["scope"]["source_sha256s"] = sorted([*record["scope"]["source_sha256s"], "0" * 64])
    # Simulate reading a historical malformed row. Its old scope digest cannot
    # authorize these new source bytes even if the enclosing receipt rehashes.
    detached = PipelineSelection(project_id=selected.project_id,
        configuration_sha256=selected.configuration_sha256, scope_sha256=selected.scope_sha256,
        receipt_text=canonical_text(record), receipt_sha256=content_digest(record))
    with pytest.raises(PipelineQualificationRefused, match="scope differs"):
        pipeline_receipt(detached)


def test_sql_selection_scope_must_equal_the_qualified_scope(session, project, matrix_source, tmp_path):
    gate = _qualify(session, _gate_fixture(session, project, matrix_source, tmp_path))
    original = pipeline_receipt(gate)
    expanded = dict(original["scope"])
    expanded["source_sha256s"] = sorted([*expanded["source_sha256s"], "0" * 64])
    record = {"schema": "pipeline-selection-v1", "project_id": project.id,
              "configuration_sha256": gate.configuration_sha256, "scope_sha256": gate.scope_sha256,
              "qualification_id": gate.id, "qualification_sha256": gate.receipt_sha256,
              "scope": expanded, "scope_text": canonical_text(expanded), "previous_selection_id": None,
              "actor": ACTOR.subject, "reason": "Cannot expand the qualified allowlist", "enabled": True}
    with pytest.raises(DBAPIError, match="scope"), session.begin_nested():
        selection = PipelineSelection(project_id=project.id, configuration_sha256=gate.configuration_sha256,
            scope_sha256=gate.scope_sha256, deployment=expanded["deployment"], qualification_id=gate.id,
            previous_selection_id=None, actor=ACTOR.subject, reason=record["reason"], enabled=True,
            receipt_text=canonical_text(record), receipt_sha256=content_digest(record))
        session.add(selection)
        session.flush()


def _acceptance_body(project, configuration_sha256, scope, **overrides):
    body = {
        "schema": "pipeline-acceptance-v1", "basis": "maintainer_acceptance",
        "decision": "ADR-0095", "project_id": project.id,
        "configuration_sha256": configuration_sha256, "implementation_revision": "a" * 40,
        "scope_sha256": scope.identity, "scope": scope.model_dump(mode="json"),
        "scope_text": canonical_text(scope.model_dump(mode="json")),
        "evidence": [{"name": "integrated replay", "reference": "artifacts/pipeline-qualification/",
                      "summary": "466 required rows reproduced twice."}],
        "limits": ["Shared-reader references establish no independent full-field accuracy."],
        "words": "I accept this on the evidence already measured.", "actor": ACTOR.subject,
        "accepted_on": "2026-09-08", "accepted_at": "2026-09-08T00:00:00+00:00",
    }
    body.update(overrides)
    return body


def _insert_acceptance(session, project, configuration_sha256, body, *, actor=None):
    row = PipelineAcceptance(
        project_id=project.id, configuration_sha256=configuration_sha256,
        scope_sha256=body["scope_sha256"], implementation_revision=body["implementation_revision"],
        actor=actor or body["actor"], receipt_text=canonical_text(body),
        receipt_sha256=content_digest(body))
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize("overrides", [
    {"status": "passed"},
    {"missing": []},
    {"failed": []},
    {"basis": "qualification"},
    {"schema": "pipeline-qualification-v1"},
    {"limits": []},
    {"limits": ["   "]},
    {"evidence": []},
    {"evidence": [{"name": "unreachable", "reference": "", "summary": "no way to follow it"}]},
    {"words": "   "},
    {"decision": ""},
    {"accepted_at": "2026-09-08"},
    {"actor": "local:system"},
    {"implementation_revision": "not-a-revision"},
])
def test_sql_refuses_an_acceptance_that_wears_a_gate_or_omits_its_own_fields(
    session, project, matrix_source, overrides,
):
    _, scope, acceptance = _acceptance_fixture(session, project, matrix_source)
    body = _acceptance_body(project, acceptance.configuration_sha256, scope, **overrides)
    before = session.scalar(select(func.count()).select_from(PipelineAcceptance))
    with pytest.raises(DBAPIError), session.begin_nested():
        _insert_acceptance(session, project, acceptance.configuration_sha256, body,
                           actor=body.get("actor"))
    assert session.scalar(select(func.count()).select_from(PipelineAcceptance)) == before


def _selection_row(project, basis_row, scope, record, **columns):
    return PipelineSelection(
        project_id=project.id, configuration_sha256=basis_row.configuration_sha256,
        scope_sha256=basis_row.scope_sha256, deployment=scope.deployment,
        previous_selection_id=None, actor=ACTOR.subject, reason=record["reason"],
        enabled=True, receipt_text=canonical_text(record), receipt_sha256=content_digest(record),
        **columns)


def _selection_record(project, basis_row, scope, **overrides):
    record = {"schema": "pipeline-selection-v1", "project_id": project.id,
              "configuration_sha256": basis_row.configuration_sha256,
              "scope_sha256": basis_row.scope_sha256, "basis": "maintainer_acceptance",
              "acceptance_id": basis_row.id, "acceptance_sha256": basis_row.receipt_sha256,
              "qualification_id": None, "qualification_sha256": None,
              "scope": scope.model_dump(mode="json"),
              "scope_text": canonical_text(scope.model_dump(mode="json")),
              "previous_selection_id": None, "actor": ACTOR.subject,
              "reason": "Direct SQL selection", "enabled": True}
    record.update(overrides)
    return record


@pytest.mark.parametrize("break_it", ["both_bases", "no_basis", "wrong_digest", "expanded_scope", "gate_basis_label"])
def test_sql_refuses_an_acceptance_selection_that_is_not_the_acceptance_it_names(
    session, project, matrix_source, tmp_path, break_it,
):
    gate = _qualify(session, _gate_fixture(session, project, matrix_source, tmp_path)) if break_it == "both_bases" else None
    _, scope, acceptance = _acceptance_fixture(session, project, matrix_source)
    accepted = record_acceptance(session, acceptance, project_id=project.id, scope=scope, actor=ACTOR)
    columns = {"acceptance_id": accepted.id, "qualification_id": None}
    record = _selection_record(project, accepted, scope)
    if break_it == "both_bases":
        columns = {"acceptance_id": accepted.id, "qualification_id": gate.id}
        record["qualification_id"] = gate.id
        record["qualification_sha256"] = gate.receipt_sha256
    elif break_it == "no_basis":
        columns = {"acceptance_id": None, "qualification_id": None}
        record["acceptance_id"] = None
        record["acceptance_sha256"] = None
    elif break_it == "wrong_digest":
        record["acceptance_sha256"] = "0" * 64
    elif break_it == "expanded_scope":
        widened = dict(scope.model_dump(mode="json"))
        widened["source_sha256s"] = sorted([*widened["source_sha256s"], "0" * 64])
        record["scope"] = widened
        record["scope_text"] = canonical_text(widened)
    else:
        record["basis"] = "qualification"
    before = session.scalar(select(func.count()).select_from(PipelineSelection))
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(_selection_row(project, accepted, scope, record, **columns))
        session.flush()
    assert session.scalar(select(func.count()).select_from(PipelineSelection)) == before
