"""Authoritative lineage for immutable extraction attempts.

Receipts, Candidate attachment, resume selection, and Active Run declaration
meet here.  Keeping those operations together prevents a reader from silently
reconstructing lineage from candidate existence, timestamps, or row order.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
import sys

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session, aliased

from corridor.models import (
    EXTRACTION_OUTCOMES,
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Candidate,
    Document,
    ExtractionRun,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project


def completion_predicate():
    """Which runs count as completed for resume/eval selection.

    Only a document with zero page failures is complete. A successful zero-row
    read still counts because it has `page_errors == 0`. A document with any
    failed page must retry as a whole, so its run is history, not completion,
    even if some pages appeared to yield candidates before the failure.
    """
    return and_(
        ExtractionRun.outcome == "completed", ExtractionRun.page_errors == 0
    )


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


def record_extraction_run(
    session: Session,
    document: Document,
    *,
    prompt_version: str,
    candidate_count: int,
    page_errors: int,
    outcome: str = "completed",
    candidates: Sequence[Candidate] = (),
    model: str | None = None,
    schema_version: str | None = None,
    error_detail: str | None = None,
) -> ExtractionRun:
    """Append one terminal attempt and attach every Candidate it produced."""
    if not prompt_version:
        raise ValueError("prompt_version must be non-empty")
    if outcome not in EXTRACTION_OUTCOMES:
        raise ValueError(f"unknown extraction outcome {outcome!r}")
    if candidate_count < 0 or page_errors < 0:
        raise ValueError("candidate_count and page_errors must be non-negative")
    if outcome == "completed" and page_errors:
        raise ValueError("a completed extraction run cannot have page errors")
    if outcome != "completed" and (candidate_count or candidates):
        raise ValueError("a non-completed extraction run cannot own Candidates")
    if outcome != "completed" and page_errors == 0:
        raise ValueError("a non-completed extraction run must record an error")
    if candidate_count != len(candidates):
        raise ValueError("candidate_count does not match the attached candidates")
    if len({id(candidate) for candidate in candidates}) != len(candidates):
        raise ValueError("an Extraction Run cannot repeat a Candidate")
    for candidate in candidates:
        if candidate.extraction_run_id is not None:
            raise ValueError(
                f"Candidate {candidate.id} already belongs to Extraction Run "
                f"{candidate.extraction_run_id}"
            )
        if candidate.source_document_id != document.id:
            raise ValueError("a run cannot own another document's Candidate")
        if candidate.project_id != document.project_id:
            raise ValueError("a run cannot own another project's Candidate")
        if candidate.prompt_version != prompt_version:
            raise ValueError("Candidate prompt_version does not match its run")
        if candidate.model != model:
            raise ValueError("Candidate model does not match its run")

    if candidates:
        session.add_all(candidates)
        # Candidate ids are part of the immutable input identity. They can be
        # assigned before the run because lineage is nullable only during this
        # one in-transaction construction step.
        session.flush(list(candidates))
        candidate_ids = [candidate.id for candidate in candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("an Extraction Run cannot repeat a Candidate")
    candidate_inputs = [
        candidate_input_snapshot(candidate) for candidate in candidates
    ]

    run = ExtractionRun(
        document_id=document.id,
        prompt_version=prompt_version,
        outcome=outcome,
        candidate_count=candidate_count,
        page_errors=page_errors,
        model=model,
        schema_version=schema_version,
        error_detail=error_detail,
        candidate_inputs_json=candidate_inputs,
    )
    session.add(run)
    session.flush([run])
    for candidate in candidates:
        candidate.extraction_run_id = run.id
    return run


def candidate_input_snapshot(candidate: Candidate) -> dict:
    """The immutable extractor-time input a run owns.

    ``extraction_run_id`` is intentionally absent: the containing
    ExtractionRun supplies that identity and does not exist until after this
    snapshot is assembled. Every other value the comparison may later need is
    copied before Adjudication can edit the live Candidate.
    """

    return {
        "candidate_id": candidate.id,
        "project_id": candidate.project_id,
        "kind": candidate.kind,
        "source_document_id": candidate.source_document_id,
        "payload_json": deepcopy(candidate.payload_json),
        "source_pages": list(candidate.source_pages or []),
        "confidence": candidate.confidence,
        "prompt_version": candidate.prompt_version,
        "model": candidate.model,
        "citations_verified": candidate.citations_verified,
        "state": candidate.state,
    }


def declare_active_run(
    session: Session,
    document_id: int,
    extraction_run_id: int,
    *,
    principal: HumanPrincipal,
) -> ExtractionRun:
    """Declare a completed run operative; never infer one from recency.

    The declaration is a human act at Admission's bar: the declarer is
    recorded, declarations append rather than overwrite, and re-declaring
    the run already current records nothing new — an identical rerun does
    not duplicate an outcome. ``active_extraction_runs`` stays the one-row
    projection readers join, maintained to equal the chain tail.
    """
    declarer = require_human_principal(principal)
    project_id = session.scalar(
        select(Document.project_id).where(Document.id == document_id)
    )
    if project_id is None:
        raise ValueError("document does not exist")
    lock_project(session, project_id)

    run = session.scalar(
        select(ExtractionRun).where(
            ExtractionRun.id == extraction_run_id,
            ExtractionRun.document_id == document_id,
        )
    )
    if run is None:
        raise ValueError("extraction run does not belong to the document")
    if run.outcome != "completed" or run.page_errors != 0:
        raise ValueError("only a completed extraction run can be active")

    current = session.get(
        ActiveExtractionRun,
        document_id,
        populate_existing=True,
    )
    tail = current_active_run_declaration(session, document_id)
    current_run_id = current.extraction_run_id if current is not None else None
    tail_run_id = tail.extraction_run_id if tail is not None else None
    if current_run_id != tail_run_id:
        raise ValueError(
            "the active run projection diverged from its declaration history"
        )
    if tail_run_id == extraction_run_id:
        return run

    session.add(
        ActiveRunDeclaration(
            document_id=document_id,
            extraction_run_id=extraction_run_id,
            declared_by=declarer.subject,
            predecessor_declaration_id=tail.id if tail is not None else None,
        )
    )
    if current is None:
        session.add(
            ActiveExtractionRun(
                document_id=document_id, extraction_run_id=extraction_run_id
            )
        )
    else:
        session.execute(
            update(ActiveExtractionRun)
            .where(ActiveExtractionRun.document_id == document_id)
            .values(extraction_run_id=extraction_run_id, declared_at=func.now())
        )
        session.expire(current)
    return run


def current_active_run_declaration(
    session: Session, document_id: int
) -> ActiveRunDeclaration | None:
    """The declaration no later declaration has superseded.

    A chain fact: the tail is the row nothing names as its predecessor,
    never the greatest id or the newest timestamp.
    """
    successor = aliased(ActiveRunDeclaration)
    return session.scalar(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == document_id,
            ~select(successor.id)
            .where(successor.predecessor_declaration_id == ActiveRunDeclaration.id)
            .exists(),
        )
    )


def active_run_for_document(
    session: Session, document_id: int
) -> ExtractionRun | None:
    """Return only the explicitly declared Active Run for a document."""
    return session.scalar(
        select(ExtractionRun)
        .join(
            ActiveExtractionRun,
            ActiveExtractionRun.extraction_run_id == ExtractionRun.id,
        )
        .where(ActiveExtractionRun.document_id == document_id)
    )


def main(argv: list[str], *, session_factory=None) -> int:
    """Declare an Active Run from explicit document and run identifiers.

    The declarer is the deployment-resolved principal, exactly as the web
    ingress resolves it — never free text from the command line.
    """
    if len(argv) != 2:
        print("usage: active-run <document-id> <extraction-run-id>", file=sys.stderr)
        return 2
    try:
        document_id, extraction_run_id = (int(value) for value in argv)
    except ValueError:
        print("document-id and extraction-run-id must be integers", file=sys.stderr)
        return 2
    if document_id <= 0 or extraction_run_id <= 0:
        print("document-id and extraction-run-id must be positive", file=sys.stderr)
        return 2

    from corridor.config import settings
    from corridor.principals import HumanPrincipal, InvalidHumanPrincipal

    try:
        principal = HumanPrincipal(settings.human_principal)
    except InvalidHumanPrincipal:
        print(
            "declaring an Active Run is an attributable act: set "
            "CORRIDOR_HUMAN_PRINCIPAL to a namespaced subject such as "
            "'local:alice'",
            file=sys.stderr,
        )
        return 2

    if session_factory is None:
        from corridor.db import Session as session_factory

    with session_factory() as session:
        try:
            run = declare_active_run(
                session, document_id, extraction_run_id, principal=principal
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        run_summary = (run.id, run.prompt_version, run.outcome)
        session.commit()
    run_id, prompt_version, outcome = run_summary
    print(
        f"document {document_id}: Active Run {run_id} "
        f"({prompt_version}, {outcome}) declared by {principal.subject}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
