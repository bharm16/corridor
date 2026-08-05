import json
from datetime import date

import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

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


def test_bulk_ingest_skips_locks_opted_out_of_default_materialization(
    session, tmp_path, monkeypatch, capsys
):
    import corridor.docs as docs_module

    tracked = make_pdf(
        tmp_path / "tracked.pdf",
        ["Tracked project matrix text long enough to stay above OCR fallback"],
    )
    layout = make_pdf(
        tmp_path / "layout.pdf",
        ["Layout evidence text long enough to stay above OCR fallback"],
    )
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "tracked.lock.json").write_text(
        json.dumps(
            {
                "project": "tracked-project",
                "name": "Tracked Project",
                "agency": "TxDOT",
                "ingest_by_default": True,
                "sources": {
                    "tracked": {
                        "sha256": "1" * 64,
                        "local_path": str(tracked),
                        "doc_type": "matrix",
                        "retrieved_at": "2026-08-05T00:00:00+00:00",
                    }
                },
            }
        )
    )
    (corpus_dir / "cross-agency.lock.json").write_text(
        json.dumps(
            {
                "project": "layout-evidence",
                "name": "Layout Evidence",
                "agency": "various",
                "ingest_by_default": False,
                "sources": {
                    "layout": {
                        "sha256": "2" * 64,
                        "local_path": str(layout),
                        "doc_type": "matrix",
                        "retrieved_at": "2026-08-05T00:00:00+00:00",
                    }
                },
            }
        )
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(docs_module, "SessionFactory", sessionmaker(bind=session.get_bind()))

    assert docs_module.main(["ingest"]) == 0

    out = capsys.readouterr().out
    assert "tracked-project: 1/1 parsed from tracked.lock.json" in out
    assert "layout-evidence: skipped bulk ingest (ingest_by_default is false)" in out
    assert "1 documents total" in out

    projects = {p.slug for p in session.scalars(select(Project))}
    assert "tracked-project" in projects
    assert "layout-evidence" not in projects


def test_explicit_slug_ingest_overrides_the_default_skip_policy(
    session, tmp_path, monkeypatch, capsys
):
    import corridor.docs as docs_module

    layout = make_pdf(
        tmp_path / "layout.pdf",
        ["Layout evidence text long enough to stay above OCR fallback"],
    )
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "cross-agency.lock.json").write_text(
        json.dumps(
            {
                "project": "layout-evidence",
                "name": "Layout Evidence",
                "agency": "various",
                "ingest_by_default": False,
                "sources": {
                    "layout": {
                        "sha256": "2" * 64,
                        "local_path": str(layout),
                        "doc_type": "matrix",
                        "retrieved_at": "2026-08-05T00:00:00+00:00",
                    }
                },
            }
        )
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(docs_module, "SessionFactory", sessionmaker(bind=session.get_bind()))

    assert docs_module.main(["ingest", "layout-evidence"]) == 0

    out = capsys.readouterr().out
    assert "layout-evidence: 1/1 parsed from cross-agency.lock.json" in out
    assert "skipped bulk ingest" not in out

    projects = {p.slug for p in session.scalars(select(Project))}
    assert "layout-evidence" in projects


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


# ------------------ which form of a document is cited (ADR-0005, #60)


def test_the_spreadsheet_outranks_its_own_printout(session, project):
    """ADR-0005's rule: the structured original is the Document of Record.

    The PDF is a printout of the spreadsheet, and every defect ADR-0004
    catalogues is damage done in the printing. Where both render the same
    document, Evidence cites the one the damage did not happen to.
    """
    from corridor.docs import document_of_record

    printout = _doc(session, project, "ucm.pdf", "a", date(2026, 2, 13))
    original = _doc(session, project, "ucm.xlsx", "b", date(2026, 2, 13))

    assert document_of_record([printout, original]) is original


def test_a_newer_printout_outranks_a_stale_spreadsheet():
    """The ordering ADR-0005 is explicit about, and the reason it matters.

    "A stale spreadsheet must not outrank a newer PDF. Precedence is by
    format only where both render the same document; supersession by date
    still wins, and the two rules have to be applied in that order."

    Applied the other way round, a February PDF loses to a spreadsheet from
    the previous June — which is the ledger citing a revision the project
    has already replaced.
    """
    from corridor.docs import document_of_record

    stale = _fake("ucm.xlsx", date(2025, 6, 20))
    current = _fake("ucm.pdf", date(2026, 2, 13))

    assert document_of_record([stale, current]) is current


def test_format_breaks_a_tie_only_within_one_date():
    from corridor.docs import document_of_record

    old_sheet = _fake("old.xlsx", date(2025, 6, 20))
    new_sheet = _fake("new.xlsx", date(2026, 2, 13))
    new_pdf = _fake("new.pdf", date(2026, 2, 13))

    assert document_of_record([old_sheet, new_pdf, new_sheet]) is new_sheet


def test_a_document_with_no_date_never_outranks_a_dated_one():
    """An undated document is not a current one. Sorting it as though its
    date were today would let a file nobody dated supersede the revision
    the project actually issued."""
    from corridor.docs import document_of_record

    undated = _fake("ucm.xlsx", None)
    dated = _fake("ucm.pdf", date(2025, 6, 20))

    assert document_of_record([undated, dated]) is dated


def test_no_documents_is_no_record():
    from corridor.docs import document_of_record

    assert document_of_record([]) is None


class _fake:
    """A Document-shaped stand-in: the rule reads two attributes."""

    def __init__(self, filename, doc_date):
        self.filename = filename
        self.doc_date = doc_date


def _doc(session, project, filename, sha, doc_date):
    from corridor.models import Document

    document = Document(
        project_id=project.id,
        sha256=sha * 64,
        filename=filename,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
        doc_date=doc_date,
    )
    session.add(document)
    session.flush()
    return document
