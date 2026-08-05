"""Declared document lineage and the Candidate scope derived from it.

Supersession is registry metadata, not a guess made from filenames or dates.
This module owns both sides of that fact: an atomic registration boundary and
the exact Candidate query that reviewer reads and writes share. Keeping them
together prevents a UI that looks fail-closed while a direct form post can
still adjudicate a historical row (ADR-0015, ADR-0019).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from corridor.models import ActiveExtractionRun, Candidate, DocPage, Document
from corridor.project_lock import lock_project


@dataclass(frozen=True)
class SupersessionDeclaration:
    """One authority-declared edge between registered documents."""

    predecessor_registry_id: str
    successor_registry_id: str
    replacement_date: date
    source_registry_id: str
    source_page: int


def register_supersessions(
    session: Session,
    declarations: Iterable[SupersessionDeclaration],
    *,
    project_id: int | None = None,
) -> tuple[Document, ...]:
    """Validate a complete declaration set before writing any registry edge.

    ``project_id`` is explicit at manifest ingest. Interactive callers may
    omit it when the predecessor identifiers unambiguously name one project.
    The savepoint makes the no-partial-write promise hold even if a database
    constraint catches something after validation.
    """

    declared = tuple(declarations)
    if not declared:
        return ()

    with session.begin_nested():
        _validate_shapes(declared)
        resolved_project_id = _resolve_project_id(
            session, declared, project_id=project_id
        )
        lock_project(session, resolved_project_id)
        documents = _documents_by_registry_id(
            session, declared, project_id=resolved_project_id
        )
        edges = _validated_edges(
            session,
            declared,
            documents,
            project_id=resolved_project_id,
        )

        changed: list[Document] = []
        for predecessor, successor, source, declaration in edges:
            if _matches_existing(predecessor, successor, source, declaration):
                continue
            predecessor.superseded_by = successor.id
            predecessor.superseded_on = declaration.replacement_date
            predecessor.supersession_source_document_id = source.id
            predecessor.supersession_source_page = declaration.source_page
            changed.append(predecessor)

        if changed:
            session.flush(changed)
        return tuple(edge[0] for edge in edges)


def actionable_candidate_query(
    project_id: int, *, historical_document_id: int | None = None
):
    """Pending Candidates from exact declared Active Runs only.

    Default scope includes every current document in the project. A declared
    successor therefore hides its predecessor immediately, even before the
    successor has an Active Run. Historical access is an explicit exact-
    document override and still respects that document's declared Active Run.
    """

    query = (
        select(Candidate)
        .join(Document, Document.id == Candidate.source_document_id)
        .join(
            ActiveExtractionRun,
            and_(
                ActiveExtractionRun.document_id == Document.id,
                ActiveExtractionRun.extraction_run_id == Candidate.extraction_run_id,
            ),
        )
        .where(
            Candidate.project_id == project_id,
            Document.project_id == project_id,
            Candidate.state == "pending",
        )
    )
    if historical_document_id is None:
        return query.where(Document.superseded_by.is_(None))
    return query.where(
        Document.id == historical_document_id,
        Document.superseded_by.is_not(None),
    )


def actionable_candidate(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    historical_document_id: int | None = None,
) -> Candidate | None:
    """Resolve one Candidate through the same scope used by queue reads."""

    return session.scalars(
        actionable_candidate_query(
            project_id, historical_document_id=historical_document_id
        ).where(Candidate.id == candidate_id)
    ).first()


def actionable_candidate_for_update(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    historical_document_id: int | None = None,
) -> Candidate | None:
    """Lock the project before resolving a Candidate for a mutation.

    Registration and Active Run declaration take the same lock. Whichever
    transaction wins establishes the scope that the other must re-read.
    """

    lock_project(session, project_id)
    return actionable_candidate(
        session,
        project_id,
        candidate_id,
        historical_document_id=historical_document_id,
    )


def _validate_shapes(declarations: tuple[SupersessionDeclaration, ...]) -> None:
    for declaration in declarations:
        for name in (
            "predecessor_registry_id",
            "successor_registry_id",
            "source_registry_id",
        ):
            value = getattr(declaration, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(declaration.replacement_date, date):
            raise ValueError("replacement_date must be a date")
        if (
            isinstance(declaration.source_page, bool)
            or not isinstance(declaration.source_page, int)
            or declaration.source_page <= 0
        ):
            raise ValueError("source_page must be a positive integer")
        if declaration.predecessor_registry_id == declaration.successor_registry_id:
            raise ValueError("a document cannot supersede itself")


def _resolve_project_id(
    session: Session,
    declarations: tuple[SupersessionDeclaration, ...],
    *,
    project_id: int | None,
) -> int:
    if project_id is not None:
        return project_id

    predecessor_ids = {
        declaration.predecessor_registry_id for declaration in declarations
    }
    matches = session.execute(
        select(Document.registry_id, Document.project_id).where(
            Document.registry_id.in_(predecessor_ids)
        )
    ).all()
    by_registry_id: dict[str, list[int]] = {}
    for registry_id, matched_project_id in matches:
        by_registry_id.setdefault(registry_id, []).append(matched_project_id)

    projects: set[int] = set()
    for registry_id in predecessor_ids:
        candidates = by_registry_id.get(registry_id, [])
        if not candidates:
            raise ValueError(f"predecessor {registry_id!r} does not exist")
        if len(candidates) > 1:
            raise ValueError(
                f"predecessor {registry_id!r} is ambiguous; project_id is required"
            )
        projects.add(candidates[0])
    if len(projects) != 1:
        raise ValueError("a declaration set cannot cross project boundaries")
    return projects.pop()


def _documents_by_registry_id(
    session: Session,
    declarations: tuple[SupersessionDeclaration, ...],
    *,
    project_id: int,
) -> dict[str, Document]:
    registry_ids = {
        registry_id
        for declaration in declarations
        for registry_id in (
            declaration.predecessor_registry_id,
            declaration.successor_registry_id,
            declaration.source_registry_id,
        )
    }
    matches = session.scalars(
        select(Document)
        .where(
            Document.project_id == project_id,
            Document.registry_id.in_(registry_ids),
        )
        .execution_options(populate_existing=True)
    ).all()
    documents = {document.registry_id: document for document in matches}

    missing = registry_ids - documents.keys()
    if missing:
        foreign = set(
            session.scalars(
                select(Document.registry_id).where(
                    Document.project_id != project_id,
                    Document.registry_id.in_(missing),
                )
            ).all()
        )
        if foreign:
            names = ", ".join(sorted(foreign))
            raise ValueError(f"supersession cannot cross projects: {names}")
        names = ", ".join(sorted(missing))
        raise ValueError(f"registered document target or source is missing: {names}")
    return documents


def _validated_edges(
    session: Session,
    declarations: tuple[SupersessionDeclaration, ...],
    documents: dict[str, Document],
    *,
    project_id: int,
) -> list[tuple[Document, Document, Document, SupersessionDeclaration]]:
    by_predecessor: dict[
        int, tuple[Document, Document, Document, SupersessionDeclaration]
    ] = {}
    for declaration in declarations:
        predecessor = documents[declaration.predecessor_registry_id]
        successor = documents[declaration.successor_registry_id]
        source = documents[declaration.source_registry_id]
        edge = (predecessor, successor, source, declaration)

        prior = by_predecessor.get(predecessor.id)
        if prior is not None and prior[3] != declaration:
            raise ValueError(
                f"predecessor {predecessor.registry_id!r} has conflicting declarations"
            )
        by_predecessor[predecessor.id] = edge

        source_page = session.scalar(
            select(DocPage.id).where(
                DocPage.document_id == source.id,
                DocPage.page_no == declaration.source_page,
            )
        )
        if source_page is None:
            raise ValueError(
                f"source page {source.registry_id!r} p.{declaration.source_page} "
                "does not exist"
            )
        if predecessor.superseded_by is not None and not _matches_existing(
            predecessor, successor, source, declaration
        ):
            raise ValueError(
                f"predecessor {predecessor.registry_id!r} already has a "
                "conflicting supersession"
            )

    project_documents = session.scalars(
        select(Document)
        .where(Document.project_id == project_id)
        .execution_options(populate_existing=True)
    ).all()
    project_ids = {document.id for document in project_documents}
    graph = {
        document.id: document.superseded_by
        for document in project_documents
        if document.superseded_by is not None
    }
    for predecessor, successor, _, _ in by_predecessor.values():
        graph[predecessor.id] = successor.id
    if any(target not in project_ids for target in graph.values()):
        raise ValueError("an existing supersession crosses project boundaries")
    _reject_cycles(graph)
    return list(by_predecessor.values())


def _matches_existing(
    predecessor: Document,
    successor: Document,
    source: Document,
    declaration: SupersessionDeclaration,
) -> bool:
    return (
        predecessor.superseded_by == successor.id
        and predecessor.superseded_on == declaration.replacement_date
        and predecessor.supersession_source_document_id == source.id
        and predecessor.supersession_source_page == declaration.source_page
    )


def _reject_cycles(graph: dict[int, int]) -> None:
    for start in graph:
        seen: set[int] = set()
        current = start
        while current in graph:
            if current in seen:
                raise ValueError("supersession declarations contain a cycle")
            seen.add(current)
            current = graph[current]
