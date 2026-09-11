"""The upload/preview/confirm fallback over ordinary HTTP (#349, ADR-0058).

The app shares the test's rollback-scoped session, so a route's commit is visible
to later requests in the same test and discarded at teardown. The content-addressed
store is redirected to a temp directory so staging never touches the real corpus.
"""

from __future__ import annotations

import hashlib
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor.analytics import EventFamily, capture_events
from corridor.config import settings
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
def project(member_project):
    return member_project(TEST_PRINCIPAL)


def _fingerprint(project, body, doc_type, filename):
    return source_intake._binding_fingerprint(
        project.id, hashlib.sha256(body).hexdigest(), doc_type, filename
    )


def _delivery_id(preview_text: str) -> str:
    """The delivery the preview says these bytes arrived on (#823)."""

    match = re.search(r'name="source_delivery_id" value="(\d+)"', preview_text)
    assert match, preview_text
    return match.group(1)


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
    # Nothing is registered yet -- and since #841 that is what the register
    # says, rather than the absence it used to show. The delivery is on the
    # ledger the moment the bytes are stored, and the row names the person who
    # still has to admit it.
    register = client.get(f"/projects/{project.slug}/sources")
    assert "matrix.pdf" in register.text
    assert (
        "Received and stored, and nobody has confirmed it for processing yet"
        in register.text
    )
    # No registered source, so nothing on the row opens an original.
    assert "/original" not in register.text


def test_confirm_registers_and_lists_the_upload(client, session, project, store):
    body = _matrix_pdf()
    preview = client.post(
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
            "source_delivery_id": _delivery_id(preview.text),
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
    # The request registered the source and did not read it (#893): rendering
    # and parsing belong to the standing pass, and `pending` is the state that
    # pass selects on.
    assert document.parse_status == "pending"
    assert document.pages == 0

    listing = client.get(f"/projects/{project.slug}/sources")
    assert listing.status_code == 200
    assert "matrix.pdf" in listing.text
    assert "Pending — waiting for the processing pass" in listing.text


def test_the_register_shows_a_refused_delivery_and_the_owner_of_it(
    client, project, store
):
    """The refusal is a row on the page, not the absence it used to be (#841).

    The request is still answered 400 -- the upload was refused -- and the
    delivery the gate recorded survives that answer, which is the whole reason
    ADR-0089 wanted a ledger.
    """

    refused = client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={"upload": ("hostile.pdf", b"not really a pdf", "application/pdf")},
    )
    assert refused.status_code == 400

    register = client.get(f"/projects/{project.slug}/sources")
    assert register.status_code == 200
    assert "hostile.pdf" in register.text
    assert "Refused at intake and not processed" in register.text
    # The Issue section's own owner words, so the two screens agree about who
    # puts a mechanical failure right (#840).
    assert "You, on this page" in register.text


def test_the_register_filters_and_pages_over_the_deliveries_it_holds(
    client, project, store
):
    """The three controls and the older-deliveries cursor, over the real route."""

    staged = {}
    for marker in ("alpha", "beta"):
        preview = client.post(
            f"/projects/{project.slug}/sources/upload",
            data={"doc_type": "matrix"},
            files={
                "upload": (f"{marker}.pdf", _matrix_pdf(marker), "application/pdf")
            },
        )
        assert preview.status_code == 200
        staged[marker] = _delivery_id(preview.text)

    everything = client.get(f"/projects/{project.slug}/sources")
    assert "alpha.pdf" in everything.text and "beta.pdf" in everything.text

    by_family = client.get(
        f"/projects/{project.slug}/sources", params={"family": "alpha"}
    )
    assert "alpha.pdf" in by_family.text and "beta.pdf" not in by_family.text

    by_state = client.get(
        f"/projects/{project.slug}/sources",
        params={"state": "awaiting_confirmation"},
    )
    assert "alpha.pdf" in by_state.text and "beta.pdf" in by_state.text
    assert "No delivery matches this filter." in client.get(
        f"/projects/{project.slug}/sources", params={"state": "processed"}
    ).text

    # The newest delivery is the one listed first, so the page after it holds
    # the older one and not itself.
    older = client.get(
        f"/projects/{project.slug}/sources",
        params={"before": f"delivery:{staged['beta']}"},
    )
    assert older.status_code == 200
    assert "alpha.pdf" in older.text and "beta.pdf" not in older.text


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
            "source_delivery_id": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_confirm_refuses_a_cross_project_binding(client, session, project, store):
    """A source previewed for one project cannot be confirmed into another.

    The bytes are taken the way `/sources/upload` takes them, by the same call,
    instead of through an upload request: taking delivery is all that request
    contributes here — `preview_intake` writes nothing — and the confirm is the
    act under test. That keeps the test to one request, which is what a
    transaction holding one project-authorization scope allows (#657, #662),
    and keeps the confirm's writes real, so "nothing was registered" is read
    from the database rather than from a rolled-back request.

    The delivery named is the real one `project` took (#823), so what is
    refused is the binding and not a delivery the confirm could not find.
    """
    other = Project(slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    # A member of `other` still cannot confirm a source previewed for `project`
    # into it: the binding mismatch is refused after the access gate passes.
    seed_membership(session, other, TEST_PRINCIPAL)

    body = _matrix_pdf()
    received = source_intake.receive_upload(
        session,
        project=project,
        body=body,
        filename="matrix.pdf",
        principal=TEST_PRINCIPAL,
        customer=settings.customer_id,
    )

    # Confirm the source previewed for `project` against `other`.
    r = client.post(
        f"/projects/{other.slug}/sources/confirm",
        data={
            "sha256": hashlib.sha256(body).hexdigest(),
            "filename": "matrix.pdf",
            "doc_type": "matrix",
            "binding_fingerprint": _fingerprint(project, body, "matrix", "matrix.pdf"),
            "source_delivery_id": str(received.delivery_id),
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


def test_an_upload_names_its_project_and_delivery_when_a_source_arrives(
    client, project, store
):
    """What an upload's arrival records now carry.

    The observation was once all this channel had, because a human upload could
    not be a row in the delivery ledger at all, and it was emitted with no
    customer and no project — which made an uploaded source invisible in every
    per-project reading of what arrived. #823 gives the channel a delivery, so
    there are two observations of one arrival and they say different things:
    the channel staged the exact bytes, and the ledger recorded the delivery
    they arrived on. Only the second can name a delivery, and it does.
    """

    body = _matrix_pdf()
    with capture_events() as collector:
        response = client.post(
            f"/projects/{project.slug}/sources/upload",
            data={"doc_type": "matrix"},
            files={"upload": ("matrix.pdf", body, "application/pdf")},
        )

    assert response.status_code == 200
    arrivals = [
        event
        for event in collector.events
        if event.family == EventFamily.SOURCE_ARRIVAL
    ]
    assert [event.payload["outcome"] for event in arrivals] == ["staged", "recorded"]
    for arrival in arrivals:
        assert arrival.payload["project_id"] == project.id
        assert arrival.payload["customer_id"] == settings.customer_id
        assert arrival.payload["channel"] == source_intake.PRODUCT_UPLOAD_CHANNEL
        assert (
            arrival.payload["content_sha256"] == hashlib.sha256(body).hexdigest()
        )
    assert arrivals[0].payload["source_delivery_id"] is None
    assert arrivals[1].payload["source_delivery_id"] == int(
        _delivery_id(response.text)
    )
    assert arrivals[1].payload["disposition"] == "stored"


# --- A later revision of the registered workbook (#825) ----------------------
#
# On an adopted project the same confirmation establishes what the bytes cannot
# say: which registered source this is, which revision, whether it lists every
# current row, and how it stands to what already arrived. These tests drive
# that over HTTP, because the whole point of the ticket is that those answers
# stopped being keyword arguments only a developer could set.


def _adopted(session, project, tmp_path):
    """Adopt one synthetic baseline so the project reads its later revisions."""

    from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes

    adopt(
        session,
        project,
        workbook_bytes(tmp_path / "baseline.xlsx", BASELINE_ROWS),
        tmp_path,
    )


def _later_workbook(tmp_path):
    from later_revision_support import BASELINE_ROWS, HEADINGS, workbook_bytes

    rows = [list(row) for row in BASELINE_ROWS]
    rows[0][HEADINGS.index("Size")] = "18 in"
    return workbook_bytes(tmp_path / "later.xlsx", rows)


def test_the_preview_asks_only_what_registration_cannot_answer(
    client, session, project, store, tmp_path
):
    _adopted(session, project, tmp_path)
    body = _later_workbook(tmp_path)

    preview = client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={
            "upload": (
                "ucm-later.xlsx",
                body,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert preview.status_code == 200
    # Answered from what the project registered, and shown rather than asked.
    assert "Which registered source is this a revision of?" in preview.text
    assert "Does this file use the registered field mapping?" in preview.text
    # Asked, because nothing can answer them for the person.
    assert 'name="completeness"' in preview.text
    assert 'name="revision_relationship"' in preview.text
    assert 'name="revision_identity"' in preview.text


def test_confirming_records_the_declaration_beside_the_delivery(
    client, session, project, store, tmp_path
):
    from corridor.models import SourceRevisionDeclaration
    from corridor.source_revision_declaration import (
        COMPLETE_ENUMERATION,
        FROM_DECLARATION,
        FROM_REGISTRATION,
        REPLACES,
    )

    _adopted(session, project, tmp_path)
    body = _later_workbook(tmp_path)
    digest = hashlib.sha256(body).hexdigest()
    preview = client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={
            "upload": (
                "ucm-later.xlsx",
                body,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    confirm = client.post(
        f"/projects/{project.slug}/sources/confirm",
        data={
            "sha256": digest,
            "filename": "ucm-later.xlsx",
            "doc_type": "matrix",
            "binding_fingerprint": _fingerprint(
                project, body, "matrix", "ucm-later.xlsx"
            ),
            "source_delivery_id": _delivery_id(preview.text),
            "revision_identity": "UCM workbook revision D",
            "completeness": COMPLETE_ENUMERATION,
            "revision_relationship": REPLACES,
        },
        follow_redirects=False,
    )

    assert confirm.status_code == 303
    declaration = session.scalars(
        select(SourceRevisionDeclaration).where(
            SourceRevisionDeclaration.project_id == project.id
        )
    ).one()
    assert declaration.delivery_id == int(_delivery_id(preview.text))
    assert declaration.revision_identity == "UCM workbook revision D"
    assert declaration.completeness == COMPLETE_ENUMERATION
    assert declaration.revision_relationship == REPLACES
    assert declaration.declared_by_principal == TEST_PRINCIPAL.subject
    assert declaration.answer_sources_json["source_family"] == FROM_REGISTRATION
    assert declaration.answer_sources_json["completeness"] == FROM_DECLARATION


def test_an_ordinary_upload_on_a_legacy_project_asks_nothing_extra(
    client, project, store
):
    body = _matrix_pdf()

    preview = client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": "matrix"},
        files={"upload": ("matrix.pdf", body, "application/pdf")},
    )

    assert preview.status_code == 200
    assert 'name="completeness"' not in preview.text
    assert "Which registered source is this a revision of?" not in preview.text
