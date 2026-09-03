"""Resolving documents back to the content-addressed store."""

from __future__ import annotations

from pathlib import Path

from corridor.models import Document
from corridor.object_storage import content_store, local_staging_path


def stored_file(document: Document | None) -> Path | None:
    """Resolve a Document back to the file in the content-addressed store.

    Found by hash rather than by extension: the store preserves whatever
    suffix the source had so it stays browsable, and since ADR-0005 that is
    no longer always `.pdf`. One hash, one file — the name is the hash, so
    a glob cannot match two different documents. The path returned is the
    locally staged copy; with the object-store backend the store fills it
    on first use (ADR-0079).
    """
    if not document or not document.sha256:
        return None
    return staged_file(document.sha256)


def staged_file(sha256: str | None) -> Path | None:
    """Resolve staged bytes back to the store by their hash, before any Document.

    Product intake writes exact bytes to the same content-addressed store keyed by
    their own hash *before* registration (``source_intake``), so a bounded
    read-only pass — a source-intake draft (#362) — can read the exact previewed
    bytes by hash without a registered Document. One hash, one file.
    """
    if not sha256:
        return None
    store = content_store()
    key = store.resolve(sha256)
    if key is None:
        return None
    return store.stage(key, local_staging_path(key), sha256=sha256)


def stored_pdf(document: Document | None) -> Path | None:
    """The stored file, when it really is a PDF.

    Callers that render pages or read word boxes need this rather than
    `stored_file`: handing a workbook to PyMuPDF raises somewhere deep
    instead of saying the document is the wrong kind.
    """
    path = stored_file(document)
    return path if path and path.suffix.lower() == ".pdf" else None
