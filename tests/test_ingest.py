import hashlib

import pymupdf
import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.ingest import ingest_document
from corridor.models import DocPage, Document, Project


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
    p = Project(slug="ingest-test", name="Ingest Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def pdf(tmp_path):
    """A synthetic PDF. Exercises code paths only — never a quality claim."""
    doc = pymupdf.open()
    # Each page must carry more than MIN_TEXT_CHARS of real text, or the
    # thin-text heuristic correctly treats it as a scan and OCRs it.
    bodies = [
        "Utility Owner: AT&T Texas (SWBT) - Telecom - underground fiber optic",
        "STA 1149+00 to STA 1153+17, offset 303 L/R, crossing IH 69 baseline",
    ]
    for n, body in enumerate(bodies, start=1):
        page = doc.new_page()
        page.insert_text((72, 100), f"Page {n}")
        page.insert_text((72, 130), body)
    path = tmp_path / "matrix.pdf"
    doc.save(path)
    doc.close()
    return path


def ingest(session, project, pdf, images, **kw):
    return ingest_document(
        session,
        project_id=project.id,
        path=pdf,
        doc_type="matrix",
        images_dir=images,
        **kw,
    )


def test_registers_the_document_with_its_provenance(session, project, pdf, tmp_path):
    doc = ingest(
        session,
        project,
        pdf,
        tmp_path / "images",
        source_url="https://example.gov/utilities.zip",
        retrieved_at="2026-08-02T20:00:00+00:00",
    )
    assert doc.sha256 == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert doc.filename == "matrix.pdf"
    assert doc.pages == 2
    assert doc.parse_status == "parsed"
    # Corpus provenance survives into the ledger. Without it a citation
    # bottoms out at "a file on my laptop".
    assert doc.source_url == "https://example.gov/utilities.zip"
    assert doc.retrieved_at is not None


def test_every_page_gets_text_and_an_image(session, project, pdf, tmp_path):
    doc = ingest(session, project, pdf, tmp_path / "images")
    pages = session.scalars(
        select(DocPage).where(DocPage.document_id == doc.id).order_by(DocPage.page_no)
    ).all()

    assert [p.page_no for p in pages] == [1, 2]
    assert "AT&T Texas" in pages[0].text
    assert "1149+00" in pages[1].text
    for page in pages:
        assert page.image_path
        image = tmp_path / "images" / f"{doc.sha256}" / f"{page.page_no:04d}.png"
        assert image.exists() and image.stat().st_size > 0


def test_filename_can_override_the_content_addressed_path(
    session, project, pdf, tmp_path
):
    """The store names files by hash, so path.name is 64 hex characters.

    A citation rendered as `cfded966….pdf p.4` tells a reader nothing.
    """
    doc = ingest(
        session,
        project,
        pdf,
        tmp_path / "images",
        filename="nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
    )
    assert doc.filename == "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"


def test_page_numbers_are_one_based(session, project, pdf, tmp_path):
    """Citations read [D12 p.4]. A zero-based page number cites the wrong page."""
    doc = ingest(session, project, pdf, tmp_path / "images")
    first = session.scalars(
        select(DocPage).where(DocPage.document_id == doc.id).order_by(DocPage.page_no)
    ).first()
    assert first.page_no == 1
    assert "Page 1" in first.text


def test_reingest_is_a_noop(session, project, pdf, tmp_path):
    first = ingest(session, project, pdf, tmp_path / "images")
    session.flush()
    second = ingest(session, project, pdf, tmp_path / "images")

    assert second.id == first.id
    docs = session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).all()
    assert len(docs) == 1
    pages = session.scalars(
        select(DocPage).where(DocPage.document_id == first.id)
    ).all()
    assert len(pages) == 2


def test_reingest_backfills_missing_provenance(session, project, pdf, tmp_path):
    """Re-ingest is a no-op for content, not for provenance.

    A document first ingested without a date would otherwise carry that gap
    forever, since the sha256 check returns early.
    """
    from datetime import date

    first = ingest(session, project, pdf, tmp_path / "images")
    assert first.doc_date is None
    session.flush()

    second = ingest(
        session,
        project,
        pdf,
        tmp_path / "images",
        source_url="https://example.gov/utilities.zip",
        doc_date=date(2026, 2, 13),
    )
    assert second.id == first.id
    assert second.doc_date == date(2026, 2, 13)
    assert second.source_url == "https://example.gov/utilities.zip"


def test_reingest_never_overwrites_existing_provenance(session, project, pdf, tmp_path):
    from datetime import date

    first = ingest(
        session, project, pdf, tmp_path / "images", doc_date=date(2026, 2, 13)
    )
    session.flush()
    second = ingest(
        session, project, pdf, tmp_path / "images", doc_date=date(1999, 1, 1)
    )
    assert second.doc_date == date(2026, 2, 13)


def test_the_original_file_is_never_modified(session, project, pdf, tmp_path):
    before = pdf.read_bytes()
    ingest(session, project, pdf, tmp_path / "images")
    assert pdf.read_bytes() == before


@pytest.fixture
def scanned_pdf(tmp_path):
    """An image-only PDF, as a scanner produces.

    Built by rendering a text page and re-inserting it as an image, so the
    resulting file has pixels and no text layer.
    """
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 120), "UTILITY RELOCATION AGREEMENT", fontsize=22)
    page.insert_text((72, 170), "CENTERPOINT ENERGY", fontsize=22)
    pixmap = page.get_pixmap(dpi=300)
    source.close()

    scanned = pymupdf.open()
    out_page = scanned.new_page()
    out_page.insert_image(out_page.rect, pixmap=pixmap)
    path = tmp_path / "scanned.pdf"
    scanned.save(path)
    scanned.close()
    return path


def test_a_page_with_a_real_text_layer_is_not_ocred(session, project, pdf, tmp_path):
    doc = ingest(session, project, pdf, tmp_path / "images")
    pages = session.scalars(
        select(DocPage).where(DocPage.document_id == doc.id)
    ).all()
    assert {p.text_source for p in pages} == {"text_layer"}


def test_a_scanned_page_falls_back_to_ocr(session, project, scanned_pdf, tmp_path):
    """Scanner output yields ~30 chars, which reads as blank rather than scanned."""
    doc = ingest_document(
        session,
        project_id=project.id,
        path=scanned_pdf,
        doc_type="agreement",
        images_dir=tmp_path / "images",
    )
    page = session.scalars(
        select(DocPage).where(DocPage.document_id == doc.id)
    ).one()

    assert page.text_source == "ocr"
    assert "UTILITY" in page.text.upper()
    assert "CENTERPOINT" in page.text.upper()


def test_ocr_text_is_citable(session, project, scanned_pdf, tmp_path):
    """A citation against OCR output must still verify, or scanned evidence
    can never support a claim."""
    from corridor.verify import quote_appears_on

    doc = ingest_document(
        session,
        project_id=project.id,
        path=scanned_pdf,
        doc_type="agreement",
        images_dir=tmp_path / "images",
    )
    page = session.scalars(
        select(DocPage).where(DocPage.document_id == doc.id)
    ).one()
    assert quote_appears_on("UTILITY RELOCATION AGREEMENT", page.text)


def test_an_unreadable_file_is_recorded_as_failed(session, project, tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not a real pdf")

    doc = ingest_document(
        session,
        project_id=project.id,
        path=broken,
        doc_type="matrix",
        images_dir=tmp_path / "images",
    )
    # Registered so it is visible, not silently skipped, and clearly not parsed.
    assert doc.parse_status == "failed"
    assert doc.pages == 0
