"""Register a document and extract its pages.

Every page gets both text and a rendered image, because evidence display
needs both: a quote is *verified* against the page text and *shown* against
the page image. A citation you cannot see is not much of a citation.

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

from corridor.models import DocPage, Document

# 150 dpi: legible for reading a quote in context, and small enough that a
# 700-row matrix does not turn into a gigabyte of PNGs.
RENDER_DPI = 150

# Below this, the page has no usable text layer and is almost certainly a
# scan. Scanner-output PDFs return roughly 30 characters per page, which
# reads as an empty page rather than as a scan — a first-pass extraction
# returning little means "scan", not "blank".
MIN_TEXT_CHARS = 50


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
) -> Document:
    # The content-addressed store names files by hash, so `path.name` is a
    # 64-character hex string. Callers pass the document's real name — the
    # archive member or manifest title — because this is what a reader sees
    # next to a citation.
    path = Path(path)
    filename = filename or path.name
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    existing = session.scalars(
        select(Document).where(
            Document.project_id == project_id, Document.sha256 == sha256
        )
    ).first()
    if existing is not None:
        # Re-ingest never re-parses — the bytes are identical by definition.
        # But provenance describes where the file came from, not the file,
        # and a document first ingested without a date would otherwise carry
        # that gap forever. Backfill nulls only; never overwrite.
        for attribute, value in (
            ("source_url", source_url),
            ("retrieved_at", _as_datetime(retrieved_at)),
            ("doc_date", doc_date),
            ("filename", filename),
        ):
            if value and getattr(existing, attribute) in (None, ""):
                setattr(existing, attribute, value)
        session.flush()
        return existing

    document = Document(
        project_id=project_id,
        sha256=sha256,
        filename=filename,
        doc_type=doc_type,
        source_url=source_url,
        retrieved_at=_as_datetime(retrieved_at),
        doc_date=doc_date,
        pages=0,
        parse_status="pending",
    )
    session.add(document)
    session.flush()

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
                image_path=str(image_path),
                text_source=text_source,
            )
        )

    document.pages = len(pages)
    document.parse_status = "parsed"
    session.flush()
    return document


def _extract(path: Path, images_dir: Path) -> list[tuple[int, str, Path, str]]:
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
