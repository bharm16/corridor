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

from corridor.candidates import dedupe_hint, propose
from corridor.llm import OpenAIClient, StructuredClient
from corridor.models import EVENT_TYPES, Candidate, DocPage, Document
from corridor.verify import quote_appears_on

PROMPT_VERSION = "minutes_v2"
PROMPT_PATH = Path("prompts/minutes_v2.md")

MIN_PAGE_CHARS = 200

TIMING_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["text", "precision", "start_date", "end_date"],
    "properties": {
        "text": {"type": "string"},
        "precision": {"type": "string", "enum": ["day", "month", "approximate"]},
        "start_date": {"type": ["string", "null"]},
        "end_date": {"type": ["string", "null"]},
    },
}

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
                    "stated_party",
                    "conflict_ref",
                    "station_from",
                    "station_to",
                    "committed_date",
                    "previous_timing",
                    "quote",
                    "confidence",
                ],
                "properties": {
                    "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
                    "description": {"type": "string"},
                    "event_date": {"type": ["string", "null"]},
                    "external_org": {"type": ["string", "null"]},
                    "stated_party": {"type": ["string", "null"]},
                    "conflict_ref": {"type": ["string", "null"]},
                    "station_from": {"type": ["string", "null"]},
                    "station_to": {"type": ["string", "null"]},
                    "committed_date": TIMING_SCHEMA,
                    "previous_timing": TIMING_SCHEMA,
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
            ("stated_party", item.get("stated_party")),
            ("conflict_ref", item.get("conflict_ref")),
            ("station_from", item.get("station_from")),
            ("station_to", item.get("station_to")),
            ("committed_date", item.get("committed_date")),
            ("previous_timing", item.get("previous_timing")),
        )
        if value
    }
    if not fields.get("description"):
        return None

    return propose(
        document,
        # An event, not a dependency: these notes discuss conflicts that
        # already exist, and the adjudicator's job is to attach this to the
        # right one rather than create another.
        kind="event",
        fields=fields,
        page_no=page.page_no,
        quote=quote,
        quote_verified=verified,
        whole_row=False,
        confidence=item.get("confidence"),
        prompt_version=PROMPT_VERSION,
        model=model,
        # The merge search blocks on the resolved party, then scores on
        # stationing — both of which these notes carry.
        dedupe=dedupe_hint(
            fields.get("external_org", ""),
            fields.get("conflict_ref", ""),
            f"{fields.get('station_from', '')}-{fields.get('station_to', '')}",
        ),
        text_source=page.text_source,
    )


def main(argv: list[str]) -> int:
    """`make minutes ARGS="<slug> [limit]"`"""
    from corridor.extract_batch import Noun, run_extraction

    return run_extraction(
        argv,
        doc_type="minutes",
        default_slug="sh99-grand-parkway",
        prompt_version=PROMPT_VERSION,
        system=PROMPT_PATH.read_text(),
        schema=SCHEMA,
        min_page_chars=MIN_PAGE_CHARS,
        to_candidate=_to_candidate,
        items_key="events",
        noun=Noun("notes", "events"),
    )


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
