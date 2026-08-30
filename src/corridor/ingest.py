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
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import NUMBERING_SCHEMES, DocPage, Document, DocumentQuarantine

# 150 dpi: legible for reading a quote in context, and small enough that a
# 700-row matrix does not turn into a gigabyte of PNGs.
RENDER_DPI = 150

# Below this, the page has no usable text layer and is almost certainly a
# scan. Scanner-output PDFs return roughly 30 characters per page, which
# reads as an empty page rather than as a scan — a first-pass extraction
# returning little means "scan", not "blank".
MIN_TEXT_CHARS = 50

# Suffixes read as a workbook rather than a page image. `.xlsm` alongside
# `.xlsx` because TxDOT's own form ships macros in some revisions and the
# cells are identical either way; `.xls` is deliberately absent, since
# openpyxl cannot read the old binary format and a file that silently
# failed would look like a document nobody collected.
SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm"}


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

    for page_no, text, image_path, text_source in pages:
        session.add(
            DocPage(
                document_id=document.id,
                page_no=page_no,
                text=text,
                # None for a worksheet, which has no rendering to point at.
                image_path=str(image_path) if image_path else None,
                text_source=text_source,
            )
        )

    document.pages = len(pages)
    document.parse_status = "parsed"
    session.flush()
    return document


def _extract(path: Path, images_dir: Path) -> list[tuple[int, str, Path | None, str]]:
    if path.suffix.lower() in SPREADSHEET_SUFFIXES:
        return _extract_sheets(path)
    if path.suffix.lower() == ".eml":
        return _extract_message(path)
    return _extract_pages(path, images_dir)


def _extract_message(path: Path) -> list[tuple[int, str, None, str]]:
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
    return [(1, body, None, "text_layer")]


def _extract_sheets(path: Path) -> list[tuple[int, str, None, str]]:
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
        (index, sheet_text(sheet), None, "cells")
        for index, sheet in enumerate(sheets, start=1)
    ]


def _extract_pages(path: Path, images_dir: Path) -> list[tuple[int, str, Path, str]]:
    images_dir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[int, str, Path, str]] = []

    with pymupdf.open(path) as pdf:
        if pdf.page_count == 0:
            raise ValueError(f"{path.name}: no pages")
        for index, page in enumerate(pdf):
            # 1-based: citations are written for humans, and [D12 p.4] must
            # mean the page a reader sees.
            page_no = index + 1
            image_path = images_dir / f"{page_no:04d}.png"
            page.get_pixmap(dpi=RENDER_DPI).save(image_path)

            text = page.get_text()
            source = "text_layer"
            if len(text.strip()) < MIN_TEXT_CHARS:
                ocr = _ocr(image_path)
                if len(ocr.strip()) > len(text.strip()):
                    text, source = ocr, "ocr"

            out.append((page_no, text, image_path, source))

    return out


def _ocr(image_path: Path) -> str:
    """OCR the page image we already rendered.

    The spec names `ocrmypdf`, whose value is producing a searchable PDF.
    v0 stores page text and never mutates originals, so that second PDF
    pipeline (and its ghostscript dependency chain) buys nothing here —
    the 150 dpi PNG is already on disk.
    """
    try:
        import pytesseract
        from PIL import Image

        with Image.open(image_path) as image:
            return pytesseract.image_to_string(image)
    except Exception:
        # A missing tesseract binary must not fail the whole ingest. The
        # page keeps its thin text layer and is visibly not OCR'd.
        return ""


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
