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


def test_acceptance_cli_records_the_maintainers_own_words_and_selects_on_them(
    runtime_database, matrix_source, tmp_path, capsys,
):
    """ADR-0095's two explicit acts: record the acceptance, then select on it."""
    from corridor.models import PipelineAcceptance, PipelineQualification
    from corridor.native_pipeline import register_pipeline_configuration
    from corridor.pipeline_contracts import canonical_text
    from test_native_pipeline import _document, _scope

    with runtime_database.session_factory() as session, session.begin():
        project = Project(slug="synthetic-cli-acceptance", name="CLI acceptance fixture", is_synthetic=True)
        session.add(project)
        session.flush()
        _document(session, project, matrix_source)
        scope = _scope(matrix_source)
        configuration = register_pipeline_configuration(
            session, {"fixture": "cli accepted configuration", "code_revision": "a" * 40})
        slug, configuration_sha256 = project.slug, configuration.configuration_sha256
    document = tmp_path / "acceptance.json"
    document.write_text(canonical_text({
        "acceptance": {
            "decision": "ADR-0095", "configuration_sha256": configuration_sha256,
            "implementation_revision": "a" * 40, "scope_sha256": scope.identity,
            "evidence": [{"name": "paired-rendition exact gate",
                          "reference": "docs/adr/0095-the-maintainer-accepts-the-pdf-replacement-on-the-evidence-already-measured.md",
                          "summary": "262 of 263 development pairs and 70 of 70 holdout pairs."}],
            "limits": ["The references share a reader, so independent full-field accuracy is not established."],
            "words": "I accept the replacement on the evidence already measured.",
            "accepted_on": "2026-09-08",
        },
        "scope": scope.model_dump(mode="json"),
    }))
    assert main(["accept", "--acceptance", str(document), "--project", slug,
                 "--actor", "local:test-maintainer"],
                session_factory=runtime_database.session_factory) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["basis"] == "maintainer_acceptance" and "status" not in recorded
    # The next act needs this row's identity, so the command prints it.
    assert isinstance(recorded["acceptance_id"], int)
    assert recorded["actor"] == "local:test-maintainer"
    assert recorded["words"].startswith("I accept the replacement")
    assert recorded["limits"] and recorded["evidence"][0]["name"] == "paired-rendition exact gate"
    identity = recorded["acceptance_id"]
    with runtime_database.session_factory() as session:
        assert session.scalar(select(PipelineAcceptance.id)) == identity
        assert session.scalar(select(func.count()).select_from(PipelineQualification)) == 0
    assert main(["select", "--acceptance", str(identity), "--initial",
                 "--actor", "local:test-maintainer",
                 "--reason", "Accepted on the evidence already measured (ADR-0095)"],
                session_factory=runtime_database.session_factory) == 0
    selection = json.loads(capsys.readouterr().out)
    assert selection["basis"] == "maintainer_acceptance" and selection["acceptance_id"] == identity
    assert selection["qualification_id"] is None
    # A later rollback names this selection as its expected predecessor.
    assert isinstance(selection["selection_id"], int)
    with runtime_database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PipelineSelection)) == 1
        assert session.scalar(select(func.count()).select_from(ActiveExtractionRun)) == 0
        assert session.scalar(select(func.count()).select_from(RecordInclusionRequest)) == 0


@pytest.mark.parametrize("argv,expected", [
    (["select", "--initial", "--actor", "local:test-maintainer", "--reason", "none"],
     "--qualification QUALIFICATION | --acceptance ACCEPTANCE"),
    (["select", "--qualification", "1", "--acceptance", "2", "--initial",
      "--actor", "local:test-maintainer", "--reason", "both"],
     "argument --acceptance: not allowed with argument --qualification"),
])
def test_selection_cli_stands_on_exactly_one_named_basis(capsys, argv, expected):
    with pytest.raises(SystemExit) as refused:
        main(argv)
    assert refused.value.code == 2
    assert expected in capsys.readouterr().err


def test_accept_can_register_the_configuration_it_names_but_never_a_different_one(
    runtime_database, matrix_source, tmp_path, capsys,
):
    """A fresh database holds no configuration row until a run or this file puts one there."""
    from corridor.models import PipelineAcceptance, PipelineConfiguration
    from corridor.pipeline_contracts import canonical_text, content_digest
    from test_native_pipeline import _document, _scope

    configuration = {"fixture": "cli registered configuration", "code_revision": "a" * 40}
    with runtime_database.session_factory() as session, session.begin():
        project = Project(slug="synthetic-cli-registration", name="CLI registration fixture", is_synthetic=True)
        session.add(project)
        session.flush()
        _document(session, project, matrix_source)
        slug = project.slug
    scope = _scope(matrix_source)
    configuration_file = tmp_path / "configuration.json"
    configuration_file.write_text(canonical_text(configuration))
    document = tmp_path / "registered-acceptance.json"
    document.write_text(canonical_text({
        "acceptance": {
            "decision": "ADR-0095", "configuration_sha256": content_digest(configuration),
            "implementation_revision": "a" * 40, "scope_sha256": scope.identity,
            "evidence": [{"name": "integrated replay", "reference": "gold/native-matrix/v1/dataset.json",
                          "summary": "466 required rows reproduced twice."}],
            "limits": ["Independent full-field accuracy is not established."],
            "words": "I accept the replacement on the evidence already measured.",
            "accepted_on": "2026-09-08",
        },
        "scope": scope.model_dump(mode="json"),
    }))
    without = ["accept", "--acceptance", str(document), "--project", slug, "--actor", "local:test-maintainer"]
    with pytest.raises(Exception, match="registered configuration bytes"):
        main(without, session_factory=runtime_database.session_factory)
    other = tmp_path / "other-configuration.json"
    other.write_text(canonical_text({"fixture": "a different configuration", "code_revision": "a" * 40}))
    with pytest.raises(ValueError, match="not the configuration this acceptance names"):
        main([*without, "--configuration", str(other)], session_factory=runtime_database.session_factory)
    with runtime_database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PipelineAcceptance)) == 0
    assert main([*without, "--configuration", str(configuration_file)],
                session_factory=runtime_database.session_factory) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["configuration_sha256"] == content_digest(configuration)
    with runtime_database.session_factory() as session:
        assert session.get(PipelineConfiguration, content_digest(configuration)) is not None


def test_the_prepared_acceptance_document_is_complete_but_for_its_run_bound_identities():
    """The maintainer's file states everything ADR-0095 requires of him.

    Only the configuration digest and the implementation revision come from the
    run he is accepting, and until he supplies them the command refuses rather
    than recording a placeholder.
    """
    from pathlib import Path

    from pydantic import ValidationError

    from corridor.pipeline_contracts import MaintainerAcceptance, PipelineScope

    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "docs/operations/native-matrix-maintainer-acceptance.json").read_bytes())
    with pytest.raises(ValidationError):
        MaintainerAcceptance.model_validate(document["acceptance"])
    scope = PipelineScope.model_validate(document["scope"])
    acceptance = MaintainerAcceptance.model_validate({
        **document["acceptance"], "configuration_sha256": scope.identity,
        "implementation_revision": "0" * 40, "scope_sha256": scope.identity,
    })
    assert acceptance.decision == "ADR-0095" and acceptance.words.strip()
    assert len(acceptance.evidence) >= 4 and all(item.reference.strip() for item in acceptance.evidence)
    assert len(acceptance.limits) >= 4
    assert scope.source_class == "native_matrix" and len(scope.source_sha256s) == 7
