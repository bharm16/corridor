"""Raw file to pending candidates.

Extractors never write to the Ledger — they only create Candidates, and the
single path onward is a human keystroke in `corridor.adjudicate`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.extract import PROMPT_VERSION, extract_rows, to_candidates
from corridor.ingest import ingest_document
from corridor.models import Candidate, DocPage, Document


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
    filename: str | None = None,
    source_url: str | None = None,
    retrieved_at: str | None = None,
    doc_date: date | None = None,
    doc_type: str = "matrix",
) -> tuple[Document, list[Candidate]]:
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

    # Verify against the *stored* page text, which is what evidence display
    # will show. Verifying against a freshly re-extracted copy could pass
    # here and fail in the UI.
    page_text = {
        page.page_no: page.text
        for page in session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        )
    }

    payloads = to_candidates(
        extract_rows(path), document_id=document.id, page_text=page_text
    )

    candidates = []
    for payload in payloads:
        citations = payload["citations"]
        candidate = Candidate(
            project_id=project_id,
            kind=payload["kind"],
            payload_json=payload,
            source_document_id=document.id,
            source_pages=sorted({c["page"] for c in citations}),
            confidence=payload["confidence"],
            prompt_version=PROMPT_VERSION,
            # Deterministic extractor: no model involved, and recording that
            # honestly matters when eval compares runs.
            model=None,
            citations_verified=all(c["verified"] for c in citations),
        )
        session.add(candidate)
        candidates.append(candidate)

    session.flush()
    return document, candidates
