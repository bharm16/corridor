"""Make/CLI surface for the managed connected-location operations (#350).

The two human acts — authorizing a proposed reference and recovering a failed
parse — plus the read-only operations view run through the same commands an
operator uses. Attribution is fail-closed on ``CORRIDOR_HUMAN_PRINCIPAL``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from corridor.config import settings
from corridor.location_discovery import effective_source_url, reference_key
from corridor.location_discovery_cli import main
from corridor.models import DiscoveredReference, Document, Project

from pdf_fixture_support import PdfFixture

NOW = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
LOCATION = "txdot-loc"


def _payload(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


@pytest.fixture
def principal(monkeypatch):
    monkeypatch.setattr(settings, "human_principal", "local:operator")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


def _project(factory) -> Project:
    with factory() as s:
        p = Project(slug=f"loc-cli-{uuid4().hex[:8]}", name="Loc CLI", is_synthetic=True)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.expunge(p)
        return p


def _pdf() -> bytes:
    fixture = PdfFixture()
    fixture.add_page().text((72, 100), "Utility Conflict Matrix")
    return fixture.tobytes()


def test_authorize_and_view_through_the_cli(runtime_database, principal, capsys):
    factory = runtime_database.session_factory
    project = _project(factory)
    url = "https://docs.example.gov/a.pdf"
    key = reference_key(LOCATION, effective_source_url(url, None))
    with factory() as s:
        s.add(
            DiscoveredReference(
                project_id=project.id, location_id=LOCATION, reference_key=key,
                source_url=url, first_observed_at=NOW, last_observed_at=NOW,
                observed_count=1, state="proposed",
            )
        )
        s.commit()

    assert main(
        ["authorize", project.slug, f"--reference-key={key}", "--doc-type=matrix",
         "--registry-id=ucm-2026-08"],
        session_factory=factory,
    ) == 0
    authorized = _payload(capsys)
    assert authorized["doc_type"] == "matrix" and authorized["registry_id"] == "ucm-2026-08"
    with factory() as s:
        row = s.scalar(select_reference(key))
        assert row.state == "authorized" and row.authorized_by == "local:operator"

    assert main(["view", project.slug], session_factory=factory) == 0
    view = _payload(capsys)
    assert [item["identity"] for item in view["authorized_pending"]] == [url]


def test_recover_parse_through_the_cli(runtime_database, principal, store, capsys):
    factory = runtime_database.session_factory
    project = _project(factory)
    body = _pdf()
    sha = hashlib.sha256(body).hexdigest()
    shard = Path(settings.corpus_store) / sha[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / f"{sha}.pdf").write_bytes(body)
    with factory() as s:
        document = Document(
            project_id=project.id, sha256=sha, filename="broken.pdf", doc_type="matrix",
            parse_status="failed", pages=0,
        )
        s.add(document)
        s.commit()
        document_id = document.id

    assert main(
        ["recover-parse", project.slug, f"--document-id={document_id}"],
        session_factory=factory,
    ) == 0
    recovered = _payload(capsys)
    assert recovered["outcome"] == "recovered" and recovered["pages"] >= 1
    with factory() as s:
        assert s.get(Document, document_id).parse_status == "parsed"


def test_the_cli_is_fail_closed_without_a_principal(runtime_database, monkeypatch, capsys):
    monkeypatch.setattr(settings, "human_principal", "")
    factory = runtime_database.session_factory
    project = _project(factory)
    assert main(
        ["authorize", project.slug, "--reference-key=" + "0" * 64, "--doc-type=matrix"],
        session_factory=factory,
    ) == 1
    assert "CORRIDOR_HUMAN_PRINCIPAL" in capsys.readouterr().err


def select_reference(key: str):
    from sqlalchemy import select

    return select(DiscoveredReference).where(DiscoveredReference.reference_key == key)
