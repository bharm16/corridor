"""Repeatable candidate-model comparison (#368).

One command runs a named candidate model over exactly the documents the
current model already read, scores both against the same Reference Dataset,
and emits one immutable comparison receipt. These tests drive it with an
injected candidate reader — no model is called — and check the guarantees the
ticket and ADR-0049 require: current-versus-candidate deltas, the sealed
model/prompt/configuration/reference identities on the receipt, byte-identical
reruns, a loud failure when the candidate cannot read the corpus, and that a
candidate reading never becomes a Current Production Run.
"""

import json
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from corridor.candidate_model import (
    CandidateExtractionIncomplete,
    comparison_artifact,
    main,
    render_comparison,
    run_candidate_comparison,
)
from corridor.experimental_database import ProductionDatabaseRefusal
from corridor.extraction_runs import (
    active_run_for_document,
    declare_active_run,
    record_extraction_run,
)
from corridor.extractor_lineage import injected_extractor_config
from corridor.models import Candidate, DocPage, Document, ExtractionRun, Project
from corridor.pipeline import EXTRACTED_PROPOSALS, ExtractionRoute
from corridor.principals import HumanPrincipal

DECLARER = HumanPrincipal("local:candidate-declarer")
CURRENT_MODEL = "current-vision-1"
CANDIDATE_MODEL = "candidate-vision-2"
PROMPT_VERSION = "matrix_tiered_v3"
SCHEMA_VERSION = "matrix_candidate_shape_v1"

TEST_EXPERIMENTAL_DATABASE_URL = (
    "postgresql+psycopg://corridor:corridor@localhost:5433/"
    "corridor_candidate_disposable"
)


def allow_test_experimental_database(database_url, *, session=None):
    assert database_url == TEST_EXPERIMENTAL_DATABASE_URL
    assert session is not None


@pytest.fixture
def project(session):
    p = Project(slug="candidate-test", name="Candidate Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def document(session, project):
    d = Document(
        project_id=project.id,
        sha256="c" * 64,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(d)
    session.flush()
    session.add(
        DocPage(
            document_id=d.id,
            page_no=1,
            text="FOC1-1 FOC1-2 E92 MT AT&T",
        )
    )
    session.flush()
    return d


def _candidate(document, uid, *, model, prompt_version=PROMPT_VERSION):
    return Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {"utility_id": uid, "external_org": "MT AT&T"},
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": uid,
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "confidence": 1.0,
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=prompt_version,
        model=model,
        citations_verified=True,
    )


def current_run(session, project, document, *uids):
    """A completed current-model Extraction Run (an unsealed production row)."""

    candidates = [_candidate(document, uid, model=CURRENT_MODEL) for uid in uids]
    for candidate in candidates:
        session.add(candidate)
    session.flush()
    run = record_extraction_run(
        session,
        document,
        prompt_version=PROMPT_VERSION,
        candidate_count=len(candidates),
        page_errors=0,
        candidates=tuple(candidates),
        model=CURRENT_MODEL,
        schema_version=SCHEMA_VERSION,
        allow_unsealed_legacy=True,
    )
    session.flush()
    return run


def candidate_route(*uids, model=CANDIDATE_MODEL):
    """An injected candidate reader: no model call, one sealed run per document."""

    def select_route(document):
        def extract(session, target):
            made = [_candidate(target, uid, model=model) for uid in uids]
            for candidate in made:
                session.add(candidate)
            session.flush()
            return made

        return ExtractionRoute(
            output=EXTRACTED_PROPOSALS,
            effective_prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            extract=extract,
            model=model,
            extractor_config=injected_extractor_config(
                extractor="candidate-matrix-fixture",
                prompt_version=PROMPT_VERSION,
                model=model,
                schema_version=SCHEMA_VERSION,
                prompt_bytes=b"candidate matrix fixture",
                schema={"type": "object"},
                postprocessor_bytes=b"candidate matrix fixture rules",
                request_controls={"strict": True},
            ),
        )

    return select_route


def failing_candidate_route():
    from corridor.extraction_errors import ExtractionFailed

    def select_route(document):
        def extract(session, target):
            raise ExtractionFailed("candidate reader could not read the matrix")

        return ExtractionRoute(
            output=EXTRACTED_PROPOSALS,
            effective_prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            extract=extract,
            model=CANDIDATE_MODEL,
            extractor_config=injected_extractor_config(
                extractor="candidate-matrix-fixture",
                prompt_version=PROMPT_VERSION,
                model=CANDIDATE_MODEL,
                schema_version=SCHEMA_VERSION,
                prompt_bytes=b"candidate matrix fixture",
                schema={"type": "object"},
                postprocessor_bytes=b"candidate matrix fixture rules",
                request_controls={"strict": True},
            ),
        )

    return select_route


def _gold(tmp_path):
    reference = tmp_path / "reference.csv"
    reference.write_text("source_ref,page\nFOC1-1,1\nFOC1-2,1\nE92,1\n")
    return reference


# --------------------------------------------------------------- comparison


def test_candidate_reading_beats_current_on_the_same_reference(
    session, project, document, tmp_path
):
    current = current_run(session, project, document, "FOC1-1", "FOC1-2")
    reference = _gold(tmp_path)

    comparison = run_candidate_comparison(
        session,
        project.slug,
        current_extraction_run_ids={current.id},
        candidate_route_selector=candidate_route("FOC1-1", "FOC1-2", "E92"),
        candidate_model=CANDIDATE_MODEL,
        gold_path=str(reference),
    )

    assert comparison.current.result.recall == pytest.approx(2 / 3)
    assert comparison.candidate.result.recall == pytest.approx(1.0)
    # Same corpus, same reference file: the two readings are comparable.
    assert (
        comparison.current.reference_scope.sha256
        == comparison.candidate.reference_scope.sha256
    )
    block = comparison_artifact(
        comparison, ran_at=datetime(2026, 8, 29, tzinfo=timezone.utc)
    )["comparison"]
    assert "recall" in block["improvements"]
    assert block["regressions"] == []
    assert block["metrics"]["recall"]["delta"] == pytest.approx(1 / 3)
    assert "recall" in render_comparison(comparison)


def test_receipt_records_both_models_prompts_configs_and_reference(
    session, project, document, tmp_path
):
    current = current_run(session, project, document, "FOC1-1", "FOC1-2")
    reference = _gold(tmp_path)

    comparison = run_candidate_comparison(
        session,
        project.slug,
        current_extraction_run_ids={current.id},
        candidate_route_selector=candidate_route("FOC1-1", "FOC1-2", "E92"),
        candidate_model=CANDIDATE_MODEL,
        gold_path=str(reference),
    )
    written = comparison_artifact(
        comparison, ran_at=datetime(2026, 8, 29, tzinfo=timezone.utc)
    )

    assert written["candidate_model"] == CANDIDATE_MODEL
    [current_config] = written["current_configuration"]
    [candidate_config] = written["candidate_configuration"]
    assert current_config["model"] == CURRENT_MODEL
    assert candidate_config["model"] == CANDIDATE_MODEL
    assert candidate_config["prompt_version"] == PROMPT_VERSION
    # The candidate ran through the sealed route, so its exact configuration
    # identity is recorded; a bare model name is not enough to reproduce it.
    assert candidate_config["extractor_config_sha256"] is not None
    # Each side carries its model and reference identity through its embedded
    # measurement artifact.
    assert written["current"]["models"] == {CURRENT_MODEL: 2}
    assert written["candidate"]["reference_scope"]["kind"] == "external_reference"
    assert (
        written["current"]["reference_scope"]["sha256"]
        == written["candidate"]["reference_scope"]["sha256"]
    )
    assert len(written["comparison_identity"]) == 64


def test_comparison_identity_is_stable_across_runs_ignoring_time(
    session, project, document, tmp_path
):
    current = current_run(session, project, document, "FOC1-1", "FOC1-2")
    reference = _gold(tmp_path)

    comparison = run_candidate_comparison(
        session,
        project.slug,
        current_extraction_run_ids={current.id},
        candidate_route_selector=candidate_route("FOC1-1", "FOC1-2", "E92"),
        candidate_model=CANDIDATE_MODEL,
        gold_path=str(reference),
    )
    first = comparison_artifact(
        comparison, ran_at=datetime(2026, 8, 29, tzinfo=timezone.utc)
    )
    later = comparison_artifact(
        comparison, ran_at=datetime(2027, 1, 1, tzinfo=timezone.utc)
    )
    assert first["comparison_identity"] == later["comparison_identity"]
    first.pop("ran_at")
    later.pop("ran_at")
    assert first == later


def test_a_candidate_that_cannot_read_a_document_fails_loudly(
    session, project, document, tmp_path
):
    current = current_run(session, project, document, "FOC1-1", "FOC1-2")
    reference = _gold(tmp_path)

    with pytest.raises(CandidateExtractionIncomplete) as excinfo:
        run_candidate_comparison(
            session,
            project.slug,
            current_extraction_run_ids={current.id},
            candidate_route_selector=failing_candidate_route(),
            candidate_model=CANDIDATE_MODEL,
            gold_path=str(reference),
        )
    assert "did not cleanly read" in str(excinfo.value)


def test_candidate_run_never_becomes_the_current_production_run(
    session, project, document, tmp_path
):
    current = current_run(session, project, document, "FOC1-1", "FOC1-2")
    declare_active_run(session, document.id, current.id, principal=DECLARER)
    reference = _gold(tmp_path)

    comparison = run_candidate_comparison(
        session,
        project.slug,
        current_extraction_run_ids={current.id},
        candidate_route_selector=candidate_route("FOC1-1", "FOC1-2", "E92"),
        candidate_model=CANDIDATE_MODEL,
        gold_path=str(reference),
    )

    [candidate_run_id] = comparison.candidate.extraction_run_ids
    assert candidate_run_id != current.id
    # A candidate reading is an experiment. It records an Extraction Run, but
    # production selection is untouched: the Active Run is still the current
    # reading, and the command declared nothing.
    assert active_run_for_document(session, document.id).id == current.id
    declared_candidate = session.scalar(
        select(ExtractionRun).where(
            ExtractionRun.id == candidate_run_id,
            ExtractionRun.model == CANDIDATE_MODEL,
        )
    )
    assert declared_candidate is not None
    assert active_run_for_document(session, document.id).id != candidate_run_id


# --------------------------------------------------------------------- main


class _OpenSession:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *_):
        return False


def test_command_writes_an_immutable_comparison_receipt(
    session, project, document, tmp_path
):
    current = current_run(session, project, document, "FOC1-1", "FOC1-2")
    reference = _gold(tmp_path)

    def route_factory(model_name):
        assert model_name == CANDIDATE_MODEL
        return nullcontext(candidate_route("FOC1-1", "FOC1-2", "E92"))

    argv = [
        project.slug,
        str(reference),
        f"--candidate-model={CANDIDATE_MODEL}",
        f"--database-url={TEST_EXPERIMENTAL_DATABASE_URL}",
        f"--current-extraction-run={current.id}",
    ]
    status = main(
        argv,
        session_factory=lambda: _OpenSession(session),
        database_guard=allow_test_experimental_database,
        candidate_route_factory=route_factory,
        output_dir=tmp_path,
        ran_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    assert status == 0
    artifacts = list(tmp_path.glob("candidate-comparison-candidate-test-*.json"))
    assert len(artifacts) == 1
    written = json.loads(artifacts[0].read_text())
    assert written["candidate_model"] == CANDIDATE_MODEL
    assert written["comparison_identity"][:16] in artifacts[0].name

    first_bytes = artifacts[0].read_bytes()
    assert (
        main(
            argv,
            session_factory=lambda: _OpenSession(session),
            database_guard=allow_test_experimental_database,
            candidate_route_factory=route_factory,
            output_dir=tmp_path,
            ran_at=datetime(2027, 3, 3, tzinfo=timezone.utc),
        )
        == 0
    )
    assert artifacts[0].read_bytes() == first_bytes


def test_command_refuses_the_production_database(
    session, project, document, tmp_path, capsys
):
    current = current_run(session, project, document, "FOC1-1", "FOC1-2")
    reference = _gold(tmp_path)

    def refuse(database_url, *, session=None):
        raise ProductionDatabaseRefusal(
            "experimental command refused the configured production database"
        )

    status = main(
        [
            project.slug,
            str(reference),
            f"--candidate-model={CANDIDATE_MODEL}",
            f"--database-url={TEST_EXPERIMENTAL_DATABASE_URL}",
            f"--current-extraction-run={current.id}",
        ],
        session_factory=lambda: _OpenSession(session),
        database_guard=refuse,
        candidate_route_factory=lambda name: nullcontext(candidate_route()),
        output_dir=tmp_path,
    )
    assert status == 1
    assert "refused the configured production database" in capsys.readouterr().err
    assert not list(tmp_path.glob("candidate-comparison-*.json"))


def test_command_requires_a_candidate_model_and_a_database(capsys):
    assert main(["candidate-test", "--current-extraction-run=1"]) == 2
    assert "--candidate-model" in capsys.readouterr().err
    assert main(
        ["candidate-test", "--candidate-model=x", "--current-extraction-run=1"]
    ) == 2
    assert "--database-url" in capsys.readouterr().err
    assert main(
        [
            "candidate-test",
            "--candidate-model=x",
            f"--database-url={TEST_EXPERIMENTAL_DATABASE_URL}",
        ]
    ) == 2
    assert "--current-extraction-run" in capsys.readouterr().err
