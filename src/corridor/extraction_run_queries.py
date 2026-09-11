"""Read-only Extraction Run predicates, without authoring engines.

These canonical queries formerly lived with commands whose imports load PDF
and model-processing modules. Archived reference scoring needs the same
completion rule without importing those engines. Command callers keep their
existing API through extraction_runs re-exports; this is the sole owner of the
predicate, not a second measurement-specific copy.

``extractable_document`` arrived here for that same reason and not a new one
(#841). It is a pure question about one registered Document -- does any
extractor read this kind of source -- and it lived in ``extract_project``,
which imports ``corridor.pipeline`` and through it the whole extraction engine
stack. The source register has to ask it: a ``plan`` PDF or an ``other``
document is never handed to an extractor, so a register that read its state
from the runs alone would tell a coordinator it was *waiting for a processing
pass* that is never going to take it. Asking the question is not running the
engines, so the question moved to where a reader can ask it.
"""

from pathlib import Path

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from corridor.ingest import SPREADSHEET_SUFFIXES
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


def extractable_document(document: Document) -> bool:
    # An "email" Document is a routed inbound message body (#372, ADR-0058):
    # written evidence that flows through the ordinary prose statement path,
    # so the standing pass is its durable handoff too.
    if document.doc_type == "minutes":
        from sqlalchemy.orm import object_session
        from corridor.operating_mode import is_adopted_baseline

        attached = object_session(document)
        return attached is not None and is_adopted_baseline(attached, document.project_id)
    return document.doc_type in ("matrix", "email") or (
        document.doc_type == "plan"
        and Path(document.filename).suffix.lower() in SPREADSHEET_SUFFIXES
    )
