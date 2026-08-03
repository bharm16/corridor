"""LLM extraction of events from utility coordination meeting notes.

These notes discuss conflicts that already exist in the matrix, so this
extractor emits **event candidates**, not new dependencies. A note saying
"PL41 — protect-in-place" is a fact about a dependency the ledger should
already hold; creating a second record for it is the duplicate-corruption
the merge search exists to prevent.

Per page, like the agreement extractor: the page number is ours, only the
quote comes from the model, and the quote is checked against the exact text
the model saw.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.llm import OpenAIClient, StructuredClient
from corridor.models import EVENT_TYPES, Candidate, DocPage, Document
from corridor.verify import quote_appears_on

PROMPT_VERSION = "minutes_v1"
PROMPT_PATH = Path("prompts/minutes_v1.md")

MIN_PAGE_CHARS = 200

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events"],
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "event_type",
                    "description",
                    "event_date",
                    "external_org",
                    "conflict_ref",
                    "station_from",
                    "station_to",
                    "committed_date",
                    "quote",
                    "confidence",
                ],
                "properties": {
                    "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
                    "description": {"type": "string"},
                    "event_date": {"type": ["string", "null"]},
                    "external_org": {"type": ["string", "null"]},
                    "conflict_ref": {"type": ["string", "null"]},
                    "station_from": {"type": ["string", "null"]},
                    "station_to": {"type": ["string", "null"]},
                    "committed_date": {"type": ["string", "null"]},
                    "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}


def extract_document(
    session: Session,
    document: Document,
    *,
    client: StructuredClient | None = None,
    max_pages: int | None = None,
) -> list[Candidate]:
    client = client or OpenAIClient()
    system = PROMPT_PATH.read_text()

    pages = session.scalars(
        select(DocPage)
        .where(DocPage.document_id == document.id)
        .order_by(DocPage.page_no)
    ).all()
    if max_pages:
        pages = pages[:max_pages]

    candidates: list[Candidate] = []
    for page in pages:
        text = (page.text or "").strip()
        if len(text) < MIN_PAGE_CHARS:
            continue

        result = client.complete(
            system=system,
            user=f"Page {page.page_no} of {document.filename}:\n\n{text}",
            schema=SCHEMA,
        )
        for item in result.get("events") or []:
            candidate = _to_candidate(
                document, page, item, getattr(client, "model", None)
            )
            if candidate is not None:
                session.add(candidate)
                candidates.append(candidate)

    session.flush()
    return candidates


def _to_candidate(
    document: Document, page: DocPage, item: dict, model: str | None
) -> Candidate | None:
    quote = (item.get("quote") or "").strip()
    event_type = item.get("event_type")
    if not quote or event_type not in EVENT_TYPES:
        return None

    verified = quote_appears_on(quote, page.text or "")

    fields = {
        key: value
        for key, value in (
            ("event_type", event_type),
            ("description", item.get("description")),
            ("event_date", item.get("event_date")),
            ("external_org", item.get("external_org")),
            ("conflict_ref", item.get("conflict_ref")),
            ("station_from", item.get("station_from")),
            ("station_to", item.get("station_to")),
            ("committed_date", item.get("committed_date")),
        )
        if value
    }
    if not fields.get("description"):
        return None

    return Candidate(
        project_id=document.project_id,
        # An event, not a dependency: these notes discuss conflicts that
        # already exist, and the adjudicator's job is to attach this to the
        # right one rather than create another.
        kind="event",
        payload_json={
            "kind": "event",
            "fields": fields,
            "citations": [
                {
                    "document_id": document.id,
                    "page": page.page_no,
                    "quote": quote,
                    "verified": verified,
                    "whole_row": False,
                }
            ],
            "confidence": item.get("confidence"),
            # The merge search blocks on the resolved party, then scores on
            # stationing — both of which these notes carry.
            "dedupe_hint": "|".join(
                [
                    fields.get("external_org", ""),
                    fields.get("conflict_ref", ""),
                    f"{fields.get('station_from', '')}-{fields.get('station_to', '')}",
                ]
            ),
            "text_source": page.text_source,
        },
        source_document_id=document.id,
        source_pages=[page.page_no],
        confidence=item.get("confidence"),
        prompt_version=PROMPT_VERSION,
        model=model,
        citations_verified=verified,
    )


def main(argv: list[str]) -> int:
    """`make minutes ARGS="<slug> [limit]"`"""
    import sys
    import time

    from corridor.db import Session as SessionFactory
    from corridor.extract_batch import already_extracted, extract_documents
    from corridor.models import Project

    slug = argv[0] if argv else "sh99-grand-parkway"
    limit = int(argv[1]) if len(argv) > 1 else None

    with SessionFactory() as session:
        project = session.scalars(
            select(Project).where(Project.slug == slug)
        ).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        documents = session.scalars(
            select(Document)
            .where(Document.project_id == project.id, Document.doc_type == "minutes")
            .order_by(Document.doc_date)
        ).all()

        # Resume: a killed run leaves whole documents done, so skip those and
        # pick up where it stopped instead of duplicating their candidates.
        done = already_extracted(session, project.id, PROMPT_VERSION)
        skipped = [d for d in documents if d.id in done]
        documents = [d for d in documents if d.id not in done]
        if limit:
            documents = documents[:limit]

        if not documents:
            print(f"nothing to do: all {len(skipped)} already extracted at {PROMPT_VERSION}")
            return 0

        client = OpenAIClient()
        started = time.time()
        print(
            f"{len(documents)} notes at {client.max_workers}-way concurrency, "
            f"model {client.model}"
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
                system=PROMPT_PATH.read_text(),
                schema=SCHEMA,
                min_page_chars=MIN_PAGE_CHARS,
                to_candidate=_to_candidate,
                items_key="events",
                on_document=report,
            )
        finally:
            client.close()

        n, ok = totals["n"], totals["ok"]
        pct = 100 * ok / n if n else 0.0
        elapsed = time.time() - started
        print(
            f"{n} events, {ok} verified ({pct:.1f}%) in {elapsed:.0f}s"
            + (f", {totals['err']} pages failed" if totals["err"] else ""),
            flush=True,
        )
        print(
            f"tokens: {client.usage.prompt_tokens:,} in / "
            f"{client.usage.completion_tokens:,} out",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
