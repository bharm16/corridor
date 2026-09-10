import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from corridor.ingest import (
    DocumentParseFailure,
    _extract_pages,
    document_parse_failure,
    ingest_document,
)
from corridor.page_inventory import READER_COORDINATE_FRAME, READER_ROUTER_VERSION
from corridor.models import DocPage, Document, PageProcessingFailure, PageRenderDerivative, TokenLayerManifest
from corridor.token_layers import (
    READER_ENGINE,
    load_token_layer,
    page_text_projection,
)

from pdf_fixture_support import PdfFixture, scan_image


@pytest.fixture
def pdf(tmp_path, request):
    """A synthetic PDF. Exercises code paths only — never a quality claim."""
    fixture = PdfFixture()
    # Each page must carry more than MIN_TEXT_CHARS of real text, or the
    # thin-text heuristic correctly treats it as a scan and OCRs it.
    bodies = [
        "Utility Owner: AT&T Texas (SWBT) - Telecom - underground fiber optic",
        "STA 1149+00 to STA 1153+17, offset 303 L/R, crossing IH 69 baseline",
    ]
    # Only page fan-out assertions need two pages. Identity, provenance,
    # failure, and re-ingestion cases use one complete synthetic page.
    page_count = getattr(request, "param", 1)
    for n, body in enumerate(bodies[:page_count], start=1):
        # These assertions exercise ingestion, not full-sheet geometry. Keep
        # the authored coordinates and text without rasterizing unused space
        # for every provenance and re-ingestion test.
        page = fixture.add_page(height=180)
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


@pytest.mark.parametrize("pdf", [2], indirect=True)
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


@pytest.mark.parametrize("pdf", [2], indirect=True)
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


@pytest.mark.parametrize("pdf", [2], indirect=True)
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



# --- the scanned route's double (#732, #739) ---------------------------------
#
# Ingest reads OCR-routed regions through the authorized Textract boundary, and
# the incumbent local engine that these tests used to monkeypatch is gone
# (#741). A test that needs a scanned reading supplies the provider's answer
# through the same boundary, with a transport double, so nothing here reaches a
# network and the reading still arrives the way a real one would.


def _provider_response(frame, lines):
    from corridor_pdf_reader.textract.tests.helpers import Page

    builder = Page(frame=frame)
    top = 30.0
    for text in lines:
        words = [builder.word(text, (20, top, 20 + 7 * len(text), top + 16))]
        builder.line(words)
        cell = builder.cell(1, 1, (15, top - 5, 25 + 7 * len(text), top + 21), words)
        builder.table((15, top - 5, 25 + 7 * len(text), top + 21), [cell])
        top += 40
    return builder.response()


class _RecordedService:
    """One recorded AnalyzeDocument answer, or a refusal to answer at all."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def analyze_document(self, *, Document, FeatureTypes):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def _authorized_reader(project, tmp_path, service, *, dpi=36):
    from corridor.provider_authorization import TransmissionApproval
    from corridor.scanned_reading import ScannedReader
    from corridor_pdf_reader.textract_adapter.boundary import open_boundary
    from corridor_pdf_reader.textract_adapter.identity import RequestConfiguration
    from corridor_pdf_reader.textract_adapter.records import (
        PROVIDER_POSTURE,
        ExperimentScope,
        RequestBoundary,
    )
    from dataclasses import replace as replace_fields

    scope = ExperimentScope(
        record_id="exp-741",
        dataset=str(project.id),
        dataset_digest="0" * 8,
        purpose="scanned-page-reading",
        scope="one synthetic scan built by the test",
        source_classes=frozenset({"synthetic", "scanned-pdf"}),
        region=PROVIDER_POSTURE.region,
        posture_identity=PROVIDER_POSTURE.identity,
        posture_digest=PROVIDER_POSTURE.digest,
        recorded_by="a named person",
        recorded_on="2026-09-08",
    )
    boundary = RequestBoundary(
        project=str(project.id),
        source_class="scanned-pdf",
        purpose="scanned-page-reading",
        region=PROVIDER_POSTURE.region,
        posture_identity=PROVIDER_POSTURE.identity,
        stage="experiment",
    )
    adapter = open_boundary(
        scope,
        boundary,
        extraction_run="run-741",
        cache_root=tmp_path / "cache",
        service=service,
        configuration=RequestConfiguration(dpi=dpi),
        posture=replace_fields(
            PROVIDER_POSTURE,
            status="accepted",
            retention="verified",
            ai_services_opt_out="optOut",
            permissions="verified",
            # The test's approval, not the document's (ADR-0098): the recorded posture has none.
            experimental_approval=TransmissionApproval(
                source_classes=("synthetic", "scanned-pdf"),
                purposes=("scanned-page-reading",),
                unverified=(),
                approved_by="a named maintainer, in this test only",
                approved_on="2026-09-10",
            ),
            customer_processing_approval=None,
        ),
        sleep=lambda seconds: None,
    )
    return ScannedReader(adapter=adapter, refusal=None, request=boundary)


def _install_reader(monkeypatch, reader):
    from corridor import ingest as ingest_module

    monkeypatch.setattr(
        ingest_module, "open_scanned_reader", lambda *args, **kwargs: reader
    )


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


def test_a_scanned_page_reads_through_the_provider(
    session, project, scanned_pdf, tmp_path, monkeypatch
):
    """An image-only page routes to OCR and is read by the selected provider."""
    service = _RecordedService(
        _provider_response(
            (595.0, 842.0),
            ("UTILITY RELOCATION AGREEMENT", "CENTERPOINT ENERGY"),
        )
    )
    _install_reader(monkeypatch, _authorized_reader(project, tmp_path, service))
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


@pytest.mark.parametrize("pdf", [2], indirect=True)
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

    service = _RecordedService(
        error=AssertionError("short native text must not route to the provider")
    )
    _install_reader(monkeypatch, _authorized_reader(project, tmp_path, service))
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
    assert service.calls == 0


def test_a_mixed_page_routes_native_and_image_regions_independently(
    session, project, tmp_path, monkeypatch
):
    scan = scan_image(200, 100, dpi=150, lines=(((20, 50), "SCANNED TABLE", 11),))
    fixture = PdfFixture()
    page = fixture.add_page(width=400, height=300)
    page.text((20, 30), "Native heading")
    page.image((20, 70, 380, 270), scan)
    path = fixture.save(tmp_path / "mixed.pdf")
    service = _RecordedService(_provider_response((400.0, 300.0), ("OCR TABLE",)))
    _install_reader(monkeypatch, _authorized_reader(project, tmp_path, service))

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


def test_a_refused_scanned_read_persists_a_visible_processing_failure(
    session, project, scanned_pdf, tmp_path
):
    """No authorization record exists (#732, #522), so the read is refused.

    The refusal is the ordinary answer today, and it must reach the record as
    a scoped Processing Failure that fails the document attempt: an OCR-routed
    page that nobody read is not a parsed page.
    """
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
    assert failure.engine == "textract"
    assert failure.configuration_json["operation"] == "AnalyzeDocument"
    assert failure.scope_json["page_number"] == 1
    assert failure.error_type == "authorization-absent"
    assert failure.error_message
    from corridor.docs import list_documents

    [summary] = list_documents(session, project.id)
    assert summary.ocr_pages == 1


def test_successful_reparse_preserves_the_prior_processing_failure(
    session, project, scanned_pdf, tmp_path, monkeypatch
):
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
    refused_message = session.scalar(
        select(PageProcessingFailure.error_message).where(
            PageProcessingFailure.id == failure_id
        )
    )
    service = _RecordedService(
        _provider_response((595.0, 842.0), ("RECOVERED TEXT",))
    )
    _install_reader(monkeypatch, _authorized_reader(project, tmp_path, service))

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
    ) == refused_message


def test_ocr_text_is_citable(session, project, scanned_pdf, tmp_path, monkeypatch):
    """A citation against OCR output must still verify, or scanned evidence
    can never support a claim."""
    from corridor.verify import quote_appears_on

    service = _RecordedService(
        _provider_response((595.0, 842.0), ("UTILITY RELOCATION AGREEMENT",))
    )
    _install_reader(monkeypatch, _authorized_reader(project, tmp_path, service))
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
    # And the reason survives. `parse_status` alone said only "something went
    # wrong somewhere", which is exactly what a per-page failure is not allowed
    # to say: it records engine, region, error type and message.
    failure = document_parse_failure(doc)
    assert failure.stage == "read_document"
    assert failure.error_type and failure.error_message


def test_a_whole_document_read_failure_keeps_its_stage_type_and_message(
    session, project, pdf, tmp_path, monkeypatch
):
    """The same shape a `PageProcessingFailure` row has, for the document.

    A per-page failure records `error_type` and `error_message`; the
    whole-document `except` discarded both, so `pipeline.ingest_and_extract`
    could only record "ingest parse_status is 'failed'" and a reviewer had to
    re-run the reader to learn what happened.
    """

    def unreadable(*args, **kwargs):
        raise RuntimeError("the reader could not open the rendition")

    monkeypatch.setattr("corridor.ingest._extract", unreadable)
    doc = ingest(session, project, pdf, tmp_path / "images")

    assert (doc.parse_status, doc.pages) == ("failed", 0)
    assert document_parse_failure(doc) == DocumentParseFailure(
        stage="read_document",
        error_type="RuntimeError",
        error_message="the reader could not open the rendition",
    )


def test_a_source_the_store_lost_records_that_stage_rather_than_a_read(
    session, project, tmp_path
):
    doc = ingest_document(
        session,
        project_id=project.id,
        path=tmp_path / "never-stored.pdf",
        doc_type="matrix",
        images_dir=tmp_path / "images",
        expected_sha256="a" * 64,
    )

    assert doc.parse_status == "failed"
    failure = document_parse_failure(doc)
    assert failure.stage == "locate_source"
    assert failure.error_type == "FileNotFoundError"


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


@pytest.mark.parametrize("pdf", [2], indirect=True)
def test_the_reader_supplies_the_page_text_and_the_native_layer(
    session, project, pdf, tmp_path
):
    """One reader, no switch: ADR-0094's native reading is the only one (#741)."""

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


# ---- the reader-backed page inventory and routing (#734) -------------------


def _pages(session, document_id):
    return session.scalars(
        select(DocPage)
        .where(DocPage.document_id == document_id)
        .order_by(DocPage.page_no)
    ).all()


def test_the_inventory_and_the_route_come_from_the_reader(
    session, project, pdf, tmp_path
):
    """The persisted inventory is the reader's and the route names Textract.

    Both used to be one setting away from the incumbent's inventory and the
    incumbent's engine name (#734). The incumbent is gone (#741), so this is
    now what a page records, with nothing to compare it against inside the
    product.
    """

    doc = ingest(session, project, pdf, tmp_path / "images")
    pages = _pages(session, doc.id)

    assert all(
        page.inventory_json["coordinate_frame"] == READER_COORDINATE_FRAME
        for page in pages
    )
    assert {page.routing_json["ocr_engine"] for page in pages} == {"textract"}
    assert {page.routing_json["router_version"] for page in pages} == {
        READER_ROUTER_VERSION
    }
    # The route is native on these pages, so naming Textract changed nothing
    # about what was read: the engine is recorded, not invoked.
    assert {page.routing_json["page_mode"] for page in pages} == {"native"}
    assert {page.text_source for page in pages} == {"text_layer"}
    assert session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == doc.id
        )
    ).all() == []


def test_a_failed_provider_call_is_a_scoped_processing_failure(
    session, project, tmp_path, monkeypatch
):
    """The route names Textract, and so does the failure, because Textract read.

    A transport failure is not the same record as a refused authorization: the
    request left, it was answered with an error, and the failure says so. There
    is no second engine left to answer the region instead (#741), which is the
    point — a reading from an engine nobody selected would be worse than none.
    """

    scan = scan_image(400, 300, dpi=150, lines=(((20, 50), "SCANNED", 11),))
    fixture = PdfFixture()
    fixture.add_page(width=400, height=300).image((0, 0, 400, 300), scan)
    path = fixture.save(tmp_path / "scan.pdf")

    service = _RecordedService(error=RuntimeError("provider unavailable"))
    _install_reader(monkeypatch, _authorized_reader(project, tmp_path, service))
    doc = ingest(session, project, path, tmp_path / "images")

    [page] = _pages(session, doc.id)
    assert page.routing_json["page_mode"] == "ocr"
    assert page.routing_json["ocr_engine"] == "textract"
    [failure] = session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == doc.id
        )
    ).all()
    assert failure.engine == "textract"
    assert failure.scope_json["page_number"] == 1
    assert failure.error_type == "provider-call-failed"


def _scan_pdf(tmp_path, name="scan.pdf"):
    scan = scan_image(400, 300, dpi=150, lines=(((20, 50), "SCANNED", 11),))
    fixture = PdfFixture()
    fixture.add_page(width=400, height=300).image((0, 0, 400, 300), scan)
    return fixture.save(tmp_path / name)


def _ocr_layers(session, document_id):
    return session.scalars(
        select(TokenLayerManifest)
        .where(
            TokenLayerManifest.document_id == document_id,
            TokenLayerManifest.origin == "ocr",
        )
        .order_by(TokenLayerManifest.page_no)
    ).all()


def test_the_scanned_route_refuses_without_an_authorization_and_never_falls_back(
    session, project, tmp_path, monkeypatch
):
    """Corridor has no authorization record yet (#732, #522), so this is the live shape.

    The routed region records a Processing Failure naming Textract, with zero
    outbound requests, and the incumbent engine is not asked to fill the gap:
    there is no OCR token layer and the page carries no OCR text. A reading
    from an engine nobody selected would be worse than no reading.
    """

    doc = ingest(session, project, _scan_pdf(tmp_path), tmp_path / "images")

    [page] = _pages(session, doc.id)
    assert page.routing_json["ocr_engine"] == "textract"
    assert page.routing_json["page_mode"] == "ocr"
    failures = session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == doc.id
        )
    ).all()
    assert [failure.engine for failure in failures] == ["textract"]
    assert failures[0].error_type == "authorization-absent"
    assert failures[0].scope_json["page_number"] == 1
    assert failures[0].configuration_json["operation"] == "AnalyzeDocument"
    assert _ocr_layers(session, doc.id) == []
    assert "SCANNED" not in (page.text or "")


def test_an_authorized_scanned_read_writes_the_provider_backed_token_layer(
    session, project, tmp_path, monkeypatch
):
    """The other branch, with a record and a transport double: no live call anywhere."""
    service = _RecordedService(_provider_response((400.0, 300.0), ("SCANNED",)))
    _install_reader(monkeypatch, _authorized_reader(project, tmp_path, service))

    doc = ingest(session, project, _scan_pdf(tmp_path), tmp_path / "images")

    [page] = _pages(session, doc.id)
    assert service.calls == 1
    assert session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == doc.id
        )
    ).all() == []
    [layer] = _ocr_layers(session, doc.id)
    assert layer.engine_json["engine"] == "textract"
    assert layer.engine_json["provider"] == "textract"
    assert layer.engine_json["provider_model_version"] == "1.0"
    assert len(layer.engine_json["raw_response_sha256"]) == 64
    assert len(layer.engine_json["reading_sha256"]) == 64
    assert "SCANNED" in page.text
    assert page.text_source == "ocr"
    # The receipt names the engine that actually read, and records which cells
    # are Unconfirmed readings rather than leaving that to be re-derived.
    receipt = json.loads(
        next((tmp_path / "images" / doc.sha256).glob("0001-page-raw-ocr-*.json")).read_text()
    )
    assert receipt["engine"] == "textract"
    assert [(value["value"], value["value_source"], value["state"]) for value in receipt["values"]] == [
        ("SCANNED", "textract_words", "unconfirmed")
    ]
    assert receipt["values"][0]["provenance"]["processing"]["engine"] == "textract"


def test_a_textract_only_value_lands_as_an_unconfirmed_reading_the_upgrade_pass_can_see(
    session, project, tmp_path, monkeypatch
):
    """ADR-0094: a value only Textract supplies is an Unconfirmed reading.

    The receipt above records the class; this is the record of it. Ingest
    appends one ``unconfirmed`` resolution per Textract-only cell, bound to the
    document, the page and the cell the receipt names, so ADR-0064's upgrade
    pass — wired through ``load_project`` — has a population to work: the row
    never counts toward Ready, and a readable sibling stating the value
    upgrades it without a human step.
    """
    from corridor.models import UnreadableCellResolution
    from corridor.scanned_reading import UNCONFIRMED_READING_POLICY_VERSION
    from corridor.unreadable_cell_admission import process_unreadable_cell_upgrades
    from corridor.unreadable_cells import contributes_to_ready

    service = _RecordedService(_provider_response((400.0, 300.0), ("SCANNED",)))
    _install_reader(monkeypatch, _authorized_reader(project, tmp_path, service))

    doc = ingest(session, project, _scan_pdf(tmp_path), tmp_path / "images")

    receipt = json.loads(
        next((tmp_path / "images" / doc.sha256).glob("0001-page-raw-ocr-*.json")).read_text()
    )
    [value] = receipt["values"]
    [row] = session.scalars(
        select(UnreadableCellResolution).where(
            UnreadableCellResolution.project_id == project.id
        )
    ).all()
    assert row.document_id == doc.id
    assert row.page_no == 1
    assert row.cell_key == f"scan:p1:t{value['table']}:r{value['row']}:c{value['column']}"
    assert (row.state, row.value, row.origin) == ("unconfirmed", "SCANNED", "harness")
    assert row.policy_version == UNCONFIRMED_READING_POLICY_VERSION
    assert contributes_to_ready(row) is False
    # The row resolves to the observation that produced it (#809): the same
    # request identity, response digest and authorization identity the receipt
    # and the token-layer manifest recorded, held once as a row and named by
    # id. `run_id` is the harness's and stays null on this route.
    from corridor.models import ScannedPageObservation

    processing = value["provenance"]["processing"]
    [layer] = _ocr_layers(session, doc.id)
    observation = session.get(ScannedPageObservation, row.observation_id)
    assert row.run_id is None
    assert row.observation_unbound_reason is None
    assert row.source_region_id == value["region_id"] == value["provenance"]["source"]["region_id"]
    assert (observation.document_id, observation.page_no) == (doc.id, 1)
    assert observation.rendition_sha256 == doc.sha256
    assert observation.authorization_record_id == "exp-741" == processing["authorization_record_id"]
    assert observation.scope_digest == processing["scope_digest"]
    assert observation.raster_sha256 == processing["raster_sha256"]
    assert observation.raw_response_sha256 == processing["raw_response_digest"]
    assert observation.raw_response_sha256 == layer.engine_json["raw_response_sha256"]
    assert observation.reading_sha256 == processing["normalized_reading_digest"]
    assert observation.reading_sha256 == layer.engine_json["reading_sha256"]
    assert observation.provider_request_id == processing["request_identity"]["request_id"]
    assert observation.provider_request_id == layer.engine_json["provider_request_id"]
    assert observation.provider_model_version == processing["provider_model_version"]

    readable = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"sue/level-a.xlsx").hexdigest(),
        filename="sue/level-a.xlsx",
        doc_type="other",
        parse_status="parsed",
    )
    session.add(readable)
    session.flush()
    session.add(
        DocPage(
            document_id=readable.id,
            page_no=1,
            text="Sheet: SCANNED\nOwner: CenterPoint",
            text_source="cells",
        )
    )
    session.flush()

    [upgraded] = process_unreadable_cell_upgrades(session, project.id)

    assert upgraded.document_id == doc.id
    assert upgraded.cell_key == row.cell_key
    assert (upgraded.state, upgraded.origin) == ("corroborated", "corroboration_upgrade")
    assert upgraded.corroboration_document_id == readable.id
    assert contributes_to_ready(upgraded) is False
    # The corroboration is of this observation's value, and says so.
    assert upgraded.observation_id == row.observation_id
    assert upgraded.source_region_id == row.source_region_id


def test_a_page_the_route_sends_nowhere_is_not_read_and_is_not_a_failure(
    session, project, pdf, tmp_path, monkeypatch
):
    """An ordinary native page is not scanned work that failed (#739).

    The scanned setting is on and no authorization record exists, so any page
    the decision routed to Textract would record a refusal. These pages are
    routed nowhere, so nothing is spent and nothing is recorded — a blank or
    clean page must not become a Processing Failure just because the scanned
    path is selected.
    """

    doc = ingest(session, project, pdf, tmp_path / "images")

    pages = _pages(session, doc.id)
    assert {page.routing_json["page_mode"] for page in pages} == {"native"}
    assert {page.text_source for page in pages} == {"text_layer"}
    assert session.scalars(
        select(PageProcessingFailure).where(
            PageProcessingFailure.document_id == doc.id
        )
    ).all() == []
    assert _ocr_layers(session, doc.id) == []
