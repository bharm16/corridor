"""Read-only Extraction Run completion queries, without authoring engines.

These three canonical queries formerly lived with commands whose imports
load PDF and model-processing modules. Archived reference scoring needs the
same completion rule without importing those engines. Command callers keep
their existing API through extraction_runs re-exports; this is the sole owner
of the predicate, not a second measurement-specific copy.
"""

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from corridor.models import Document, ExtractionRun


def completion_predicate():
    """Which runs count as completed for resume/eval selection.

    Only a document with zero page failures is complete. A successful zero-row
    read still counts because it has `page_errors == 0`. A document with any
    failed page must retry as a whole, so its run is history, not completion,
    even if some pages appeared to yield candidates before the failure.
    """
    return and_(ExtractionRun.outcome == "completed", ExtractionRun.page_errors == 0)


def is_completed_run(run: ExtractionRun) -> bool:
    """Object-level form of the one extraction completion rule."""

    return run.outcome == "completed" and run.page_errors == 0


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
