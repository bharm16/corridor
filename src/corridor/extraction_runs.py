"""Shared seams for completed extraction attempts.

Resume and evaluation both answer the same question: has this document
completed extraction at this prompt version? The answer is not "is there a
Candidate row" and it is not "did an attempt happen". It is the narrower
predicate below, and it lives in one place so the two readers cannot drift.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Document, ExtractionRun


def completion_predicate():
    """Which runs count as completed for resume/eval selection.

    Only a document with zero page failures is complete. A successful zero-row
    read still counts because it has `page_errors == 0`. A document with any
    failed page must retry as a whole, so its run is history, not completion,
    even if some pages appeared to yield candidates before the failure.
    """
    return ExtractionRun.page_errors == 0


def completed_document_ids(
    session: Session, project_id: int, *, prompt_version: str | None = None
) -> set[int]:
    query = (
        select(ExtractionRun.document_id)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project_id, completion_predicate())
    )
    if prompt_version is not None:
        query = query.where(ExtractionRun.prompt_version == prompt_version)
    return set(session.scalars(query.distinct()).all())


def record_extraction_run(
    session: Session,
    document: Document,
    *,
    prompt_version: str,
    candidate_count: int,
    page_errors: int,
) -> ExtractionRun:
    """Record one completed attempt in the same transaction as its results."""
    if not prompt_version:
        raise ValueError("prompt_version must be non-empty")
    run = ExtractionRun(
        document_id=document.id,
        prompt_version=prompt_version,
        candidate_count=candidate_count,
        page_errors=page_errors,
    )
    session.add(run)
    return run
