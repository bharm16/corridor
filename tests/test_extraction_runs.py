"""Extraction run lineage and Active Run declaration contracts."""

import pytest
from sqlalchemy import select, update

from corridor.db import Session, engine
from corridor.extraction_runs import record_extraction_run
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Document,
    ExtractionRun,
    Project,
)

extraction_runs = __import__("corridor.extraction_runs", fromlist=["*"])

PROMPT_VERSION = "test_v1"


def _run_id(run):
    return run.id if hasattr(run, "id") else run


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def project(session):
    p = Project(slug="run-lineage-test", name="Run Lineage Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def add_matrix(session, project, name, sha):
    doc = Document(
        project_id=project.id,
        sha256=sha,
        filename=name,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    return doc


def test_active_run_helpers_are_explicit_contracts():
    assert hasattr(
        extraction_runs, "declare_active_run"
    ), "Active Runs must be declared, not inferred"
    assert callable(getattr(extraction_runs, "declare_active_run"))
    assert hasattr(extraction_runs, "active_run_for_document")
    assert callable(getattr(extraction_runs, "active_run_for_document"))


def test_active_run_declared_by_explicit_run_id(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    first = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
    )
    record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.v2",
        candidate_count=1,
        page_errors=0,
    )
    session.flush()

    extraction_runs.declare_active_run(session, doc.id, first.id)
    assert _run_id(extraction_runs.active_run_for_document(session, doc.id)) == first.id

    # A newer successful run is not automatically active.
    assert _run_id(extraction_runs.active_run_for_document(session, doc.id)) == first.id


def test_newer_runs_do_not_imply_active_run(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    active = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.v1",
        candidate_count=1,
        page_errors=0,
    )
    session.flush()
    extraction_runs.declare_active_run(session, doc.id, active.id)

    _ = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.v2-experimental",
        candidate_count=2,
        page_errors=0,
    )
    _ = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.v2-backfill",
        candidate_count=0,
        page_errors=0,
    )
    session.flush()

    assert _run_id(extraction_runs.active_run_for_document(session, doc.id)) == active.id


def test_active_run_declaration_refreshes_a_prewarmed_identity_map(session, project):
    doc = add_matrix(session, project, "prewarmed.pdf", "b" * 64)
    first = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.first",
        candidate_count=0,
        page_errors=0,
    )
    second = record_extraction_run(
        session,
        doc,
        prompt_version=f"{PROMPT_VERSION}.second",
        candidate_count=0,
        page_errors=0,
    )
    extraction_runs.declare_active_run(session, doc.id, first.id)
    stale = session.get(ActiveExtractionRun, doc.id)

    session.execute(
        update(ActiveExtractionRun)
        .where(ActiveExtractionRun.document_id == doc.id)
        .values(extraction_run_id=second.id)
        .execution_options(synchronize_session=False)
    )
    assert stale.extraction_run_id == first.id

    extraction_runs.declare_active_run(session, doc.id, first.id)
    session.expire(stale)
    assert stale.extraction_run_id == first.id


def test_run_receipt_carries_provenance_and_owns_its_candidates(session, project):
    doc = add_matrix(session, project, "a.pdf", "c" * 64)
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
        source_document_id=doc.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model="test-model",
        citations_verified=True,
    )
    session.add(candidate)

    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
        schema_version="dependency-schema-v3",
    )
    session.flush()

    assert candidate.extraction_run_id == run.id
    assert (run.prompt_version, run.model, run.schema_version, run.outcome) == (
        PROMPT_VERSION,
        "test-model",
        "dependency-schema-v3",
        "completed",
    )


def test_failed_or_foreign_runs_cannot_be_declared_active(session, project):
    first = add_matrix(session, project, "a.pdf", "d" * 64)
    second = add_matrix(session, project, "b.pdf", "e" * 64)
    failed = record_extraction_run(
        session,
        first,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=1,
        outcome="failed",
        error_detail="upstream unavailable",
    )
    completed = record_extraction_run(
        session,
        second,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
    )
    session.flush()

    with pytest.raises(ValueError, match="only a completed"):
        extraction_runs.declare_active_run(session, first.id, failed.id)
    with pytest.raises(ValueError, match="does not belong"):
        extraction_runs.declare_active_run(session, first.id, completed.id)


def test_operator_entrypoint_declares_the_exact_active_run(
    session, project, capsys
):
    doc = add_matrix(session, project, "operator.pdf", "f" * 64)
    run = record_extraction_run(
        session,
        doc,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
    )
    session.flush()
    document_id = doc.id
    extraction_run_id = run.id

    class ScopedSession:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            session.close()
            return False

    assert extraction_runs.main(
        [str(document_id), str(extraction_run_id)], session_factory=ScopedSession
    ) == 0
    assert (
        _run_id(extraction_runs.active_run_for_document(session, document_id))
        == extraction_run_id
    )
    assert f"Active Run {extraction_run_id}" in capsys.readouterr().out


def test_null_or_ambiguous_lineage_is_not_active(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    session.add(
        Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
            source_document_id=doc.id,
            source_pages=[1],
            confidence=1.0,
            citations_verified=True,
        )
    )
    session.flush()

    assert extraction_runs.active_run_for_document(session, doc.id) is None
    runs = session.scalars(
        select(ExtractionRun).where(ExtractionRun.document_id == doc.id)
    ).all()
    assert runs == []
