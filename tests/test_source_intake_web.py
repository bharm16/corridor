"""The upload/preview/confirm fallback over ordinary HTTP (#349, ADR-0058).

The app shares the test's rollback-scoped session, so a route's commit is visible
to later requests in the same test and discarded at teardown. The content-addressed
store is redirected to a temp directory so staging never touches the real corpus.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor.config import settings
from corridor.db import Session, engine
from corridor.models import Document, Project
import corridor.source_intake as source_intake
from corridor.web.app import app, get_human_principal, get_session

from access_support import seed_membership
from pdf_fixture_support import PdfFixture

TEST_PRINCIPAL = source_intake.HumanPrincipal("local:web-uploader")


def _matrix_pdf(marker: str = "AT&T Texas (SWBT)") -> bytes:
    fixture = PdfFixture()
    page = fixture.add_page()
    page.text((72, 100), "Utility Conflict Matrix — segment 3C2")
    page.text(
        (72, 130),
        f"FOC1-1  {marker}  Telecom  underground fiber  STA 1149+00 to 1153+17",
    )
    return fixture.tobytes()


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
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_without_session(session):
    # No get_human_principal override: an anonymous request (no session cookie)
    # is refused before any handler runs (#331).
    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    p = Project(slug=f"web-intake-{uuid4().hex[:8]}", name="Web Intake", is_synthetic=True)
    session.add(p)
    session.flush()
    seed_membership(session, p, TEST_PRINCIPAL)
    return p


def _fingerprint(project, body, doc_type, filename):
    return source_intake._binding_fingerprint(
        project.id, hashlib.sha256(body).hexdigest(), doc_type, filename
    )


def test_upload_form_renders_without_a_path_or_command(client, project, store):
    r = client.get(f"/projects/{project.slug}/sources/upload")
    assert r.status_code == 200
    assert "Upload a source document" in r.text


def test_upload_shows_a_read_only_preview_before_confirm(client, project, store):
    body = _matrix_pdf()
    r = client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={"upload": ("matrix.pdf", body, "application/pdf")},
    )
    assert r.status_code == 200
    assert "Confirm registration" in r.text
    assert hashlib.sha256(body).hexdigest() in r.text
    # The unresolved metadata is spelled out, not silently made authoritative.
    assert "supersession" in r.text.lower()
    assert _fingerprint(project, body, "matrix", "matrix.pdf") in r.text
    # Nothing is registered yet.
    assert (
        client.get(f"/projects/{project.slug}/sources").text.count("matrix.pdf") == 0
    )


def test_confirm_registers_and_lists_the_upload(client, session, project, store):
    body = _matrix_pdf()
    client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={"upload": ("matrix.pdf", body, "application/pdf")},
    )
    confirm = client.post(
        f"/projects/{project.slug}/sources/confirm",
        data={
            "sha256": hashlib.sha256(body).hexdigest(),
            "filename": "matrix.pdf",
            "doc_type": "matrix",
            "binding_fingerprint": _fingerprint(project, body, "matrix", "matrix.pdf"),
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303

    document = session.scalars(
        select(Document).where(
            Document.project_id == project.id,
            Document.sha256 == hashlib.sha256(body).hexdigest(),
        )
    ).first()
    assert document is not None
    assert document.doc_type == "matrix"
    assert document.parse_status == "parsed"

    listing = client.get(f"/projects/{project.slug}/sources")
    assert listing.status_code == 200
    assert "matrix.pdf" in listing.text
    assert "Pending" in listing.text


def test_upload_refuses_an_unsupported_type_with_no_registration(
    client, session, project, store
):
    r = client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={"upload": ("notes.txt", b"just some notes", "text/plain")},
    )
    assert r.status_code == 400
    assert "accepted source file" in r.text
    assert (
        session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.project_id == project.id)
        )
        == 0
    )


def test_upload_refuses_foreign_content_that_lies_about_its_suffix(
    client, project, store
):
    r = client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={"upload": ("report.pdf", b"not really a pdf", "application/pdf")},
    )
    assert r.status_code == 400
    assert "does not contain" in r.text


def test_confirm_needs_a_signed_in_session(client_without_session, project, store):
    body = _matrix_pdf()
    r = client_without_session.post(
        f"/projects/{project.slug}/sources/confirm",
        data={
            "sha256": hashlib.sha256(body).hexdigest(),
            "filename": "matrix.pdf",
            "doc_type": "matrix",
            "binding_fingerprint": _fingerprint(project, body, "matrix", "matrix.pdf"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_confirm_refuses_a_cross_project_binding(client, session, project, store):
    """A source previewed for one project cannot be confirmed into another.

    The bytes are staged the way `/sources/upload` stages them, by the same
    call, instead of through an upload request: staging is all that request
    contributes here — `preview_intake` writes nothing — and the confirm is the
    act under test. That keeps the test to one request, which is what a
    transaction holding one project-authorization scope allows (#657, #662),
    and keeps the confirm's writes real, so "nothing was registered" is read
    from the database rather than from a rolled-back request.
    """
    other = Project(slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    # A member of `other` still cannot confirm a source previewed for `project`
    # into it: the binding mismatch is refused after the access gate passes.
    seed_membership(session, other, TEST_PRINCIPAL)

    body = _matrix_pdf()
    source_intake.validate_and_stage(body, "matrix.pdf")

    # Confirm the source previewed for `project` against `other`.
    r = client.post(
        f"/projects/{other.slug}/sources/confirm",
        data={
            "sha256": hashlib.sha256(body).hexdigest(),
            "filename": "matrix.pdf",
            "doc_type": "matrix",
            "binding_fingerprint": _fingerprint(project, body, "matrix", "matrix.pdf"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 409
    # The refusal is the binding mismatch and not missing bytes: the exact
    # previewed bytes are staged and still hash to what was submitted.
    assert "no longer matches the source you previewed" in r.json()["detail"]
    # Registered into neither project — not the one it was aimed at, and not
    # the one it was previewed for.
    for owner in (other, project):
        assert (
            session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.project_id == owner.id)
            )
            == 0
        )
