"""A missing JSON member cannot turn SQL's NULL into selection permission."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from corridor.models import PipelineQualification, PipelineSelection
from corridor.pipeline_contracts import canonical_text, content_digest
from corridor.pipeline_qualification import pipeline_receipt
from corridor.pipeline_qualification import PipelineQualificationRefused, select_qualified_pipeline
from test_native_matrix import matrix_source, project, session
from test_native_pipeline import ACTOR, _gate_fixture, _qualify


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
