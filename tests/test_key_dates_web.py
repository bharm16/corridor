"""HTTP flow for the Key dates import preview and attributable confirm (#337)."""

import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.config import settings
from corridor.db import Session, engine
from corridor.milestones import import_xer, preview_import
from corridor.models import Milestone, MilestoneRegistration, Project
from corridor.principals import HumanPrincipal
from corridor.web.app import app, get_human_principal, get_session

TEST_PRINCIPAL = HumanPrincipal("local:test-reviewer")

CSV = "code,name,need_date\nUTIL-CLEAR,Utility clearance,2026-11-01\nLET,Letting,2027-01-20\n"


def xer_bytes():
    fields = ["task_id", "proj_id", "task_code", "task_name", "task_type", "target_end_date"]
    lines = [
        "\t".join(["ERMHDR", "19.12", "2026-08-29", "P", "a", "a", "db", "US", "USD"]),
        "\t".join(["%T", "TASK"]),
        "\t".join(["%F", *fields]),
        "\t".join(["%R", "102", "1", "UTIL-CLEAR", "Utility clearance complete", "TT_FinMile", "2026-11-01 00:00"]),
        "%E",
    ]
    return "\n".join(lines).encode("cp1252")


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
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_without_principal(session, monkeypatch):
    """Exercise the real fail-closed deployment-identity dependency."""
    monkeypatch.setattr(settings, "human_principal", "")
    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    p = Project(slug="kd-web", name="Key Dates Web", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def codes(session, project):
    return set(
        session.scalars(
            select(Milestone.code).where(Milestone.project_id == project.id)
        ).all()
    )


def _confirm_fields(session, project, content, source_name="typed.csv"):
    preview = preview_import(
        session, project_id=project.id, content=content.encode(), source_name=source_name
    )
    return {
        "source_name": source_name,
        "content": content,
        "expected_sha256": preview.source_sha256,
        "expected_predecessors": json.dumps(preview.predecessors),
    }


def test_landing_lists_registered_key_dates(client, session, project):
    import_xer(session, project_id=project.id, content=xer_bytes(), source_name="seg.xer")
    response = client.get(f"/key-dates/{project.slug}")
    assert response.status_code == 200
    assert "UTIL-CLEAR" in response.text
    assert "Utility clearance complete" in response.text


def test_preview_shows_classified_rows_and_fingerprint_without_writing(
    client, session, project
):
    response = client.post(
        f"/key-dates/{project.slug}/preview",
        data={"source_name": "typed.csv", "content": CSV},
    )
    assert response.status_code == 200
    assert "New key date" in response.text
    assert sha256(CSV.encode()).hexdigest() in response.text
    # A preview writes nothing.
    assert codes(session, project) == set()


def test_confirm_imports_under_the_configured_person(client, session, project):
    fields = _confirm_fields(session, project, CSV)
    response = client.post(
        f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/key-dates/{project.slug}?imported=")
    registrations = session.scalars(
        select(MilestoneRegistration)
        .join(Milestone, MilestoneRegistration.milestone_id == Milestone.id)
        .where(Milestone.project_id == project.id)
    ).all()
    assert {r.recorded_by for r in registrations} == {"local:test-reviewer"}
    assert codes(session, project) == {"UTIL-CLEAR", "LET"}


def test_confirm_is_unavailable_without_a_configured_principal(
    client_without_principal, session, project
):
    fields = _confirm_fields(session, project, CSV)
    response = client_without_principal.post(
        f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False
    )
    assert response.status_code == 503
    assert codes(session, project) == set()


def test_preview_of_a_malformed_sheet_refuses(client, session, project):
    response = client.post(
        f"/key-dates/{project.slug}/preview",
        data={"source_name": "bad.csv", "content": "code,name\nX,no date\n"},
    )
    assert response.status_code == 422
    assert "Not imported" in response.text
    assert codes(session, project) == set()


def test_confirm_against_changed_bytes_refuses_and_presents_new_state(
    client, session, project
):
    # Preview one file, then confirm different bytes carrying the old fingerprint.
    fields = _confirm_fields(session, project, CSV)
    changed = CSV.replace("2026-11-01", "2026-12-15")
    fields["content"] = changed
    response = client.post(
        f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False
    )
    assert response.status_code == 409
    assert "Refused" in response.text
    assert codes(session, project) == set()


def test_import_is_scoped_to_its_project(client, session, project):
    other = Project(slug="kd-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    fields = _confirm_fields(session, project, CSV)
    client.post(f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False)
    assert codes(session, project) == {"UTIL-CLEAR", "LET"}
    assert codes(session, other) == set()
