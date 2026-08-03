"""Raw file to pending candidates.

Extractors never write to the Ledger — they only create Candidates, and the
single path onward is a human keystroke in `corridor.adjudicate`.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.extract import PROMPT_VERSION, extract_rows, to_candidates
from corridor.ingest import ingest_document
from corridor.models import Candidate, DocPage, Document


def ingest_and_extract(
    session: Session,
    *,
    project_id: int,
    path: Path | str,
    images_dir: Path | str,
    filename: str | None = None,
    source_url: str | None = None,
    retrieved_at: str | None = None,
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
