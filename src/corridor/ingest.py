"""Register a document and extract its pages.

Every page of a PDF gets both text and a rendered image, because evidence
display needs both: a quote is *verified* against the page text and *shown*
against the page image. A citation you cannot see is not much of a citation.

A spreadsheet has neither pages nor a layout (ADR-0005). Each worksheet
becomes a page, its text is generated from its cells, and there is no image
— rendering one would produce a picture of a spreadsheet rather than
evidence, and the generated text already is the rendering a reviewer checks
a quote against. `text_source` says which of the two a page came from,
because a citation against cells verifies exactly and one against a
printout cannot.

Originals are never modified. The file on disk is read and hashed; nothing
is written back to it.

The discarded PDF router treated fewer than fifty native characters as a scan
and swallowed every OCR exception into an empty string. That made short pages,
mixed pages, and failed OCR indistinguishable. PDF ingest now persists the page
inventory and region decision before it uses any recovered text; an engine
exception becomes a scoped Processing Failure and fails the document attempt.

Which engine supplies a PDF page's native reading is one setting,
`native_reader_token_layer` (ADR-0094, #733). Off, and this is the incumbent
path unchanged. On, and the paired-rendition reader supplies exactly two
things initially: the page's native text and its native Token Layer. #736
binds those projections and the new prose/cell Source Segments to the same
immutable reading. Explicit shadow ingestion can append that reading to an
already-ingested document without replacing its historical DocPage projection.

Which engine's facts decide where OCR is needed is a second, separate setting,
`reader_page_inventory` (#734). Off, and the Page Inventory and the routing
decision come from the incumbent open below. On, and both come from the same
reader, in its own isolated read of the document. The two settings are kept
apart because they move different boundaries — one changes which engine's text
a page carries, the other changes which pages are sent to OCR at all — and a
rollback of either must not drag the other with it. Both are off, because
merging an adapter is not selecting it (#447, #739).

A reader-backed route names Amazon Textract as the engine that should read its
OCR regions (ADR-0094). Ingest does not call Textract: wiring the scanned read
is #739's, and until then the OCR attempt below is the incumbent engine's and
records itself as the incumbent engine's. A routing decision says who a region
*should* be read by; an attempt and its Processing Failure record who read it.
Neither is allowed to borrow the other's name.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.models import (
    NUMBERING_SCHEMES,
    DocPage,
    Document,
    DocumentQuarantine,
    PageProcessingFailure,
    ProcessingArtifact,
)
from corridor.page_inventory import (
    OCR_CONFIGURATION,
    OCR_ENGINE,
    PageInventory,
    PageRoutingDecision,
    PdfRect,
    inventory_page,
    read_reader_page_inventories,
    route_page,
    route_reader_page,
)
from corridor.render_profiles import (
    RenderDerivative,
    persist_render_derivative,
    render_page_derivatives,
)
from corridor.retention import open_reference, register_processing_artifact
from corridor.scanned_reading import (
    ScannedReader,
    open_scanned_reader,
    read_routed_page,
    recovered_text,
)
from corridor.source_segments import (
    SPREADSHEET_SUFFIXES,
    append_ingested_source_segments,
)
from corridor.token_layers import (
    OcrRequest,
    TesseractEngine,
    TokenLayer,
    extract_native_token_layer,
    page_text_projection,
    persist_token_layer,
    read_native_token_layers,
)

# Suffixes read as a workbook rather than a page image. `.xlsm` alongside
# `.xlsx` because TxDOT's own form ships macros in some revisions and the
# cells are identical either way; `.xls` is deliberately absent, since
# openpyxl cannot read the old binary format and a file that silently
# failed would look like a document nobody collected.


@dataclass(frozen=True)
class PageFailure:
    engine: str
    configuration: dict
    region_id: str
    scope: dict
    error_type: str
    error_message: str


@dataclass(frozen=True)
class OcrAttempt:
    """One region's raw OCR output, kept as a Class B intermediary (ADR-0072).

    The recovered text folds into the page's Class A text; this receipt keeps
    the exact engine attempt — configuration, outcome, and output or error —
    as a regenerable, TTL-eligible file so a later failure review can read
    what OCR actually produced without the record depending on it.
    """

    region_id: str
    outcome: str
    receipt_path: Path


@dataclass(frozen=True)
class ExtractedPage:
    page_no: int
    text: str
    image_path: Path | None
    text_source: str
    inventory: PageInventory | None = None
    routing: PageRoutingDecision | None = None
    failures: tuple[PageFailure, ...] = ()
    derivatives: tuple[RenderDerivative, ...] = ()
    ocr_attempts: tuple[OcrAttempt, ...] = ()
    token_layers: tuple[TokenLayer, ...] = ()
    native_reading: object | None = None


def ingest_document(
    session: Session,
    *,
    project_id: int,
    path: Path | str,
    doc_type: str,
    images_dir: Path | str,
    filename: str | None = None,
    source_url: str | None = None,
    retrieved_at: str | datetime | None = None,
    doc_date: date | None = None,
    registry_id: str | None = None,
    expected_sha256: str | None = None,
    numbering_scheme: str | None = None,
    source_delivery_id: int | None = None,
) -> Document:
    # `source_delivery_id` is the ledger row of the delivery that carried these
    # exact bytes in (#687). It is the caller's proven fact, never derived here:
    # only an intake path that already holds a `source_deliveries` row for these
    # bytes may pass one, and every other path leaves the link unknown rather
    # than guessing a delivery from a time, a filename, or an arrival order. The
    # composite foreign key `(source_delivery_id, project_id)` means a delivery
    # taken for another customer's project is refused by the database.

    # The content-addressed store names files by hash, so `path.name` is a
    # 64-character hex string. Callers pass the document's real name — the
    # archive member or manifest title — because this is what a reader sees
    # next to a citation.
    path = Path(path)
    filename = filename or path.name
    source_file_missing = not path.exists()
    if source_file_missing:
        # The lockfile recorded a successful fetch, but the content-addressed
        # store lost the bytes. The recorded hash still identifies the
        # document, so register it visibly failed rather than aborting the
        # whole project's ingest on one hole in the store.
        if expected_sha256 is None:
            raise FileNotFoundError(
                f"document file {path} is missing and no lockfile sha256 "
                "identifies its content"
            )
        sha256 = expected_sha256
    else:
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise ValueError(
                f"document bytes do not match lockfile sha256: expected "
                f"{expected_sha256}, got {sha256}"
            )
    if registry_id is not None and not registry_id.strip():
        raise ValueError("registry_id must be non-empty")
    if numbering_scheme is not None and numbering_scheme not in NUMBERING_SCHEMES:
        raise ValueError(
            f"unknown numbering_scheme {numbering_scheme!r}; expected one "
            f"of {', '.join(NUMBERING_SCHEMES)}"
        )

    registered = None
    if registry_id is not None:
        registered = session.scalar(
            select(Document).where(
                Document.project_id == project_id,
                Document.registry_id == registry_id,
            )
        )

    existing = session.scalars(
        select(Document).where(
            Document.project_id == project_id, Document.sha256 == sha256
        )
    ).first()
    if existing is not None:
        if registered is not None and registered.id != existing.id:
            raise ValueError(
                f"registry_id {registry_id!r} already names another document"
            )
        if registry_id is not None and existing.registry_id not in (
            None,
            registry_id,
        ):
            raise ValueError(
                "one registered document cannot carry multiple registry ids"
            )
        # Re-ingest never re-parses — the bytes are identical by definition.
        # But provenance describes where the file came from, not the file,
        # and a document first ingested without a date would otherwise carry
        # that gap forever. Backfill nulls only; never overwrite. The delivery
        # link follows the same rule for a stronger reason: identical bytes can
        # be delivered twice, and the second delivery must not relabel which one
        # this document came in on (#687).
        for attribute, value in (
            ("registry_id", registry_id),
            ("source_url", source_url),
            ("retrieved_at", _as_datetime(retrieved_at)),
            ("doc_date", doc_date),
            ("filename", filename),
            ("source_delivery_id", source_delivery_id),
        ):
            if value and getattr(existing, attribute) in (None, ""):
                setattr(existing, attribute, value)
        # The numbering scheme is a declaration, not provenance: unlike
        # the backfill-nulls-only facts above it always holds a value
        # (the default), so a manifest that states one re-declares it —
        # re-registration is exactly where a registry fact may change
        # (ADR-0030).
        if numbering_scheme is not None:
            existing.numbering_scheme = numbering_scheme
        if settings.native_reader_token_layer and path.suffix.lower() == ".pdf":
            ingest_native_reader(
                session, document=existing, path=path, images_dir=images_dir
            )
        else:
            append_ingested_source_segments(session, existing, path)
        _quarantine_unmodeled_semantics(session, existing)
        session.flush()
        return existing

    if registered is not None:
        raise ValueError(
            f"registry_id {registry_id!r} already names different document bytes"
        )

    document = Document(
        project_id=project_id,
        registry_id=registry_id,
        sha256=sha256,
        filename=filename,
        doc_type=doc_type,
        source_url=source_url,
        retrieved_at=_as_datetime(retrieved_at),
        doc_date=doc_date,
        pages=0,
        parse_status="pending",
        numbering_scheme=numbering_scheme or "project-unique",
        source_delivery_id=source_delivery_id,
    )
    session.add(document)
    session.flush()
    _quarantine_unmodeled_semantics(session, document)

    if source_file_missing:
        # Registered and visibly failed rather than silently absent — the
        # same contract as a parse failure. The recovery path for failed
        # documents picks it up once the store file is restored.
        document.parse_status = "failed"
        document.pages = 0
        session.flush()
        return document

    try:
        token_dir = Path(images_dir) / sha256
        pages = _extract(path, token_dir, sha256, project=str(document.project_id))
    except Exception:
        # Registered and visibly failed rather than silently absent. A
        # document missing from the ledger looks the same as one that was
        # never collected.
        document.parse_status = "failed"
        document.pages = 0
        session.flush()
        return document

    _persist_pages(session, document, pages, token_dir)

    append_ingested_source_segments(
        session, document, path, native_reading=pages[0].native_reading if pages else None
    )

    document.pages = len(pages)
    document.parse_status = (
        "failed" if any(page.failures for page in pages) else "parsed"
    )
    session.flush()
    return document


def reparse_document(
    session: Session,
    *,
    document: Document,
    path: Path | str,
    images_dir: Path | str,
) -> bool:
    """Re-run parsing for one document whose earlier parse failed (#350).

    Ordinary re-ingest returns the existing document untouched when the bytes are
    identical, which is correct for provenance but leaves a document that failed to
    parse permanently unread even after the reader is fixed. This is the explicit,
    bounded recovery for exactly that case. It re-extracts from the original file —
    which is never modified — and, only on success, writes the freshly parsed pages
    and flips the status. It refuses to touch a document that already parsed, so a
    successfully parsed history is never rewritten as a retry; the caller is
    responsible for the attributable receipt and for excluding held inputs.

    Returns ``True`` when the document is now parsed, ``False`` when it failed
    again. Either way the prior state is preserved rather than corrupted: a second
    failure leaves the status ``failed`` and no partial pages.
    """

    if document.parse_status == "parsed":
        raise ValueError("a successfully parsed document is never re-parsed as a retry")
    path = Path(path)
    try:
        token_dir = Path(images_dir) / document.sha256
        pages = _extract(path, token_dir, document.sha256, project=str(document.project_id))
    except Exception:
        document.parse_status = "failed"
        document.pages = 0
        session.flush()
        return False

    # A failed parse left no pages; guard the invariant rather than assume it, so a
    # partial earlier attempt could never leave duplicated page numbers behind.
    existing = session.scalars(
        select(DocPage).where(DocPage.document_id == document.id)
    ).all()
    for page in existing:
        session.delete(page)
    session.flush()

    _persist_pages(session, document, pages, token_dir)
    append_ingested_source_segments(
        session, document, path, native_reading=pages[0].native_reading if pages else None
    )
    document.pages = len(pages)
    document.parse_status = (
        "failed" if any(page.failures for page in pages) else "parsed"
    )
    session.flush()
    return document.parse_status == "parsed"


def ingest_native_reader(
    session: Session, *, document: Document, path: Path | str,
    images_dir: Path | str, engine: str = "tagged", dpi: int = 36,
):
    """Append an explicit disabled challenger to any registered PDF rendition.

    This returns its own page text and tokens along with the new segments.
    Existing DocPage projections and accepted citations remain historical;
    ordinary document deduplication cannot suppress this execution. Selection
    and downstream semantic mapping remain #447/#737 respectively.
    """
    from corridor.reader_segments import append_native_segments, read_native_pdf

    reading = read_native_pdf(path, source_sha256=document.sha256, engine=engine, dpi=dpi)
    segments = append_native_segments(session, document, reading)
    for layer in reading.token_layers:
        persist_token_layer(session, document.id, layer, output_dir=Path(images_dir) / document.sha256)
    return reading, segments


def _persist_pages(
    session: Session,
    document: Document,
    pages: list[ExtractedPage],
    token_dir: Path,
) -> None:
    # A render or receipt is terminal the moment ingest finishes producing it;
    # the 30-day Class B clock starts here.
    terminal_at = datetime.now(timezone.utc)
    for extracted in pages:
        page = DocPage(
            document_id=document.id,
            page_no=extracted.page_no,
            text=extracted.text,
            # None for a worksheet, which has no rendering to point at.
            image_path=str(extracted.image_path) if extracted.image_path else None,
            text_source=extracted.text_source,
            inventory_json=(
                extracted.inventory.model_dump(mode="json")
                if extracted.inventory
                else None
            ),
            routing_json=(
                extracted.routing.model_dump(mode="json")
                if extracted.routing
                else None
            ),
        )
        session.add(page)
        session.flush()
        # Every render — review, OCR-layout, table-CV, and any region crop —
        # is Class B by construction: the persistence seam classifies it, so
        # no current or future render path can escape TTL.
        for derivative in extracted.derivatives:
            persist_render_derivative(session, document.id, derivative)
        # Native and OCR token layers persist as Class B artifacts with
        # PostgreSQL manifests (ADR-0073); deleting them later leaves every
        # promoted source segment verifiable.
        for layer in extracted.token_layers:
            persist_token_layer(session, document.id, layer, output_dir=token_dir)
        # Raw OCR output is its own Class B intermediary; register each receipt
        # and key it by region so an open failure can hold it reachable.
        raw_ocr_by_region: dict[str, ProcessingArtifact] = {}
        for attempt in extracted.ocr_attempts:
            raw_ocr_by_region[attempt.region_id] = register_processing_artifact(
                session,
                project_id=document.project_id,
                kind="raw_ocr",
                path=attempt.receipt_path,
                terminal_at=terminal_at,
            )
        ocr_layout_artifact = _ocr_layout_artifact(session, extracted)
        for failure in extracted.failures:
            failure_row = PageProcessingFailure(
                document_id=document.id,
                page_number=extracted.page_no,
                engine=failure.engine,
                configuration_json=failure.configuration,
                region_id=failure.region_id,
                scope_json=failure.scope,
                error_type=failure.error_type,
                error_message=failure.error_message,
            )
            session.add(failure_row)
            session.flush()
            referenced_by = f"page_processing_failure:{failure_row.id}"
            # An open failure keeps its own render and raw-OCR intermediaries
            # reachable (90 days), so the evidence survives while it is unresolved.
            for artifact in (
                raw_ocr_by_region.get(failure.region_id),
                ocr_layout_artifact,
            ):
                if artifact is None:
                    continue
                open_reference(
                    session,
                    project_id=document.project_id,
                    family="processing_artifact",
                    source_row_id=artifact.id,
                    kind="processing_failure",
                    referenced_by=referenced_by,
                )


def _ocr_layout_artifact(
    session: Session, extracted: ExtractedPage
) -> ProcessingArtifact | None:
    """The registered OCR-layout render for this page, if any — the artifact an
    open OCR failure holds reachable alongside its raw-OCR receipt."""

    for derivative in extracted.derivatives:
        if derivative.profile_name == "ocr_layout":
            return session.scalar(
                select(ProcessingArtifact).where(
                    ProcessingArtifact.storage_path == str(derivative.artifact_path)
                )
            )
    return None


def _extract(
    path: Path, images_dir: Path, source_sha256: str, *, project: str
) -> list[ExtractedPage]:
    if path.suffix.lower() in SPREADSHEET_SUFFIXES:
        return _extract_sheets(path)
    if path.suffix.lower() == ".eml":
        return _extract_message(path)
    return _extract_pages(path, images_dir, source_sha256, project=project)


def _extract_message(path: Path) -> list[ExtractedPage]:
    """One page holding a stored raw message's plain-text body.

    Email intake (#372, ADR-0058) stores the complete original message
    byte-exact; the body is written evidence that flows through the ordinary
    prose statement path, so it needs a registered page a quote can verify
    against. The exact transmitted text is the page — no image exists, and
    "text_layer" is honest: this is the text the source itself carried.
    """
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body = str(part.get_content() or "")
                break
    elif message.get_content_type() == "text/plain":
        body = str(message.get_content() or "")
    if not body.strip():
        raise ValueError(f"{path.name}: no plain-text body")
    return [ExtractedPage(1, body, None, "text_layer")]


def _extract_sheets(path: Path) -> list[ExtractedPage]:
    """One page per worksheet, its text generated from its cells.

    Sheet order is the file's own, and the page number is its position —
    1-based like a PDF's, so `[D12 p.2]` still names something a reader can
    find. A sheet name would be a better citation and cannot be one: the
    citation columns hold a page number, and widening them is a schema
    change this ticket does not need.
    """
    from corridor.sheets import read_workbook, sheet_text

    sheets = read_workbook(path)
    if not sheets:
        raise ValueError(f"{path.name}: no worksheets")
    return [
        ExtractedPage(index, sheet_text(sheet), None, "cells")
        for index, sheet in enumerate(sheets, start=1)
    ]


def _extract_pages(
    path: Path, images_dir: Path, source_sha256: str, *, project: str
) -> list[ExtractedPage]:
    images_dir.mkdir(parents=True, exist_ok=True)
    out: list[ExtractedPage] = []
    # Which engine actually reads an OCR region. The route names one (#734),
    # and ADR-0094 makes that Textract; `textract_scanned_reading` is what
    # decides whether ingest reads through the authorized adapter or through
    # the incumbent engine (#739). Exactly one of the two is built, so a
    # refused Textract read can never be quietly answered by Tesseract.
    scanned = (
        open_scanned_reader(
            project,
            extraction_run=source_sha256,
            cache_root=Path(images_dir) / "textract",
        )
        if settings.textract_scanned_reading
        else None
    )
    ocr_engine = None if scanned is not None else TesseractEngine()
    if scanned is not None and not settings.reader_page_inventory:
        # The engine an OCR route names comes from the reader-backed router.
        # With the incumbent inventory the route names the incumbent engine,
        # and the scanned setting would have ingest read regions Textract was
        # never routed to. Refuse the pair rather than route by the setting.
        raise ValueError(
            f"{path.name}: the Textract scanned path reads what the "
            "reader-backed Page Inventory routes to it; enable "
            "reader_page_inventory beside it, or neither"
        )
    # One isolated read of the whole document, before the page loop, when the
    # replacement adapter is selected. A failure here fails the document
    # attempt like any other engine failure; it is never quietly answered by
    # the incumbent, which would make the two readings indistinguishable in
    # the record.
    from corridor.reader_segments import read_native_pdf

    native_reading = (
        read_native_pdf(path, source_sha256=source_sha256)
        if settings.native_reader_token_layer else None
    )
    reader_layers = {
        layer.page_no: layer for layer in native_reading.token_layers
    } if native_reading is not None else {}
    # The inventory's own isolated read, on the same terms. It is a second
    # read of the same document when both settings are on: the two adapters
    # own their own readings and neither answers for the other, which is what
    # keeps a rollback of one from disturbing the other. Collapsing them into
    # one read is worth doing the day both are selected together (#447).
    reader_inventories: dict[int, PageInventory] = (
        read_reader_page_inventories(path)
        if settings.reader_page_inventory
        else {}
    )

    with pymupdf.open(path) as pdf:
        if pdf.page_count == 0:
            raise ValueError(f"{path.name}: no pages")
        for index, page in enumerate(pdf):
            # 1-based: citations are written for humans, and [D12 p.4] must
            # mean the page a reader sees.
            page_no = index + 1
            incumbent_text = page.get_text()
            if settings.reader_page_inventory:
                reader_inventory = reader_inventories.get(page_no)
                if reader_inventory is None:
                    raise ValueError(
                        f"{path.name}: the reader returned no page {page_no}"
                    )
                inventory, routing = (
                    reader_inventory,
                    route_reader_page(reader_inventory),
                )
            else:
                inventory = inventory_page(page, native_text=incumbent_text)
                routing = route_page(inventory)
            reader_layer = reader_layers.get(page_no)
            if settings.native_reader_token_layer and reader_layer is None:
                raise ValueError(
                    f"{path.name}: the reader returned no page {page_no}"
                )
            # `DocPage.text` is a projection over the native token layer, not
            # a second reading beside it (ADR-0073).
            native_text = (
                page_text_projection(reader_layer)
                if reader_layer is not None
                else incumbent_text
            )
            # The layout/model derivative is purpose-specific even when OCR is
            # not needed; vision consumers never borrow reviewer pixels. All
            # three are asked for at once so the worker's OpenCV/PyMuPDF import
            # is paid once a page rather than three times (#548).
            derivatives = render_page_derivatives(
                pdf_path=path,
                page_number=page_no,
                profile_names=("review", "ocr_layout", "table_cv"),
                output_dir=images_dir,
            )
            review_derivative, ocr_derivative, table_derivative = derivatives
            image_path = review_derivative.artifact_path
            ocr_text: list[str] = []
            failures: list[PageFailure] = []
            ocr_attempts: list[OcrAttempt] = []
            scanned_layer: TokenLayer | None = None
            if scanned is not None:
                outcome = _read_scanned_page(
                    scanned,
                    path=path,
                    images_dir=images_dir,
                    page_no=page_no,
                    source_sha256=source_sha256,
                    routing=routing,
                    native_layer=reader_layer,
                    render_profile_id=ocr_derivative.profile_id,
                )
                ocr_text.extend(outcome.text)
                failures.extend(outcome.failures)
                ocr_attempts.extend(outcome.attempts)
                scanned_layer = outcome.token_layer
            # The incumbent per-region loop. The scanned route read the whole
            # page above — one raster, one call, the routed regions selecting
            # which of its cells are consumed — so the two never both run.
            for region in routing.regions if scanned is None else ():
                if region.mode not in {"ocr", "both"}:
                    continue
                scope = {
                    "page_number": page_no,
                    "region_id": region.region_id,
                    "box": region.box.model_dump(mode="json"),
                }
                # The engine that actually reads the region, and its
                # configuration — not the engine the route names. Until #739
                # wires Textract in, those differ on a reader-backed route,
                # and a Processing Failure must name the engine that failed.
                configuration = {
                    **OCR_CONFIGURATION,
                    "render_profile_id": ocr_derivative.profile_id,
                    "render_dpi": ocr_derivative.dpi,
                }
                try:
                    recovered = _ocr_region(
                        ocr_derivative.artifact_path,
                        region.box,
                        inventory.boxes.crop,
                    )
                except Exception as exc:
                    outcome = "failed"
                    receipt = _write_raw_ocr_receipt(
                        images_dir,
                        page_no=page_no,
                        region_id=region.region_id,
                        configuration=configuration,
                        scope=scope,
                        outcome=outcome,
                        text=None,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                    ocr_attempts.append(
                        OcrAttempt(region.region_id, outcome, receipt)
                    )
                    failures.append(
                        PageFailure(
                            engine=OCR_ENGINE,
                            configuration=configuration,
                            region_id=region.region_id,
                            scope=scope,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
                    continue
                outcome = "recovered" if recovered.strip() else "empty"
                receipt = _write_raw_ocr_receipt(
                    images_dir,
                    page_no=page_no,
                    region_id=region.region_id,
                    configuration=configuration,
                    scope=scope,
                    outcome=outcome,
                    text=recovered,
                    error_type=None,
                    error_message=None,
                )
                ocr_attempts.append(OcrAttempt(region.region_id, outcome, receipt))
                if recovered.strip():
                    ocr_text.append(recovered.strip())
            parts = []
            if routing.page_mode in {"native", "both"} or not ocr_text:
                if native_text:
                    parts.append(native_text.rstrip())
            parts.extend(text for text in ocr_text if text not in parts)
            text = "\n".join(parts)
            if text:
                text += "\n"
            # Compatibility readers keep every page that required OCR on the
            # less-trusted OCR path, including failed attempts. Calling an
            # image-only failure `text_layer` would recreate the retired lie.
            source = (
                "ocr" if routing.page_mode in {"ocr", "both"} else "text_layer"
            )

            # Two coordinate-bearing token layers, kept separately (ADR-0073):
            # the native reading always, plus an OCR layer when the page routes
            # through OCR. Nothing picks a page-wide winner here — DocPage.text
            # above is a rebuildable projection, and geometry-consuming
            # extraction reads these tokens, not the page string.
            token_layers: list[TokenLayer] = [
                reader_layer
                if reader_layer is not None
                else extract_native_token_layer(
                    page, page_no=page_no, source_sha256=source_sha256
                )
            ]
            if scanned_layer is not None:
                token_layers.append(scanned_layer)
            elif ocr_engine is not None and routing.page_mode in {"ocr", "both"}:
                token_layers.append(
                    ocr_engine.recognize(
                        OcrRequest(
                            page_no=page_no,
                            source_sha256=source_sha256,
                            image_path=ocr_derivative.artifact_path,
                            derivative=ocr_derivative,
                        )
                    )
                )

            out.append(
                ExtractedPage(
                    page_no=page_no,
                    text=text,
                    image_path=image_path,
                    text_source=source,
                    inventory=inventory,
                    routing=routing,
                    failures=tuple(failures),
                    derivatives=tuple(derivatives),
                    ocr_attempts=tuple(ocr_attempts),
                    token_layers=tuple(token_layers),
                    native_reading=native_reading,
                )
            )

    return out


@dataclass(frozen=True)
class _ScannedPageOutcome:
    """What one page's Textract read produced, in the shapes the page loop keeps."""

    text: tuple[str, ...]
    failures: tuple[PageFailure, ...]
    attempts: tuple[OcrAttempt, ...]
    token_layer: TokenLayer | None


def _read_scanned_page(
    scanned: ScannedReader,
    *,
    path: Path,
    images_dir: Path,
    page_no: int,
    source_sha256: str,
    routing: PageRoutingDecision,
    native_layer: TokenLayer | None,
    render_profile_id: str,
) -> _ScannedPageOutcome:
    """One page through the authorized adapter, as the page loop records it.

    A page the decision routes nowhere is not read and is not a failure: an
    ordinary blank page and a clean native page are not scanned work. A page
    that is routed and cannot be read is a Processing Failure per routed
    region, naming the provider that failed, the request configuration and the
    page scope — and the incumbent engine is not asked to fill the gap, so the
    record says Textract did not read this page rather than showing a reading
    from an engine nobody selected.
    """

    outcome = read_routed_page(
        scanned,
        path,
        page_no=page_no,
        routing=routing,
        rendition_sha256=source_sha256,
        source_sha256=source_sha256,
        native_layer=native_layer,
        render_profile_id=render_profile_id,
    )
    if outcome is None:
        return _ScannedPageOutcome((), (), (), None)
    scope = {
        "page_number": page_no,
        "region_ids": [region.region_id for region in outcome.regions],
        "router_version": routing.router_version,
    }
    if outcome.failure is not None:
        failure = outcome.failure
        receipt = _write_raw_ocr_receipt(
            images_dir,
            page_no=page_no,
            region_id="page",
            configuration=failure.configuration,
            scope=failure.scope,
            outcome="failed",
            text=None,
            error_type=failure.reason,
            error_message=failure.detail,
            engine=failure.engine,
        )
        return _ScannedPageOutcome(
            (),
            tuple(
                PageFailure(
                    engine=failure.engine,
                    configuration=failure.configuration,
                    region_id=region.region_id,
                    scope=dict(scope, region_id=region.region_id),
                    error_type=failure.reason,
                    error_message=failure.detail,
                )
                for region in outcome.regions
            ),
            (OcrAttempt("page", "failed", receipt),),
            None,
        )
    reading = outcome.reading
    assert reading is not None
    recovered = recovered_text(reading.reading)
    result = "recovered" if recovered.strip() else "empty"
    receipt = _write_raw_ocr_receipt(
        images_dir,
        page_no=page_no,
        region_id="page",
        configuration={"provenance": reading.provenance},
        scope=scope,
        outcome=result,
        text=recovered,
        error_type=None,
        error_message=None,
        engine=reading.token_layer.identity.engine,
        values=[
            {
                "region_id": value.region_id,
                "table": value.table,
                "row": value.row,
                "column": value.column,
                "box": value.box.model_dump(mode="json"),
                "value": value.value,
                "value_source": value.value_source,
                "state": value.state,
                "confidence": value.confidence,
                "locator": value.locator.model_dump(mode="json") if value.locator else None,
                "provenance": value.provenance,
            }
            for value in reading.values
        ],
    )
    return _ScannedPageOutcome(
        (recovered.strip(),) if recovered.strip() else (),
        (),
        (OcrAttempt("page", result, receipt),),
        reading.token_layer,
    )


def _write_raw_ocr_receipt(
    images_dir: Path,
    *,
    page_no: int,
    region_id: str,
    configuration: dict,
    scope: dict,
    outcome: str,
    text: str | None,
    error_type: str | None,
    error_message: str | None,
    engine: str = OCR_ENGINE,
    values: list[dict] | None = None,
) -> Path:
    """Write one OCR attempt's exact output to a content-addressed Class B file.

    Content-addressed so a re-render of the same page reuses the identical
    receipt rather than colliding; the persistence seam registers it for
    retention. No session here: extraction stays pure, persistence classifies.

    `engine` is the engine that actually read, which is not always the one the
    route named (#739): a receipt that said `tesseract` over a Textract reading
    would be the same lie the retired router told about thin text. `values` is
    the scanned route's per-cell classification — which cells were re-mapped
    from the document's own glyphs and which are Unconfirmed readings — kept
    here rather than in the record, because it is evidence about a reading and
    not a value the record depends on.
    """

    payload = {
        "configuration": dict(configuration),
        "engine": engine,
        "error_message": error_message,
        "error_type": error_type,
        "outcome": outcome,
        "page_number": page_no,
        "region_id": region_id,
        "scope": scope,
        "text": text,
        "values": values,
    }
    content = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    safe_region_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in region_id
    )
    destination = (
        images_dir / f"{page_no:04d}-{safe_region_id}-raw-ocr-{digest}.json"
    )
    if destination.exists():
        if destination.read_bytes() != content:
            raise ValueError(f"raw OCR receipt content-address collision at {destination}")
    else:
        destination.write_bytes(content)
    return destination.resolve()


def _ocr_region(image_path: Path, region: PdfRect, page_box: PdfRect) -> str:
    """OCR one recorded page region from the compatibility render.

    The spec names `ocrmypdf`, whose value is producing a searchable PDF.
    v0 stores page text and never mutates originals, so that second PDF
    pipeline (and its ghostscript dependency chain) buys nothing here —
    the 150 dpi PNG is already on disk.

    Exceptions deliberately escape this adapter. The page loop turns each one
    into a scoped Processing Failure; an empty-string fallback would make a
    failed engine indistinguishable from a genuinely blank region.
    """
    import pytesseract
    from PIL import Image

    with Image.open(image_path) as image:
        page_width = max(1, page_box.width)
        page_height = max(1, page_box.height)
        crop = (
            round((region.x0 - page_box.x0) * image.width / page_width),
            round((region.y0 - page_box.y0) * image.height / page_height),
            round((region.x1 - page_box.x0) * image.width / page_width),
            round((region.y1 - page_box.y0) * image.height / page_height),
        )
        bounded = (
            max(0, crop[0]),
            max(0, crop[1]),
            min(image.width, crop[2]),
            min(image.height, crop[3]),
        )
        region_image = image.crop(bounded)
        return pytesseract.image_to_string(
            region_image,
            lang=str(OCR_CONFIGURATION["language"]),
            config=f"--psm {OCR_CONFIGURATION['page_segmentation_mode']}",
        )


def _as_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _quarantine_unmodeled_semantics(session, document) -> None:
    """Record the durable outcome for a document Corridor must not read.

    A schedule's rows relate to each other — one work item must complete
    before another may start — and Corridor has no model for that relation
    (#149). The document is registered and visible; this row is the
    project-level fact that it is deliberately unread, surviving process
    exit rather than living in an operator's memory. Idempotent, so
    re-ingest never duplicates it.
    """
    if document.doc_type != "schedule":
        return
    if session.get(DocumentQuarantine, document.id) is None:
        session.add(
            DocumentQuarantine(
                document_id=document.id,
                reason=(
                    "document-asserted work sequencing is not modeled; rows "
                    "would keep their values and lose their relationships "
                    "(#149)"
                ),
            )
        )
