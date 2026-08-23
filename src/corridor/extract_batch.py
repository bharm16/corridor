"""Batched, concurrent extraction shared by the document extractors.

Two structural choices, both load-bearing:

**Pages pool across documents, not within one.** A coordination meeting note
is often a single page, so a per-document pool would have nothing to
parallelise and the run would stay sequential in practice.

**Documents stay atomic even inside a pooled page run.** If one page for a
document fails, that document persists no Candidates from any of its pages
and retries as a whole next time. Clean sibling documents in the same pool
still commit.

Only the HTTP calls run concurrently. SQLAlchemy sessions are not
thread-safe, so every `session.add` happens back on the calling thread.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataclasses import dataclass

from corridor.admission import load_and_report
from corridor.extraction_runs import completed_document_ids, record_extraction_run
from corridor.llm import DEFAULT_WORKERS, complete_many
from corridor.models import Candidate, DocPage, Document


@dataclass(frozen=True)
class Noun:
    """What this extractor calls its documents and what it finds in them.

    The only thing that differed between the two runners besides the
    document type and the schema — "agreements"/"obligations" against
    "notes"/"events".
    """

    plural: str
    items: str


def already_extracted(
    session: Session, project_id: int, prompt_version: str
) -> set[int]:
    """Document ids that completed extraction at this prompt version.

    Candidate existence is not the seam: a completed document may validly
    yield zero candidates, and a legacy candidate row with no recorded
    completed attempt must not make resume skip it. The durable boundary is
    the document attempt itself.
    """
    return completed_document_ids(
        session, project_id, prompt_version=prompt_version
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
    prompt_version: str,
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
        results = []
        if work:
            users = [
                f"Page {page.page_no} of {doc.filename}:\n\n{(page.text or '').strip()}"
                for doc, page in work
            ]
            results = complete_many(
                client, system=system, schema=schema, users=users, max_workers=workers
            )

        per_document: dict[int, list[Candidate]] = {d.id: [] for d, _ in group}
        unreadable = {d.id for d, pages in group if not pages}
        errors: dict[int, int] = {
            d.id: 1 if not pages else 0 for d, pages in group
        }

        for (doc, page), completion in zip(work, results):
            if completion.failed:
                # This document will retry as a whole; keep counting siblings.
                errors[doc.id] += 1
                continue
            for item in completion.value.get(items_key) or []:
                candidate = to_candidate(doc, page, item, model)
                if candidate is not None:
                    per_document[doc.id].append(candidate)

        for doc, _ in group:
            batch = per_document[doc.id] if errors[doc.id] == 0 else []
            for candidate in batch:
                session.add(candidate)
            record_extraction_run(
                session,
                doc,
                prompt_version=prompt_version,
                candidate_count=len(batch),
                page_errors=errors[doc.id],
                outcome=(
                    "unreadable"
                    if doc.id in unreadable
                    else "failed"
                    if errors[doc.id]
                    else "completed"
                ),
                candidates=tuple(batch),
                model=model,
                schema_version=prompt_version,
                error_detail=(
                    "no eligible readable pages"
                    if doc.id in unreadable
                    else f"{errors[doc.id]} page extraction error(s)"
                    if errors[doc.id]
                    else None
                ),
            )
        session.flush()
        if commit:
            session.commit()

        for doc, _ in group:
            batch = per_document[doc.id] if errors[doc.id] == 0 else []
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


def run_extraction(
    argv: list[str],
    *,
    doc_type: str,
    default_slug: str,
    prompt_version: str,
    system: str,
    schema: dict,
    min_page_chars: int,
    to_candidate: Callable,
    items_key: str,
    noun: Noun,
    client_factory: Callable | None = None,
    session_factory: Callable | None = None,
) -> int:
    """Run a pooled extractor over a project or exact registered Documents.

    This was written twice, 96 lines each, differing on six: the slug
    default, the `doc_type` filter, the `items_key`, and the noun in three
    print strings. Neither copy had a test — both modules' suites drove a
    sequential `extract_document` that nothing in `src/` calls, so the
    resume filtering, the limit, the per-document tally, the error count
    and the `client.close()` in a `finally` were the untested half, and
    they are the half where the real bugs live.

    `client_factory` and `session_factory` exist so a test can drive the
    whole runner without an API key or a committed database. Two adapters
    justify each seam: the real client and the real session factory in
    production, a stub and a transaction-scoped session in the suite.

    Repeated ``--document-id`` arguments form an all-or-nothing selection.
    Every id is validated before the model client exists, so a missing,
    duplicated, cross-project, or wrong-type Document cannot widen the run.
    """
    import sys
    import time

    from corridor.db import Session as DefaultSessionFactory
    from corridor.llm import OpenAIClient
    from corridor.models import Project

    slug, limit, document_ids = _parse_runner_args(argv, default_slug=default_slug)
    if slug is None:
        return 1

    with (session_factory or DefaultSessionFactory)() as session:
        project = session.scalars(
            select(Project).where(Project.slug == slug)
        ).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        documents = _selected_documents(
            session,
            project_id=project.id,
            doc_type=doc_type,
            requested_ids=document_ids,
        )
        if documents is None:
            return 1
        if not document_ids:
            documents = session.scalars(
                select(Document)
                .where(Document.project_id == project.id, Document.doc_type == doc_type)
                .order_by(Document.doc_date)
            ).all()

        # Resume: a killed run leaves whole documents done, so skip those and
        # pick up where it stopped instead of duplicating their candidates.
        done = already_extracted(session, project.id, prompt_version)
        skipped = [d for d in documents if d.id in done]
        documents = [d for d in documents if d.id not in done]
        if limit:
            documents = documents[:limit]

        if not documents:
            print(
                f"nothing to do: all {len(skipped)} already extracted "
                f"at {prompt_version}"
            )
            return 0

        client = (client_factory or OpenAIClient)()
        started = time.time()
        print(
            f"{len(documents)} {noun.plural} at {client.max_workers}-way "
            f"concurrency, model {client.model}"
            + (f" ({len(skipped)} already done)" if skipped else ""),
            flush=True,
        )

        totals = {"n": 0, "ok": 0, "err": 0}

        def report(document, candidates, errors):
            ok = sum(1 for c in candidates if c.citations_verified)
            totals["n"] += len(candidates)
            totals["ok"] += ok
            totals["err"] += errors
            print(
                f"  {ok:>3}/{len(candidates):<3} verified"
                + (f"  {errors} page errors" if errors else "")
                + f"  {document.filename.split('/')[-1][:50]}",
                flush=True,
            )

        try:
            extract_documents(
                session,
                documents,
                client=client,
                system=system,
                schema=schema,
                min_page_chars=min_page_chars,
                to_candidate=to_candidate,
                items_key=items_key,
                on_document=report,
                prompt_version=prompt_version,
            )
        finally:
            client.close()

        n, ok = totals["n"], totals["ok"]
        pct = 100 * ok / n if n else 0.0
        elapsed = time.time() - started
        print(
            f"{n} {noun.items}, {ok} verified ({pct:.1f}%) in {elapsed:.0f}s"
            + (f", {totals['err']} pages failed" if totals["err"] else ""),
            flush=True,
        )
        print(
            f"tokens: {client.usage.prompt_tokens:,} in / "
            f"{client.usage.completion_tokens:,} out",
            flush=True,
        )
        print(load_and_report(session, project), flush=True)
    return 0


def _parse_runner_args(
    argv: list[str], *, default_slug: str
) -> tuple[str | None, int | None, list[int]]:
    import sys

    positional: list[str] = []
    document_ids: list[int] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--document-id":
            if index + 1 >= len(argv):
                print("--document-id requires an integer value", file=sys.stderr)
                return None, None, []
            try:
                document_ids.append(int(argv[index + 1]))
            except ValueError:
                print("--document-id must be an integer", file=sys.stderr)
                return None, None, []
            index += 2
            continue
        positional.append(token)
        index += 1

    if len(positional) > 2:
        print("usage: <slug> [limit] [--document-id <id> ...]", file=sys.stderr)
        return None, None, []

    slug = positional[0] if positional else default_slug
    if document_ids and len(positional) == 2:
        print("limit cannot be combined with explicit document selection", file=sys.stderr)
        return None, None, []
    if len(positional) == 2:
        try:
            limit = int(positional[1])
        except ValueError:
            print("limit must be an integer", file=sys.stderr)
            return None, None, []
    else:
        limit = None
    return slug, limit, document_ids


def _selected_documents(
    session: Session,
    *,
    project_id: int,
    doc_type: str,
    requested_ids: list[int],
) -> list[Document] | None:
    import sys

    if not requested_ids:
        return []

    duplicates = [
        document_id
        for document_id in requested_ids
        if requested_ids.count(document_id) > 1
    ]
    if duplicates:
        repeated = sorted(set(duplicates))
        print(
            "duplicate document selection is not allowed: "
            + ", ".join(str(document_id) for document_id in repeated),
            file=sys.stderr,
        )
        return None

    documents = {
        document.id: document
        for document in session.scalars(
            select(Document).where(Document.id.in_(requested_ids))
        ).all()
    }
    missing = [document_id for document_id in requested_ids if document_id not in documents]
    if missing:
        print(
            "selected documents do not exist: "
            + ", ".join(str(document_id) for document_id in missing),
            file=sys.stderr,
        )
        return None

    ordered: list[Document] = []
    for document_id in requested_ids:
        document = documents[document_id]
        if document.project_id != project_id:
            print(
                f"document {document_id} belongs to another project",
                file=sys.stderr,
            )
            return None
        if document.doc_type != doc_type:
            print(
                f"document {document_id} is {document.doc_type!r}, not {doc_type!r}",
                file=sys.stderr,
            )
            return None
        ordered.append(document)
    return ordered
