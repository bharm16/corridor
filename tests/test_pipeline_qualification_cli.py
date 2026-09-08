"""The explicit maintenance adapter selects only inside an injected test database."""

import json

import pytest
from sqlalchemy import func, select

from corridor.models import ActiveExtractionRun, PipelineSelection, Project, RecordInclusionRequest
from corridor.pipeline_qualification_cli import main
from test_native_matrix import matrix_source
from test_native_pipeline import _gate_fixture, _qualify


def test_selection_cli_requires_an_explicit_initial_or_predecessor_choice(capsys):
    with pytest.raises(SystemExit) as refused:
        main(["select", "--qualification", "1", "--actor", "local:test-maintainer", "--reason", "test"])
    assert refused.value.code == 2
    assert "--expected-selection --initial" in capsys.readouterr().err


def test_selection_cli_commits_only_its_synthetic_routing_receipt(runtime_database, matrix_source, tmp_path, capsys):
    with runtime_database.session_factory() as session, session.begin():
        project = Project(slug="synthetic-cli-selection", name="CLI selection fixture", is_synthetic=True)
        session.add(project)
        session.flush()
        qualification = _qualify(session, _gate_fixture(session, project, matrix_source, tmp_path))
        identity = qualification.id
    assert main(["select", "--qualification", str(identity), "--initial",
                 "--actor", "local:test-maintainer", "--reason", "Explicit synthetic CLI fixture"],
                session_factory=runtime_database.session_factory) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["actor"] == "local:test-maintainer" and receipt["enabled"] is True
    assert receipt["scope"]["purpose"] == "synthetic_validation"
    with runtime_database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PipelineSelection)) == 1
        assert session.scalar(select(func.count()).select_from(ActiveExtractionRun)) == 0
        assert session.scalar(select(func.count()).select_from(RecordInclusionRequest)) == 0
