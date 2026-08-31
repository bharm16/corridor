"""The draft->preview->confirm interface over ordinary HTTP (#362).

Every test drives the real product intake surface against real PostgreSQL with a
fake model adapter and declared coordination spend authority. The draft is an
explicit, read-only act that registers nothing; confirmation stays the ordinary
``/sources/confirm`` act, reconstructed independently from server-bound inputs.
The app shares the test's rollback-scoped session, and the content-addressed
store is redirected to a temp directory so staging never touches the real corpus.
"""

from __future__ import annotations

import hashlib
import io
import re

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select

from corridor import access
from corridor.config import settings
from corridor.db import Session, engine
from corridor.models import Document, Project, SourceIntakeDraftRequest
import corridor.source_intake as source_intake
from corridor.principals import HumanPrincipal
from corridor.web.app import (
    app,
    get_human_principal,
    get_intake_draft_client_factory,
    get_session,
)

from access_support import seed_membership

CURATOR = HumanPrincipal("local:web-curator")

_COVER_ROWS = (
    "Utility Conflict Matrix Rev 3",
    "Registry number: UCM-REV-3",
    "Document date: 2024-03-01",
    "This revision supersedes UCM-REV-2 effective 2024-03-01.",
)


def _cover_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Cover"
    for index, value in enumerate(_COVER_ROWS, start=1):
        sheet[f"A{index}"] = value
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _valid_result():
    return {
        "metadata_suggestions": [
            {
                "field": "doc_date",
                "value": "2024-03-01",
                "source_ref": "P1",
                "source_quote": "Document date: 2024-03-01",
                "basis": "The cover states the document's date.",
            }
        ],
        "replacement_proposals": [
            {
                "predecessor_registry_id": "UCM-REV-2",
                "effective_date": "2024-03-01",
                "source_ref": "P1",
                "source_quote": (
                    "This revision supersedes UCM-REV-2 effective 2024-03-01."
                ),
                "basis": "The revision states what it replaces.",
            }
        ],
        "uncertainties": [],
    }


class FakeAdapter:
    adapter = "fake-intake-draft"
    adapter_contract_version = "fake-adapter-v1"

    def __init__(self, result=None):
        self._result = result
        self.calls: list[dict] = []

    def complete(self, *, system, user, schema):
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self._result


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


@pytest.fixture
def adapter_box():
    return {"adapter": FakeAdapter(result=_valid_result())}


@pytest.fixture
def client(session, adapter_box):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: CURATOR
    app.dependency_overrides[get_intake_draft_client_factory] = lambda: (
        lambda configuration: adapter_box["adapter"]
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    row = Project(slug="draft-web", name="Draft Web", is_synthetic=True)
    session.add(row)
    session.flush()
    seed_membership(session, row, CURATOR)
    return row


def _register_predecessor(session, project):
    document = Document(
        project_id=project.id,
        registry_id="UCM-REV-2",
        sha256="a" * 64,
        filename="UCM-REV-2.xlsx",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    return document


def _declare_config(client, project):
    return client.post(
        f"/projects/{project.slug}/sources/draft-configuration",
        data={
            "model": "fake-model",
            "prompt_version": "source_intake_draft_v1",
            "max_input_tokens": "50000",
            "max_output_tokens": "2000",
            "timeout_seconds": "30",
            "max_requests": "1",
            "retry_policy": "none",
            "retention_policy": "class_b_30_days",
            "observation_context": "internal_working_view",
        },
        follow_redirects=False,
    )


def _preview(client, project, body, *, doc_type="matrix", filename="cover.xlsx"):
    return client.post(
        f"/projects/{project.slug}/sources/upload",
        data={"doc_type": doc_type},
        files={
            "upload": (
                filename,
                body,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


def _state_token(preview_text: str) -> str:
    match = re.search(r'name="state_token" value="([a-f0-9]{64})"', preview_text)
    assert match, preview_text
    return match.group(1)


def _request_draft(client, project, body, filename="cover.xlsx"):
    preview = _preview(client, project, body, filename=filename)
    assert "Draft suggestions" in preview.text
    return client.post(
        f"/projects/{project.slug}/sources/draft",
        data={
            "sha256": hashlib.sha256(body).hexdigest(),
            "filename": filename,
            "doc_type": "matrix",
            "state_token": _state_token(preview.text),
            "permitted_pages": "",
        },
        follow_redirects=False,
    )


def _draft_count(session, project) -> int:
    return session.scalar(
        select(func.count(SourceIntakeDraftRequest.id)).where(
            SourceIntakeDraftRequest.project_id == project.id
        )
    )


def _document_count(session, project) -> int:
    return session.scalar(
        select(func.count(Document.id)).where(Document.project_id == project.id)
    )


# --- the complete interface --------------------------------------------------


def test_preview_offers_a_draft_only_when_spend_is_declared(client, project, store):
    body = _cover_xlsx()

    before = _preview(client, project, body)
    assert "No bounded intake-draft spend authority" in before.text
    assert "Draft suggestions" not in before.text

    assert _declare_config(client, project).status_code == 303
    after = _preview(client, project, body)
    assert "Draft suggestions" in after.text


def test_draft_shows_source_backed_suggestions_and_registers_nothing(
    client, session, project, store, adapter_box
):
    _register_predecessor(session, project)
    _declare_config(client, project)
    body = _cover_xlsx()

    response = _request_draft(client, project, body)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text

    assert "register nothing" in page.lower() or "registers nothing" in page.lower()
    assert "Document date: 2024-03-01" in page
    assert "UCM-REV-2" in page
    assert len(adapter_box["adapter"].calls) == 1

    receipt = session.scalars(
        select(SourceIntakeDraftRequest).where(
            SourceIntakeDraftRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "completed"
    assert receipt.non_authoritative is True
    # Only the predecessor is registered; the drafted source is not.
    assert _document_count(session, project) == 1
    assert (
        session.scalar(
            select(func.count(Document.id)).where(
                Document.project_id == project.id,
                Document.sha256 == hashlib.sha256(body).hexdigest(),
            )
        )
        == 0
    )


def test_confirm_registers_independently_after_a_draft(
    client, session, project, store
):
    _register_predecessor(session, project)
    _declare_config(client, project)
    body = _cover_xlsx()
    sha = hashlib.sha256(body).hexdigest()

    assert _request_draft(client, project, body).status_code == 303
    # The ordinary confirmation reconstructs its binding from server inputs and
    # never trusts the draft.
    confirm = client.post(
        f"/projects/{project.slug}/sources/confirm",
        data={
            "sha256": sha,
            "filename": "cover.xlsx",
            "doc_type": "matrix",
            "binding_fingerprint": source_intake._binding_fingerprint(
                project.id, sha, "matrix", "cover.xlsx"
            ),
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303

    document = session.scalars(
        select(Document).where(
            Document.project_id == project.id, Document.sha256 == sha
        )
    ).one()
    # Registered as an ordinary new document — the draft preselected no
    # Supersession and set no registry metadata.
    assert document.doc_type == "matrix"
    assert document.superseded_by is None
    assert document.registry_id is None


def test_hostile_draft_is_refused_without_registering(
    client, session, project, store, adapter_box
):
    _declare_config(client, project)
    fabricated = _valid_result()
    fabricated["replacement_proposals"] = []
    fabricated["metadata_suggestions"] = [
        {
            "field": "doc_date",
            "value": "1999-01-01",
            "source_ref": "P1",
            "source_quote": "Document date: 1999-01-01",  # never on the page
            "basis": "An invented reading.",
        }
    ]
    adapter_box["adapter"] = FakeAdapter(result=fabricated)
    body = _cover_xlsx()

    response = _request_draft(client, project, body)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "No suggestions were shown" in page

    receipt = session.scalars(
        select(SourceIntakeDraftRequest).where(
            SourceIntakeDraftRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "validation_refused"
    assert _document_count(session, project) == 0


def test_stale_bytes_refuse_the_draft_over_http(client, project, store, adapter_box):
    _declare_config(client, project)
    body = _cover_xlsx()
    preview = _preview(client, project, body)
    token = _state_token(preview.text)

    response = client.post(
        f"/projects/{project.slug}/sources/draft",
        data={
            # A sha the person never staged: refused before any model call.
            "sha256": "b" * 64,
            "filename": "cover.xlsx",
            "doc_type": "matrix",
            "state_token": token,
            "permitted_pages": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert adapter_box["adapter"].calls == []


# --- access ------------------------------------------------------------------


def test_draft_surface_requires_project_coordination(
    client, session, project, store
):
    seed_membership(
        session, project, CURATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    body = _cover_xlsx()

    assert _declare_config(client, project).status_code == 403
    assert (
        client.post(
            f"/projects/{project.slug}/sources/draft",
            data={
                "sha256": hashlib.sha256(body).hexdigest(),
                "filename": "cover.xlsx",
                "doc_type": "matrix",
                "state_token": "0" * 64,
                "permitted_pages": "",
            },
        ).status_code
        == 403
    )
