"""Resolving documents back to the content-addressed store."""

from __future__ import annotations

from pathlib import Path

from corridor.config import settings
from corridor.models import Document


def stored_file(document: Document | None) -> Path | None:
    """Resolve a Document back to the file in the content-addressed store.

    Found by hash rather than by extension: the store preserves whatever
    suffix the source had so it stays browsable, and since ADR-0005 that is
    no longer always `.pdf`. One hash, one file — the name is the hash, so
    a glob cannot match two different documents.
    """
    if not document or not document.sha256:
        return None
    shard = Path(settings.corpus_store) / document.sha256[:2]
    return next(iter(sorted(shard.glob(f"{document.sha256}.*"))), None)


def staged_file(sha256: str | None) -> Path | None:
    """Resolve staged bytes back to the store by their hash, before any Document.

    Product intake writes exact bytes to the same content-addressed store keyed by
    their own hash *before* registration (``source_intake``), so a bounded
    read-only pass — a source-intake draft (#362) — can read the exact previewed
    bytes by hash without a registered Document. One hash, one file.
    """
    if not sha256:
        return None
    shard = Path(settings.corpus_store) / sha256[:2]
    return next(iter(sorted(shard.glob(f"{sha256}.*"))), None)


def stored_pdf(document: Document | None) -> Path | None:
    """The stored file, when it really is a PDF.

    Callers that render pages or read word boxes need this rather than
    `stored_file`: handing a workbook to PyMuPDF raises somewhere deep
    instead of saying the document is the wrong kind.
    """
    path = stored_file(document)
    return path if path and path.suffix.lower() == ".pdf" else None
