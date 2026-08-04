"""Raw file to pending candidates.

Extractors never write to the Ledger — they only create Candidates, and the
single path onward is a human keystroke in `corridor.adjudicate`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from corridor.docs import stored_file
from corridor.ingest import SPREADSHEET_SUFFIXES, ingest_document
from corridor.models import Candidate, Document


def ingest_manifest(
    session: Session,
    *,
    project_id: int,
    lock_path: Path | str,
    images_dir: Path | str,
) -> list[Document]:
    """Ingest every successfully fetched source in the manifest lockfile.

    Provenance flows straight through: the lockfile already knows where each
    file came from and when it was retrieved, and losing that at the ingest
    boundary would leave citations bottoming out at a path on disk.
    """
    lock = json.loads(Path(lock_path).read_text())
    documents = []

    for key, record in sorted(lock.get("sources", {}).items()):
        # A failed fetch has a null sha256 and nothing on disk. It stays
        # visible in the lockfile rather than being ingested as though it
        # had worked.
        if not record.get("sha256") or not record.get("local_path"):
            continue

        documents.append(
            ingest_document(
                session,
                project_id=project_id,
                path=record["local_path"],
                doc_type=record.get("doc_type", "other"),
                images_dir=images_dir,
                # The leaf path only: a nested member's full path drags the
                # inner zip's name into every citation. "Meeting Notes/Air
                # Liquide/2024.07.30 notes.pdf" is what a reader needs.
                filename=(record.get("member") or _basename(key)).split("::")[-1],
                source_url=record.get("archive_url") or key,
                retrieved_at=record.get("retrieved_at"),
                doc_date=parse_doc_date(record.get("doc_date")),
            )
        )
    return documents


def extract_any(session: Session, document: Document, *, client=None) -> list[Candidate]:
    """Read one matrix, whichever form it was published in (ADR-0005).

    Which reader runs is a property of the document rather than something a
    caller has to know: `make extract` reads a project, and a project may
    publish its matrix as a spreadsheet, as a printout of one, or as both.

    The two readers have deliberately different signatures and that is not
    an inconsistency to smooth over. The page path needs a model and takes
    a client; the native path needs neither, and giving it a parameter it
    would ignore would suggest a model is involved somewhere in reading a
    spreadsheet. It is not.
    """
    path = stored_file(document)
    if path is not None and Path(path).suffix.lower() in SPREADSHEET_SUFFIXES:
        from corridor.extract_sheet import extract_document as extract_sheet

        return extract_sheet(session, document)

    from corridor.extract_matrix import extract_document as extract_matrix

    return extract_matrix(session, document, client=client)


def _basename(url: str) -> str:
    return Path(urlparse(url.split("::", 1)[0]).path).name or "document"


def parse_doc_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def ingest_and_extract(
    session: Session,
    *,
    project_id: int,
    path: Path | str,
    images_dir: Path | str,
    client=None,
    filename: str | None = None,
    source_url: str | None = None,
    retrieved_at: str | None = None,
    doc_date: date | None = None,
    doc_type: str = "matrix",
) -> tuple[Document, list[Candidate]]:
    """Ingest one file and extract it, in that order.

    Extraction needs a model since #63 removed the deterministic parser, so
    a caller either injects a client or one is constructed — which means
    this path needs an API key where it used to need none. That is the
    price of having a single extraction path rather than a second one kept
    alive to avoid it.
    """
    document = ingest_document(
        session,
        project_id=project_id,
        path=path,
        doc_type=doc_type,
        images_dir=images_dir,
        filename=filename,
        source_url=source_url,
        retrieved_at=retrieved_at,
        doc_date=doc_date,
    )
    if document.parse_status != "parsed":
        return document, []

    # The same extractor `make extract` runs.
    from corridor.extract_matrix import extract_document

    return document, extract_document(session, document, client=client)
