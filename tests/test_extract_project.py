"""Extraction over a whole project, from one command.

The defect this closes is silence: a project could be fully ingested and
produce no Candidates at all, with nothing in the output saying so. So
these tests are mostly about what the *report* distinguishes, not about
extraction quality — quality is measured by a real run, not a unit test.
"""

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.geometry import NoMatrixFound
from corridor.extract_project import extract_project
from corridor.models import Candidate, Document, Project

PROMPT_VERSION = "test_v1"


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
    p = Project(slug="xp-test", name="Extract Project Test", is_synthetic=True)
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


def candidate(document, *, verified=True, state="pending"):
    return Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        citations_verified=verified,
        state=state,
    )


def extractor(**by_filename):
    """An extractor scripted per document: a list of Candidates, or an error."""

    def extract(session, document):
        result = by_filename[document.filename]
        if isinstance(result, Exception):
            raise result
        candidates = [candidate(document, verified=v) for v in result]
        for c in candidates:
            session.add(c)
        session.flush()
        return candidates

    return extract


def test_it_reports_rows_and_unverified_citations_per_document(session, project):
    a = add_matrix(session, project, "a.pdf", "a" * 64)
    b = add_matrix(session, project, "b.pdf", "b" * 64)

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True, True, False], "b.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    by_name = {o.filename: o for o in outcomes}
    assert by_name["a.pdf"].status == "extracted"
    assert (by_name["a.pdf"].rows, by_name["a.pdf"].unverified) == (3, 1)
    assert (by_name["b.pdf"].rows, by_name["b.pdf"].unverified) == (1, 0)


def test_unreadable_is_a_different_outcome_from_no_rows(session, project):
    """The distinction the whole ticket exists for.

    A document with no conflicts is a correct empty answer. A document the
    extractor cannot read is an unhandled layout. Reporting both as "0
    rows" is how a broken pipeline passes for a quiet one.
    """
    empty = add_matrix(session, project, "empty.pdf", "c" * 64)
    broken = add_matrix(session, project, "broken.pdf", "d" * 64)

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{
            "empty.pdf": [],
            "broken.pdf": NoMatrixFound("no table with utility-matrix headers"),
        }),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    by_name = {o.filename: o for o in outcomes}
    assert by_name["empty.pdf"].status == "extracted"
    assert by_name["empty.pdf"].rows == 0
    assert by_name["broken.pdf"].status == "unreadable"
    assert "utility-matrix headers" in by_name["broken.pdf"].detail


def test_a_second_run_does_not_double_the_candidates(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    extract = extractor(**{"a.pdf": [True, True]})

    extract_project(
        session, project, extract=extract, prompt_version=PROMPT_VERSION, commit=False
    )
    outcomes = extract_project(
        session, project, extract=extract, prompt_version=PROMPT_VERSION, commit=False
    )

    assert outcomes[0].status == "skipped"
    assert _count(session, doc) == 2


def test_redo_replaces_pending_candidates(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)

    extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True, True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )
    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        redo=True,
        commit=False,
    )

    assert outcomes[0].status == "extracted"
    assert _count(session, doc) == 1


def test_redo_never_disturbs_an_accepted_candidate(session, project):
    """Accepted Candidates back Ledger records, and Assertions cite them."""
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    session.add(candidate(doc, state="accepted"))
    session.add(candidate(doc, state="pending"))
    session.flush()

    extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        redo=True,
        commit=False,
    )

    states = sorted(
        c.state
        for c in session.scalars(
            select(Candidate).where(Candidate.source_document_id == doc.id)
        )
    )
    assert states == ["accepted", "pending"]


def test_a_document_ingest_could_not_parse_is_reported_not_skipped(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    doc.parse_status = "failed"
    session.flush()

    outcomes = extract_project(
        session, project, extract=extractor(), prompt_version=PROMPT_VERSION, commit=False
    )

    assert outcomes[0].status == "unreadable"
    assert "failed" in outcomes[0].detail


def test_only_matrices_are_eligible(session, project):
    add_matrix(session, project, "a.pdf", "a" * 64)
    session.add(
        Document(
            project_id=project.id,
            sha256="f" * 64,
            filename="agreement.pdf",
            doc_type="agreement",
            parse_status="parsed",
            pages=1,
        )
    )
    session.flush()

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    assert [o.filename for o in outcomes] == ["a.pdf"]


def _count(session, document) -> int:
    return len(
        session.scalars(
            select(Candidate).where(Candidate.source_document_id == document.id)
        ).all()
    )
