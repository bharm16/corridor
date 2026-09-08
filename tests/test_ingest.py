import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from corridor.config import settings
from corridor.db import Session, engine
from corridor.ingest import ingest_document
from corridor.page_inventory import READER_COORDINATE_FRAME, READER_ROUTER_VERSION
from corridor.models import (
    DocPage,
    Document,
    PageProcessingFailure,
    PageRenderDerivative,
    Project,
    TokenLayerManifest,
)
from corridor.token_layers import (
    READER_ENGINE,
    load_token_layer,
    page_text_projection,
)

from pdf_fixture_support import PdfFixture, scan_image


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
    fixture = PdfFixture()
    # Each page must carry more than MIN_TEXT_CHARS of real text, or the
    # thin-text heuristic correctly treats it as a scan and OCRs it.
    bodies = [
        "Utility Owner: AT&T Texas (SWBT) - Telecom - underground fiber optic",
        "STA 1149+00 to STA 1153+17, offset 303 L/R, crossing IH 69 baseline",
    ]
    for n, body in enumerate(bodies, start=1):
        page = fixture.add_page()
        page.text((72, 100), f"Page {n}")
        page.text((72, 130), body)
    return fixture.save(tmp_path / "matrix.pdf")


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


def test_registry_identity_cannot_alias_one_document_to_two_declared_ids(
    session, project, pdf, tmp_path
):
    ingest(
        session,
        project,
        pdf,
        tmp_path / "images",
        registry_id="matrix-r1",
    )

    with pytest.raises(ValueError, match="multiple registry ids"):
        ingest(
            session,
            project,
            pdf,
            tmp_path / "images",
            registry_id="matrix-r2",
        )


def test_registry_identity_cannot_move_to_different_document_bytes(
    session, project, pdf, tmp_path
):
    ingest(
        session,
        project,
        pdf,
        tmp_path / "images",
        registry_id="matrix-r1",
    )
    other = tmp_path / "other.pdf"
    other.write_bytes(pdf.read_bytes() + b"\n%different registered bytes")

    with pytest.raises(ValueError, match="different document bytes"):
        ingest(
            session,
            project,
            other,
            tmp_path / "images",
            registry_id="matrix-r1",
        )


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
        image = Path(page.image_path)
        assert image.exists() and image.stat().st_size > 0
    derivatives = session.scalars(
        select(PageRenderDerivative).where(
            PageRenderDerivative.document_id == doc.id
        )
    ).all()
    assert {(item.page_number, item.profile_name) for item in derivatives} == {
        (1, "review"),
        (1, "ocr_layout"),
        (1, "table_cv"),
        (2, "review"),
        (2, "ocr_layout"),
        (2, "table_cv"),
    }
    assert all(
        item.manifest_json["preprocessing"] == []
        for item in derivatives
        if item.profile_name == "review"
    )


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

    Built by rasterising the text and embedding the raster as the page's only
    content, so the resulting file has pixels and no text layer.
    """
    scan = scan_image(
        595,
        842,
        dpi=300,
        lines=(
            ((72, 120), "UTILITY RELOCATION AGREEMENT", 22),
            ((72, 170), "CENTERPOINT ENERGY", 22),
        ),
    )
    fixture = PdfFixture()
    fixture.add_page().image((0, 0, 595, 842), scan)
    return fixture.save(tmp_path / "scanned.pdf")


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
    derivatives = session.scalars(
        select(PageRenderDerivative).where(
            PageRenderDerivative.document_id == doc.id
        )
    ).all()
    assert {item.profile_name for item in derivatives} == {
        "review",
        "ocr_layout",
        "table_cv",
    }
    review = next(item for item in derivatives if item.profile_name == "review")
    ocr = next(item for item in derivatives if item.profile_name == "ocr_layout")
    assert review.artifact_path == page.image_path
    assert review.manifest_json["preprocessing"] == []
    assert ocr.manifest_json["preprocessing"] == ["grayscale", "denoise"]
    assert ocr.artifact_path != review.artifact_path


def test_every_pdf_page_persists_its_inventory_and_routing_decision(
    session, project, pdf, tmp_path
):
    doc = ingest(session, project, pdf, tmp_path / "images")
    pages = session.scalars(
        select(DocPage).where(DocPage.document_id == doc.id).order_by(DocPage.page_no)
    ).all()

    assert len(pages) == 2
    assert all(
        page.inventory_json["schema_version"]
        == "corridor.pdf-page-inventory.v1"
        for page in pages
    )
    assert all(
        page.routing_json["schema_version"] == "corridor.pdf-page-routing.v1"
        for page in pages
    )
    assert {page.routing_json["page_mode"] for page in pages} == {"native"}


def test_a_short_clean_native_page_never_calls_ocr(
    session, project, tmp_path, monkeypatch
):
    fixture = PdfFixture()
    fixture.add_page().text((72, 100), "OK")
    path = fixture.save(tmp_path / "short.pdf")

    def unexpected_ocr(*_args, **_kwargs):
        raise AssertionError("short native text must not route by character count")

    monkeypatch.setattr("corridor.ingest._ocr_region", unexpected_ocr)
    stored = ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type="other",
        images_dir=tmp_path / "images",
    )
    [stored_page] = session.scalars(
        select(DocPage).where(DocPage.document_id == stored.id)
    ).all()
    assert stored_page.text == "OK\n"
    assert stored_page.text_source == "text_layer"
    assert stored_page.routing_json["reason"] == "clean_native_text"


def test_a_mixed_page_routes_native_and_image_regions_independently(
    session, project, tmp_path, monkeypatch
):
    scan = scan_image(200, 100, dpi=150, lines=(((20, 50), "SCANNED TABLE", 11),))
    fixture = PdfFixture()
    page = fixture.add_page(width=400, height=300)
    page.text((20, 30), "Native heading")
    page.image((20, 70, 380, 270), scan)
    path = fixture.save(tmp_path / "mixed.pdf")
    monkeypatch.setattr(
        "corridor.ingest._ocr_region", lambda *_args, **_kwargs: "OCR TABLE"
    )

    stored = ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type="matrix",
        images_dir=tmp_path / "images",
    )
    [stored_page] = session.scalars(
        select(DocPage).where(DocPage.document_id == stored.id)
    ).all()

    assert stored_page.routing_json["page_mode"] == "both"
    assert {region["mode"] for region in stored_page.routing_json["regions"]} == {
        "native",
        "ocr",
    }
    assert "Native heading" in stored_page.text
    assert "OCR TABLE" in stored_page.text
    assert stored_page.text_source == "ocr"


def test_ocr_exception_persists_a_visible_processing_failure(
    session, project, scanned_pdf, tmp_path, monkeypatch
):
    def failed_ocr(*_args, **_kwargs):
        raise RuntimeError("tesseract unavailable")

    monkeypatch.setattr("corridor.ingest._ocr_region", failed_ocr)
    stored = ingest_document(
        session,
        project_id=project.id,
        path=scanned_pdf,
        doc_type="agreement",
        images_dir=tmp_path / "images",
    )
    [stored_page] = session.scalars(
        select(DocPage).where(DocPage.document_id == stored.id)
    ).all()
    [failure] = session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == stored.id,
            PageProcessingFailure.page_number == 1,
        )
    ).all()

    assert stored.parse_status == "failed"
    assert stored.pages == 1
    assert stored_page.text_source == "ocr"
    assert stored_page.routing_json["page_mode"] == "ocr"
    assert failure.engine == "tesseract"
    assert failure.configuration_json == {
        "language": "eng",
        "page_segmentation_mode": 6,
        "render_profile_id": "27530b723b5a182f77a252adc085a28277084b1cf233c22e0e1859f84b26f483",
        "render_dpi": 300,
    }
    assert failure.scope_json["page_number"] == 1
    assert failure.scope_json["region_id"].startswith("image-")
    assert failure.error_type == "RuntimeError"
    assert failure.error_message == "tesseract unavailable"
    from corridor.docs import list_documents

    [summary] = list_documents(session, project.id)
    assert summary.ocr_pages == 1


def test_successful_reparse_preserves_the_prior_processing_failure(
    session, project, scanned_pdf, tmp_path, monkeypatch
):
    def failed_ocr(*_args, **_kwargs):
        raise RuntimeError("tesseract unavailable")

    monkeypatch.setattr("corridor.ingest._ocr_region", failed_ocr)
    stored = ingest_document(
        session,
        project_id=project.id,
        path=scanned_pdf,
        doc_type="agreement",
        images_dir=tmp_path / "images",
    )
    failure_id = session.scalar(
        select(PageProcessingFailure.id).where(
            PageProcessingFailure.document_id == stored.id
        )
    )
    monkeypatch.setattr(
        "corridor.ingest._ocr_region", lambda *_args, **_kwargs: "RECOVERED TEXT"
    )

    from corridor.ingest import reparse_document

    assert reparse_document(
        session,
        document=stored,
        path=scanned_pdf,
        images_dir=tmp_path / "images",
    ) is True

    assert stored.parse_status == "parsed"
    assert session.get(PageProcessingFailure, failure_id) is not None
    assert session.scalar(
        select(PageProcessingFailure.error_message).where(
            PageProcessingFailure.id == failure_id
        )
    ) == "tesseract unavailable"


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


# ------------------------- a source that is not a printout (ADR-0005, #60)


@pytest.fixture
def workbook(tmp_path):
    """A real .xlsx, so the sheet reader does its actual work."""
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    conflicts = book.create_sheet("Utility Conflicts")
    for row in (
        ["Utility Conflict Management (UCM) - Utility Conflicts"],
        ["Utility Conflict ID", "Utility Owner", "Start Station"],
        ["UC-1", "CenterPoint Energy", "1149+00"],
    ):
        conflicts.append(row)
    book.create_sheet("Drop-Down Lists").append(["District", "Utility Type"])
    path = tmp_path / "ucm.xlsx"
    book.save(path)
    return path


def test_a_workbook_ingests_a_page_per_sheet(session, project, workbook, tmp_path):
    """Ingestion stops being PDF-only, which ADR-0005 names as a
    consequence: the structured original is the Document of Record and a
    workbook is not a thing you render at 150 dpi."""
    document = ingest_document(
        session,
        project_id=project.id,
        path=workbook,
        doc_type="matrix",
        images_dir=tmp_path / "images",
    )

    assert document.parse_status == "parsed"
    assert document.pages == 2
    pages = session.scalars(
        select(DocPage).where(DocPage.document_id == document.id).order_by(DocPage.page_no)
    ).all()
    assert [p.page_no for p in pages] == [1, 2]


def test_a_sheets_text_is_generated_from_its_cells(session, project, workbook, tmp_path):
    """The text is the rendering, so every cell has to reach it.

    A sheet has no page image to show a reviewer beside a quote, so this
    text is what stands in for one — and a value stored from a cell that
    never reached it would be unverifiable by construction.
    """
    document = ingest_document(
        session, project_id=project.id, path=workbook,
        doc_type="matrix", images_dir=tmp_path / "images",
    )

    first = session.scalars(
        select(DocPage).where(DocPage.document_id == document.id, DocPage.page_no == 1)
    ).one()
    for value in ("Utility Conflict ID", "UC-1", "CenterPoint Energy", "1149+00"):
        assert value in first.text


def test_a_sheet_is_marked_as_read_from_cells(session, project, workbook, tmp_path):
    """Not `text_layer`. A spreadsheet borrowing that label would be
    indistinguishable from a PDF's, and the label is what lets a citation
    against cells verify exactly rather than at the print-damage
    threshold."""
    document = ingest_document(
        session, project_id=project.id, path=workbook,
        doc_type="matrix", images_dir=tmp_path / "images",
    )

    sources = {
        p.text_source
        for p in session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        )
    }
    assert sources == {"cells"}


def test_a_sheet_has_no_page_image(session, project, workbook, tmp_path):
    """There is nothing to render, and inventing one would be a picture of
    a spreadsheet rather than evidence. `image_path` is already nullable."""
    document = ingest_document(
        session, project_id=project.id, path=workbook,
        doc_type="matrix", images_dir=tmp_path / "images",
    )

    assert all(
        p.image_path is None
        for p in session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        )
    )


def test_a_workbook_that_cannot_be_read_fails_visibly(session, project, tmp_path):
    """Registered and visibly failed, never silently absent — the same
    bargain a broken PDF gets."""
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not a workbook")

    document = ingest_document(
        session, project_id=project.id, path=path,
        doc_type="matrix", images_dir=tmp_path / "images",
    )

    assert document.parse_status == "failed"
    assert document.pages == 0


# ── The declared numbering scheme (ADR-0030) ─────────────────────────────


def test_the_default_scheme_is_project_unique(session, project, pdf, tmp_path):
    doc = ingest(session, project, pdf, tmp_path / "img")
    assert doc.numbering_scheme == "project-unique"


def test_a_declared_scheme_is_registered_with_the_document(
    session, project, pdf, tmp_path
):
    doc = ingest(
        session, project, pdf, tmp_path / "img", numbering_scheme="per-party"
    )
    assert doc.numbering_scheme == "per-party"


def test_an_unknown_scheme_is_refused(session, project, pdf, tmp_path):
    with pytest.raises(ValueError, match="numbering_scheme"):
        ingest(
            session, project, pdf, tmp_path / "img", numbering_scheme="newest"
        )


def test_reingest_re_declares_the_scheme(session, project, pdf, tmp_path):
    """Unlike provenance, which backfills nulls only, the scheme is a
    declaration: re-registration is where a registry fact may change —
    which is exactly how an already-ingested document gets corrected."""
    first = ingest(session, project, pdf, tmp_path / "img")
    assert first.numbering_scheme == "project-unique"

    again = ingest(
        session, project, pdf, tmp_path / "img", numbering_scheme="per-party"
    )
    assert again.id == first.id
    assert again.numbering_scheme == "per-party"


def test_a_silent_reingest_keeps_the_declared_scheme(
    session, project, pdf, tmp_path
):
    ingest(session, project, pdf, tmp_path / "img", numbering_scheme="per-party")
    again = ingest(session, project, pdf, tmp_path / "img")
    assert again.numbering_scheme == "per-party"


def test_a_missing_store_file_registers_the_document_visibly_failed(
    session, project, pdf, tmp_path
):
    import hashlib

    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    # The lockfile recorded a successful fetch; the store lost the bytes.
    doc = ingest_document(
        session,
        project_id=project.id,
        path=tmp_path / "wiped" / pdf.name,
        doc_type="matrix",
        images_dir=tmp_path / "images",
        expected_sha256=sha,
    )
    assert doc.sha256 == sha
    assert doc.parse_status == "failed"
    assert doc.pages == 0


def test_a_missing_store_file_without_a_recorded_sha_still_raises(
    session, project, tmp_path
):
    with pytest.raises(FileNotFoundError):
        ingest_document(
            session,
            project_id=project.id,
            path=tmp_path / "nowhere.pdf",
            doc_type="matrix",
            images_dir=tmp_path / "images",
        )


# ---- the reader-backed native layer (#733) ---------------------------------


def _native_layers(session, document_id):
    return session.scalars(
        select(TokenLayerManifest)
        .where(
            TokenLayerManifest.document_id == document_id,
            TokenLayerManifest.origin == "native",
        )
        .order_by(TokenLayerManifest.page_no)
    ).all()


def test_the_native_layer_stays_the_incumbent_until_the_adapter_is_enabled(
    session, project, pdf, tmp_path
):
    """No merge moves a production default; #447 owns native selection."""

    assert settings.native_reader_token_layer is False
    doc = ingest(session, project, pdf, tmp_path / "images")

    layers = _native_layers(session, doc.id)
    assert [layer.page_no for layer in layers] == [1, 2]
    assert {layer.engine_json["engine"] for layer in layers} == {"pymupdf"}


def test_the_enabled_adapter_supplies_the_page_text_and_the_native_layer(
    session, project, pdf, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "native_reader_token_layer", True)

    doc = ingest(session, project, pdf, tmp_path / "images")

    layers = _native_layers(session, doc.id)
    assert [layer.page_no for layer in layers] == [1, 2]
    assert {layer.engine_json["engine"] for layer in layers} == {READER_ENGINE}
    pages = session.scalars(
        select(DocPage).where(DocPage.document_id == doc.id).order_by(DocPage.page_no)
    ).all()
    for page, manifest in zip(pages, layers, strict=True):
        # The page string is the projection over the layer that was retained,
        # not a second reading of the PDF (ADR-0073).
        projection = page_text_projection(load_token_layer(manifest))
        assert page.text.rstrip("\n") == projection
    assert "AT&T Texas" in pages[0].text
    assert "1149+00" in pages[1].text


def test_enabling_the_adapter_changes_the_text_and_the_layer_and_nothing_else(
    session, project, pdf, tmp_path, monkeypatch
):
    """The adapter supplies page text and the native token layer.

    The Page Inventory and the routing decision are read from the incumbent
    on both settings, because the reader's inventory is #734's and a routing
    change here would move the OCR boundary with it.
    """

    incumbent = ingest(session, project, pdf, tmp_path / "incumbent")
    inventories = [
        (page.inventory_json, page.routing_json, page.text_source)
        for page in session.scalars(
            select(DocPage)
            .where(DocPage.document_id == incumbent.id)
            .order_by(DocPage.page_no)
        ).all()
    ]

    other = Project(slug="reader-adapter", name="Reader adapter", is_synthetic=True)
    session.add(other)
    session.flush()
    monkeypatch.setattr(settings, "native_reader_token_layer", True)
    replacement = ingest_document(
        session,
        project_id=other.id,
        path=pdf,
        doc_type="matrix",
        images_dir=tmp_path / "replacement",
    )

    assert [
        (page.inventory_json, page.routing_json, page.text_source)
        for page in session.scalars(
            select(DocPage)
            .where(DocPage.document_id == replacement.id)
            .order_by(DocPage.page_no)
        ).all()
    ] == inventories


# ---- the reader-backed page inventory and routing (#734) -------------------


def _pages(session, document_id):
    return session.scalars(
        select(DocPage)
        .where(DocPage.document_id == document_id)
        .order_by(DocPage.page_no)
    ).all()


def test_enabling_the_reader_backed_inventory_changes_the_inventory_and_the_route_only(
    session, project, pdf, tmp_path, monkeypatch
):
    """The second adapter's blast radius, stated as an assertion (#734).

    On, the persisted inventory is the reader's and the route it decides names
    Textract. Everything the inventory does not own is byte-identical to the
    incumbent run: the page text, which engine's Token Layer was retained, the
    render derivatives, and the compatibility `text_source`.
    """

    incumbent = ingest(session, project, pdf, tmp_path / "incumbent")
    incumbent_pages = _pages(session, incumbent.id)
    # The render is content-addressed, so the file name is the same artifact
    # under either run's images directory.
    unchanged = [
        (page.page_no, page.text, page.text_source, Path(page.image_path).name)
        for page in incumbent_pages
    ]
    assert {page.routing_json["ocr_engine"] for page in incumbent_pages} == {
        "tesseract"
    }
    assert {page.routing_json["router_version"] for page in incumbent_pages} == {
        "page-inventory-router-v1"
    }

    other = Project(slug="reader-inventory", name="Reader inventory", is_synthetic=True)
    session.add(other)
    session.flush()
    monkeypatch.setattr(settings, "reader_page_inventory", True)
    replacement = ingest_document(
        session,
        project_id=other.id,
        path=pdf,
        doc_type="matrix",
        images_dir=tmp_path / "replacement",
    )
    replacement_pages = _pages(session, replacement.id)

    assert [
        (page.page_no, page.text, page.text_source, Path(page.image_path).name)
        for page in replacement_pages
    ] == unchanged
    assert [
        layer.engine_json["engine"] for layer in _native_layers(session, replacement.id)
    ] == [
        layer.engine_json["engine"] for layer in _native_layers(session, incumbent.id)
    ]
    assert all(
        page.inventory_json["coordinate_frame"] == READER_COORDINATE_FRAME
        for page in replacement_pages
    )
    assert {page.routing_json["ocr_engine"] for page in replacement_pages} == {
        "textract"
    }
    assert {page.routing_json["router_version"] for page in replacement_pages} == {
        READER_ROUTER_VERSION
    }
    # The route is still native on these pages, so naming Textract changed
    # nothing about what was read: the engine is recorded, not invoked.
    assert {page.routing_json["page_mode"] for page in replacement_pages} == {"native"}
    assert session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == replacement.id
        )
    ).all() == []


def test_a_reader_backed_ocr_failure_is_still_a_scoped_processing_failure(
    session, project, tmp_path, monkeypatch
):
    """The route names Textract; the failure names the engine that failed.

    Wiring Textract in is #739's, so the region is still read by the incumbent
    OCR engine, and a Processing Failure records that engine, its
    configuration and the page scope. Calling the failure Textract's because
    the route asked for Textract would be the same lie the retired router told
    about thin text.
    """

    scan = scan_image(400, 300, dpi=150, lines=(((20, 50), "SCANNED", 11),))
    fixture = PdfFixture()
    fixture.add_page(width=400, height=300).image((0, 0, 400, 300), scan)
    path = fixture.save(tmp_path / "scan.pdf")

    def explode(*_args, **_kwargs):
        raise RuntimeError("engine unavailable")

    monkeypatch.setattr(settings, "reader_page_inventory", True)
    monkeypatch.setattr("corridor.ingest._ocr_region", explode)
    doc = ingest(session, project, path, tmp_path / "images")

    [page] = _pages(session, doc.id)
    assert page.routing_json["page_mode"] == "ocr"
    assert page.routing_json["ocr_engine"] == "textract"
    [failure] = session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == doc.id
        )
    ).all()
    assert failure.engine == "tesseract"
    assert failure.configuration_json["language"] == "eng"
    assert failure.scope_json["page_number"] == 1
    assert failure.error_type == "RuntimeError"
