from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.adjudicate import accept_candidate, merge_candidate
from corridor.db import Session, engine
from corridor.ledger import browse, load_dependency, primary_evidence
from corridor.models import (
    Candidate,
    DependencyEvent,
    Document,
    EvidenceLink,
    Project,
)
from corridor.web.app import app, get_session

FIELDS = {
    "utility_id": "FOC1-1",
    "external_org": "LT AT&T Texas",
    "utility_type": "Telecom",
    "station_from": "1149+00",
    "station_to": "1153+17",
}


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
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    p = Project(slug="ledger-test", name="Ledger Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def document(session, project):
    d = Document(
        project_id=project.id,
        sha256="f" * 64,
        filename="nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=28,
        doc_date=date(2026, 2, 13),
    )
    session.add(d)
    session.flush()
    return d


def make_candidate(session, project, document, *, fields=None, verified=True):
    c = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields or FIELDS,
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "FOC1-1 LT AT&T Texas Telecom",
                    "verified": verified,
                }
            ],
            "confidence": 1.0,
            "dedupe_hint": "x",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=verified,
    )
    session.add(c)
    session.flush()
    return c


@pytest.fixture
def dependency(session, project, document):
    return accept_candidate(
        session, make_candidate(session, project, document), actor="tester"
    )


# ------------------------------------------------------------------- browse


def test_browse_lists_dependencies_with_their_backing(session, project, dependency):
    [row] = browse(session, project.id)
    assert row.dependency.id == dependency.id
    assert row.org_name == "LT AT&T Texas"
    assert row.assertion_count == len(FIELDS)
    assert row.evidence_count == 1
    assert row.is_ready is False
    assert row.contradicted is False


def test_browse_filters_by_status(session, project, dependency):
    assert len(browse(session, project.id, status="identified")) == 1
    assert browse(session, project.id, status="closed") == []


def test_browse_filters_by_readiness(session, project, dependency):
    """Readiness is computed, so this filter cannot be a WHERE clause."""
    assert browse(session, project.id, ready=True) == []
    assert len(browse(session, project.id, ready=False)) == 1

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    link.satisfies_requirement = True
    session.flush()

    assert len(browse(session, project.id, ready=True)) == 1
    assert browse(session, project.id, ready=False) == []


def test_browse_flags_contradicted_records(session, project, document, dependency):
    merge_candidate(
        session,
        make_candidate(
            session, project, document, fields={**FIELDS, "station_from": "1160+00"}
        ),
        dependency,
        actor="tester",
    )
    [row] = browse(session, project.id)
    assert row.contradicted is True


def test_an_unverified_disagreement_is_not_a_contradiction(
    session, project, document, dependency
):
    """A bad citation is a bad citation, not evidence that sources disagree."""
    merge_candidate(
        session,
        make_candidate(
            session,
            project,
            document,
            fields={**FIELDS, "station_from": "9999+00"},
            verified=False,
        ),
        dependency,
        actor="tester",
    )
    [row] = browse(session, project.id)
    assert row.contradicted is False


# ------------------------------------------------------------------- detail


def test_the_detail_view_carries_events_and_audit(session, project, dependency):
    session.add(
        DependencyEvent(
            dependency_id=dependency.id,
            event_type="slip",
            event_date=date(2026, 3, 4),
            description="AT&T moved relocation from June to August",
            created_by="tester",
        )
    )
    session.flush()

    view = load_dependency(session, dependency.id)
    assert [e.event_type for e in view.events] == ["slip"]
    # Acceptance already wrote one audit entry.
    assert any(a.action == "accept_candidate" for a in view.audit)


# ---------------------------------------------------------------------- web


def test_the_ledger_page_renders_rows(client, project, dependency):
    r = client.get(f"/ledger/{project.slug}")
    assert r.status_code == 200
    assert dependency.ref_code in r.text
    assert "LT AT&amp;T Texas" in r.text


def test_the_detail_page_shows_each_claim_with_its_source(
    client, project, dependency
):
    r = client.get(f"/ledger/{project.slug}/{dependency.id}")
    assert r.status_code == 200
    assert "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf" in r.text
    assert "1149+00" in r.text
    assert "not ready" in r.text


def test_marking_evidence_as_closing_makes_it_ready(
    client, session, project, dependency
):
    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    r = client.post(
        f"/dependencies/{dependency.id}/evidence/{link.id}/satisfies",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert load_dependency(session, dependency.id).is_ready is True


def test_unverified_evidence_cannot_be_marked_as_closing(
    client, session, project, document
):
    """ADR-0002: readiness cannot rest on a quote that is not on the page."""
    dep = accept_candidate(
        session,
        make_candidate(session, project, document, verified=False),
        actor="tester",
    )
    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dep.id)
    ).one()
    r = client.post(
        f"/dependencies/{dep.id}/evidence/{link.id}/satisfies",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert load_dependency(session, dep.id).is_ready is False


def test_ledger_rows_carry_their_exceptions(session, project, dependency):
    [row] = browse(session, project.id)
    rules = {e.rule for e in row.exceptions}
    # No milestone linked and no committed date on a fresh matrix record.
    assert "ORPHAN" in rules
    assert "MISSING_DATE" in rules


def test_the_ledger_can_be_filtered_to_one_rule(session, project, dependency):
    assert len(browse(session, project.id, rule="ORPHAN")) == 1
    assert browse(session, project.id, rule="OVERDUE") == []


def test_the_ledger_page_shows_exception_pills(client, project, dependency):
    r = client.get(f"/ledger/{project.slug}")
    assert "ORPHAN" in r.text
    assert "any exception" in r.text


def test_the_detail_page_explains_each_exception(client, project, dependency):
    r = client.get(f"/ledger/{project.slug}/{dependency.id}")
    assert "not linked to any milestone" in r.text


def test_a_dependency_from_another_project_is_not_reachable(
    client, session, project, dependency
):
    other = Project(slug="ledger-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    r = client.get(f"/ledger/{other.slug}/{dependency.id}")
    assert r.status_code == 404


# --------------------------------------------------------- primary evidence


def test_primary_evidence_is_the_first_verified_link(
    session, project, document, dependency
):
    """One definition of "the quote this cell cites".

    The report and the export each held their own copy of this query, so
    the rule had two implementations that happened to agree.
    """
    found = primary_evidence(session, [dependency.id])

    evidence = found[dependency.id]
    assert evidence.document_id == document.id
    assert evidence.filename == document.filename
    assert evidence.page_no == 1
    assert evidence.quote == "FOC1-1 LT AT&T Texas Telecom"


def test_primary_evidence_skips_an_unverified_link(session, project, document):
    """An unverified quote is a bad citation, not a citable one."""
    unverified = accept_candidate(
        session,
        make_candidate(session, project, document, verified=False),
        actor="tester",
    )

    assert primary_evidence(session, [unverified.id]) == {}


def test_primary_evidence_answers_for_many_dependencies_at_once(
    session, project, document, dependency
):
    """Batched: the export used to ask once per exported row."""
    other = accept_candidate(
        session,
        make_candidate(
            session, project, document, fields={**FIELDS, "utility_id": "FOC1-2"}
        ),
        actor="tester",
    )

    found = primary_evidence(session, [dependency.id, other.id])

    assert set(found) == {dependency.id, other.id}
    assert primary_evidence(session, []) == {}
