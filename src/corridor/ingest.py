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
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    NUMBERING_SCHEMES,
    DocPage,
    Document,
    DocumentQuarantine,
    PageProcessingFailure,
)
from corridor.page_inventory import (
    OCR_CONFIGURATION,
    OCR_ENGINE,
    PageInventory,
    PageRoutingDecision,
    PdfRect,
    inventory_page,
    route_page,
)
from corridor.source_segments import (
    SPREADSHEET_SUFFIXES,
    append_ingested_source_segments,
)

# 150 dpi: legible for reading a quote in context, and small enough that a
# 700-row matrix does not turn into a gigabyte of PNGs.
RENDER_DPI = 150

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
class ExtractedPage:
    page_no: int
    text: str
    image_path: Path | None
    text_source: str
    inventory: PageInventory | None = None
    routing: PageRoutingDecision | None = None
    failures: tuple[PageFailure, ...] = ()


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
) -> Document:
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
        # that gap forever. Backfill nulls only; never overwrite.
        for attribute, value in (
            ("registry_id", registry_id),
            ("source_url", source_url),
            ("retrieved_at", _as_datetime(retrieved_at)),
            ("doc_date", doc_date),
            ("filename", filename),
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
        pages = _extract(path, Path(images_dir) / sha256)
    except Exception:
        # Registered and visibly failed rather than silently absent. A
        # document missing from the ledger looks the same as one that was
        # never collected.
        document.parse_status = "failed"
        document.pages = 0
        session.flush()
        return document

    _persist_pages(session, document, pages)

    append_ingested_source_segments(session, document, path)

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
        pages = _extract(path, Path(images_dir) / document.sha256)
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

    _persist_pages(session, document, pages)
    document.pages = len(pages)
    document.parse_status = (
        "failed" if any(page.failures for page in pages) else "parsed"
    )
    session.flush()
    return document.parse_status == "parsed"


def _persist_pages(
    session: Session, document: Document, pages: list[ExtractedPage]
) -> None:
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
        for failure in extracted.failures:
            session.add(
                PageProcessingFailure(
                    document_id=document.id,
                    page_number=extracted.page_no,
                    engine=failure.engine,
                    configuration_json=failure.configuration,
                    region_id=failure.region_id,
                    scope_json=failure.scope,
                    error_type=failure.error_type,
                    error_message=failure.error_message,
                )
            )


def _extract(path: Path, images_dir: Path) -> list[ExtractedPage]:
    if path.suffix.lower() in SPREADSHEET_SUFFIXES:
        return _extract_sheets(path)
    if path.suffix.lower() == ".eml":
        return _extract_message(path)
    return _extract_pages(path, images_dir)


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


def _extract_pages(path: Path, images_dir: Path) -> list[ExtractedPage]:
    images_dir.mkdir(parents=True, exist_ok=True)
    out: list[ExtractedPage] = []

    with pymupdf.open(path) as pdf:
        if pdf.page_count == 0:
            raise ValueError(f"{path.name}: no pages")
        for index, page in enumerate(pdf):
            # 1-based: citations are written for humans, and [D12 p.4] must
            # mean the page a reader sees.
            page_no = index + 1
            image_path = images_dir / f"{page_no:04d}.png"
            page.get_pixmap(dpi=RENDER_DPI).save(image_path)

            native_text = page.get_text()
            inventory = inventory_page(page, native_text=native_text)
            routing = route_page(inventory)
            ocr_text: list[str] = []
            failures: list[PageFailure] = []
            for region in routing.regions:
                if region.mode not in {"ocr", "both"}:
                    continue
                try:
                    recovered = _ocr_region(
                        image_path,
                        region.box,
                        inventory.boxes.crop,
                    )
                except Exception as exc:
                    failures.append(
                        PageFailure(
                            engine=OCR_ENGINE,
                            configuration=dict(OCR_CONFIGURATION),
                            region_id=region.region_id,
                            scope={
                                "page_number": page_no,
                                "region_id": region.region_id,
                                "box": region.box.model_dump(mode="json"),
                            },
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
                    continue
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

            out.append(
                ExtractedPage(
                    page_no=page_no,
                    text=text,
                    image_path=image_path,
                    text_source=source,
                    inventory=inventory,
                    routing=routing,
                    failures=tuple(failures),
                )
            )

    return out


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
