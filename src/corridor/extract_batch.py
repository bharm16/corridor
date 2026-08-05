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

from dataclasses import dataclass

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
    noun: str,
    client_factory: Callable | None = None,
    session_factory: Callable | None = None,
) -> int:
    """`make <command> ARGS="<slug> [limit]"` for a pooled extractor.

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
    """
    import sys
    import time

    from corridor.db import Session as DefaultSessionFactory
    from corridor.llm import OpenAIClient
    from corridor.models import Project

    slug = argv[0] if argv else default_slug
    limit = int(argv[1]) if len(argv) > 1 else None

    with (session_factory or DefaultSessionFactory)() as session:
        project = session.scalars(
            select(Project).where(Project.slug == slug)
        ).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

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
    return 0
