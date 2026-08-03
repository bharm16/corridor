import json

import pymupdf
import pytest

from corridor.db import Session, engine
from corridor.docs import get_page, list_documents
from corridor.models import Project
from corridor.pipeline import ingest_manifest


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
    p = Project(slug="docs-test", name="Docs Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def make_pdf(path, lines):
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, 100 + 30 * i), line)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def lockfile(tmp_path):
    good = make_pdf(
        tmp_path / "matrix.pdf",
        [
            "Utility Owner: AT&T Texas (SWBT) - Telecom - underground fiber",
            "STA 1149+00 to STA 1153+17, offset 303 L/R, crossing IH 69",
        ],
    )
    other = make_pdf(
        tmp_path / "agreement.pdf",
        ["Joint Project Agreement between TxDOT and CenterPoint Energy, 1963"],
    )
    lock = {
        "project": "docs-test",
        "sources": {
            "https://example.gov/utilities.zip::matrix.pdf": {
                "sha256": "1" * 64,
                "local_path": str(good),
                "member": "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
                "archive_url": "https://example.gov/utilities.zip",
                "doc_type": "matrix",
                "doc_date": "2026-02-13",
                "retrieved_at": "2026-08-02T20:00:00+00:00",
            },
            "https://example.gov/agreement.pdf": {
                "sha256": "2" * 64,
                "local_path": str(other),
                "doc_type": "agreement",
                "doc_date": None,
                "retrieved_at": "2026-08-02T20:00:00+00:00",
            },
            # A failed fetch: present in the lockfile, nothing on disk.
            "https://example.gov/missing.pdf": {
                "sha256": None,
                "local_path": None,
                "http_status": 404,
                "doc_type": "minutes",
            },
        },
    }
    path = tmp_path / "manifest.lock.json"
    path.write_text(json.dumps(lock))
    return path


def test_ingesting_a_manifest_loads_every_fetched_source(
    session, project, lockfile, tmp_path
):
    documents = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lockfile,
        images_dir=tmp_path / "images",
    )
    assert len(documents) == 2
    assert all(d.parse_status == "parsed" for d in documents)


def test_a_failed_fetch_is_not_ingested_as_though_it_worked(
    session, project, lockfile, tmp_path
):
    """The 404 stays visible in the lockfile rather than becoming a document."""
    documents = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lockfile,
        images_dir=tmp_path / "images",
    )
    assert "missing.pdf" not in {d.filename for d in documents}


def test_provenance_survives_the_ingest_boundary(
    session, project, lockfile, tmp_path
):
    ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lockfile,
        images_dir=tmp_path / "images",
    )
    rows = list_documents(session, project.id)
    matrix = next(r for r in rows if r.doc_type == "matrix")

    # The archive member name, not the content-addressed hash on disk.
    assert matrix.filename == "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"
    assert matrix.doc_date == "2026-02-13"
    assert matrix.source_url == "https://example.gov/utilities.zip"


def test_a_nested_member_records_only_its_leaf_path(
    session, project, tmp_path
):
    """The inner zip's own name is noise in a citation."""
    import json as _json

    pdf = make_pdf(
        tmp_path / "note.pdf",
        ["Air Liquide coordination meeting notes, biweekly cadence check-in"],
    )
    lock = {
        "project": "docs-test",
        "sources": {
            "https://example.gov/u.zip::Coordination/Notes.zip::Meeting Notes/Air Liquide/2024.07.30 notes.pdf": {
                "sha256": "9" * 64,
                "local_path": str(pdf),
                "member": "Coordination/Notes.zip::Meeting Notes/Air Liquide/2024.07.30 notes.pdf",
                "archive_url": "https://example.gov/u.zip",
                "doc_type": "minutes",
                "doc_date": "2024-07-30",
                "retrieved_at": "2026-08-03T16:00:00+00:00",
            },
        },
    }
    path = tmp_path / "nested.lock.json"
    path.write_text(_json.dumps(lock))

    [doc] = ingest_manifest(
        session, project_id=project.id, lock_path=path, images_dir=tmp_path / "i"
    )
    assert doc.filename == "Meeting Notes/Air Liquide/2024.07.30 notes.pdf"


def test_reingesting_a_manifest_is_a_noop(session, project, lockfile, tmp_path):
    first = ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    second = ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    assert [d.id for d in first] == [d.id for d in second]
    assert len(list_documents(session, project.id)) == 2


def test_listing_reports_pages_and_ocr_counts(session, project, lockfile, tmp_path):
    ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    rows = list_documents(session, project.id)
    assert all(r.pages > 0 for r in rows)
    # These have real text layers; none should be OCR.
    assert all(r.ocr_pages == 0 for r in rows)


def test_any_page_is_retrievable_with_text_and_image(
    session, project, lockfile, tmp_path
):
    """M1's bar: every file retrievable with page text and page image."""
    documents = ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    page = get_page(session, documents[0].id, 1)
    assert page.text.strip()
    assert page.image_path and open(page.image_path, "rb").read(4) == b"\x89PNG"
    assert page.text_source in ("text_layer", "ocr")


def test_asking_for_a_page_that_does_not_exist_raises(
    session, project, lockfile, tmp_path
):
    documents = ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    with pytest.raises(LookupError):
        get_page(session, documents[0].id, 999)
