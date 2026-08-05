"""Batched, concurrent extraction shared by the document extractors.

Two structural choices, both load-bearing:

**Pages pool across documents, not within one.** A coordination meeting note
is often a single page, so a per-document pool would have nothing to
parallelise and the run would stay sequential in practice.

**Commits stay on document boundaries.** A killed run then leaves every
document either wholly extracted or not started, which is exactly the
invariant `already_extracted` relies on — resume by skipping documents that
already have candidates is only safe if "has candidates" means "finished".

Only the HTTP calls run concurrently. SQLAlchemy sessions are not
thread-safe, so every `session.add` happens back on the calling thread.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.llm import DEFAULT_WORKERS, complete_many
from corridor.models import Candidate, DocPage, Document


def already_extracted(
    session: Session, project_id: int, prompt_version: str
) -> set[int]:
    """Document ids that already have candidates from this prompt version.

    Without this a restarted run re-extracts everything already done and
    creates a second set of candidates for each — duplicates a reviewer then
    has to clear by hand.
    """
    return set(
        session.scalars(
            select(Candidate.source_document_id)
            .where(
                Candidate.project_id == project_id,
                Candidate.prompt_version == prompt_version,
            )
            .distinct()
        ).all()
    )


def eligible_pages(
    session: Session, document: Document, min_chars: int
) -> list[DocPage]:
    return [
        page
        for page in session.scalars(
            select(DocPage)
            .where(DocPage.document_id == document.id)
            .order_by(DocPage.page_no)
        )
        if len((page.text or "").strip()) >= min_chars
    ]


def extract_documents(
    session: Session,
    documents: list[Document],
    *,
    client,
    system: str,
    schema: dict,
    min_page_chars: int,
    to_candidate: Callable[[Document, DocPage, dict, str | None], Candidate | None],
    items_key: str,
    max_workers: int | None = None,
    on_document: Callable[[Document, list[Candidate], int], None] | None = None,
    commit: bool = True,
) -> list[Candidate]:
    workers = max_workers or getattr(client, "max_workers", DEFAULT_WORKERS)
    # Enough pages in flight to keep every worker busy without letting one
    # group grow so large that a crash loses much progress.
    target = workers * 4
    model = getattr(client, "model", None)

    created: list[Candidate] = []
    group: list[tuple[Document, list[DocPage]]] = []
    queued = 0

    def run(group):
        work = [(doc, page) for doc, pages in group for page in pages]
        users = [
            f"Page {page.page_no} of {doc.filename}:\n\n{(page.text or '').strip()}"
            for doc, page in work
        ]
        results = complete_many(
            client, system=system, schema=schema, users=users, max_workers=workers
        )

        per_document: dict[int, list[Candidate]] = {d.id: [] for d, _ in group}
        errors: dict[int, int] = {d.id: 0 for d, _ in group}

        for (doc, page), completion in zip(work, results):
            if completion.failed:
                # One page lost, reported, run continues.
                errors[doc.id] += 1
                continue
            for item in completion.value.get(items_key) or []:
                candidate = to_candidate(doc, page, item, model)
                if candidate is not None:
                    session.add(candidate)
                    per_document[doc.id].append(candidate)

        session.flush()
        if commit:
            session.commit()

        for doc, _ in group:
            batch = per_document[doc.id]
            created.extend(batch)
            if on_document:
                on_document(doc, batch, errors[doc.id])

    for document in documents:
        pages = eligible_pages(session, document, min_page_chars)
        group.append((document, pages))
        queued += len(pages)
        if queued >= target:
            run(group)
            group, queued = [], 0

    if group:
        run(group)

    return created
