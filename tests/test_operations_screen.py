"""The technical-operations screen joins existing bounded operations (#344)."""

from datetime import datetime, timezone
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor import access
from corridor.extraction_runs import record_extraction_run
from corridor.due_work import ProjectProcessingDeclaration, configure_project_processing
from corridor.models import ActiveRunDeclaration, Document, Project
from corridor.principals import HumanPrincipal
from access_support import seed_membership


OPERATOR = HumanPrincipal("local:operations")
COORDINATOR = HumanPrincipal("local:coordinator")


@pytest.fixture
def client(session):
    from corridor.web.app import app, get_human_principal, get_session

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: OPERATOR
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    row = Project(slug="operations-screen", name="Operations Screen", is_synthetic=True)
    session.add(row)
    session.flush()
    seed_membership(
        session, row, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    return row


def _document_with_completed_run(session, project):
    document = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="utility-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    run = record_extraction_run(
        session,
        document,
        prompt_version="operations-screen-v1",
        candidate_count=0,
        page_errors=0,
        model="test-model",
        allow_unsealed_legacy=True,
    )
    session.flush()
    return document, run


def _state_fingerprint(body: str) -> str:
    match = re.search(r'name="state_fingerprint" value="([a-f0-9]{64})"', body)
    assert match, body
    return match.group(1)


def test_operations_screen_is_technical_operator_only_and_shows_disabled_schedule(
    client, session, project
):
    body = client.get(f"/operations/{project.slug}").text
    assert "Processing operations" in body
    assert "Scheduled recovery is not enabled" in body
    assert "No applicable proof" in body

    seed_membership(
        session, project, OPERATOR, designations=(access.COORDINATION,)
    )
    assert client.get(f"/operations/{project.slug}").status_code == 403


def test_operator_declares_exact_completed_run_and_uses_session_identity(
    client, session, project
):
    document, run = _document_with_completed_run(session, project)

    offered = client.get(f"/operations/{project.slug}")
    response = client.post(
        f"/operations/{project.slug}/runs/{document.id}/declare",
        data={
            "extraction_run_id": str(run.id),
            "state_fingerprint": _state_fingerprint(offered.text),
            "declared_by": "local:forged",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    declaration = session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == document.id
        )
    ).one()
    assert declaration.extraction_run_id == run.id
    assert declaration.declared_by == OPERATOR.subject
    refreshed = client.get(f"/operations/{project.slug}").text
    assert "Record Inclusion is pending" in refreshed
    assert "utility-matrix.pdf" in refreshed

    # A refreshed page or double click reaches the idempotent shared writer;
    # it cannot append a second declaration or another downstream request.
    duplicate = client.post(
        f"/operations/{project.slug}/runs/{document.id}/declare",
        data={
            "extraction_run_id": str(run.id),
            "state_fingerprint": _state_fingerprint(offered.text),
        },
        follow_redirects=False,
    )
    assert duplicate.status_code == 409
    assert session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == document.id
        )
    ).all() == [declaration]


def test_operations_screen_shows_only_a_retained_gate7_processing_schedule(
    client, session, project
):
    declaration = ProjectProcessingDeclaration.released_hourly(
        project_id=project.id,
        configuration_version="operations-screen-v1",
        extractor_identity="deployed-matrix-v1",
        starts_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    configure_project_processing(
        session, declaration, now=datetime(2026, 8, 30, tzinfo=timezone.utc)
    )

    body = client.get(f"/operations/{project.slug}").text
    assert "project processing" in body
    assert "is enabled" in body
    assert "at most 3 attempts" in body
    assert "declared model budget 2000000" in body


def test_operations_mutation_rechecks_document_scope(client, session, project):
    other = Project(slug="foreign-operations", name="Foreign", is_synthetic=True)
    session.add(other)
    session.flush()
    document, run = _document_with_completed_run(session, other)

    response = client.post(
        f"/operations/{project.slug}/runs/{document.id}/declare",
        data={
            "extraction_run_id": str(run.id),
        },
    )
    assert response.status_code == 404
    assert session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == document.id
        )
    ).all() == []
