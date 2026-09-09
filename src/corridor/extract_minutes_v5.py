"""Stable Meeting Minutes extraction with deterministic Action Items.

Minutes v4 made each returned Candidate Evidence-bound, but it still let the
model decide whether a numbered Action Item existed.  The same registered page
therefore produced a closure-only run and a commitment-plus-closure run under
one exact configuration.  V5 removes that membership decision from the model:
numbered Action Items are enumerated from exact page text, conservatively
classified by deterministic wording, and replace any model rows drawn from the
same source spans.  The model remains useful for attributable statements
outside the Action Items block.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import extract_minutes_v4 as v4
from corridor.llm import OpenAIClient, StructuredClient
from corridor.models import Candidate, DocPage, Document
from corridor.prose_spans import NumberedActionSpan, numbered_action_spans
from corridor.statement_timing_parser import exact_statement_timings as _exact_timings
from corridor.verify import literal_quote_on_page


PROMPT_VERSION = "minutes_v5"
PROMPT_PATH = Path("prompts/minutes_v5.md")
MIN_PAGE_CHARS = v4.MIN_PAGE_CHARS
EXTERNAL_PARTY_STATEMENT_TYPES = v4.EXTERNAL_PARTY_STATEMENT_TYPES
TIMING_SCHEMA = v4.TIMING_SCHEMA
SCHEMA = v4.SCHEMA

_DATE_CHANGE_SIGNAL = re.compile(
    r"\b(?:changed?|changes?|moved?|moves?|shifted?|shifts?|revised?|revises?|"
    r"rescheduled?|reschedules?|updated?|updates?|extended?|extends?|"
    r"instead\s+of|rather\s+than|from)\b",
    re.IGNORECASE,
)




def extract_document(
    session: Session,
    document: Document,
    *,
    client: StructuredClient | None = None,
    max_pages: int | None = None,
) -> list[Candidate]:
    """Extract v5 Candidates from one Document without model membership drift."""

    client = client or OpenAIClient()
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
            system=PROMPT_PATH.read_text(),
            user=f"Page {page.page_no} of {document.filename}:\n\n{text}",
            schema=SCHEMA,
        )
        page_batch = extract_page_candidates(
            document,
            page,
            list(result.get("events") or []),
            getattr(client, "model", None),
        )
        for candidate in page_batch:
            session.add(candidate)
        candidates.extend(page_batch)
    session.flush()
    return candidates


def extract_page_candidates(
    document: Document,
    page: DocPage,
    model_items: list[dict],
    model: str | None,
) -> list[Candidate]:
    """Replace model membership inside Action Items with exact enumeration."""

    action_items = _action_items(page.text or "")
    created: list[Candidate] = []

    for action_item in action_items:
        candidate = _action_item_candidate(
            document,
            page,
            action_item.exact_text,
            model,
        )
        if candidate is not None:
            created.append(candidate)

    for item in model_items:
        if _item_drawn_from_action_items(item, action_items, page.text or ""):
            continue
        candidate = to_candidate(document, page, item, model)
        if candidate is not None:
            created.append(candidate)

    return _deduplicate(created)


def to_candidate(
    document: Document,
    page: DocPage,
    item: dict,
    model: str | None,
) -> Candidate | None:
    """Apply the v4 Evidence-bound converter under the new v5 lineage."""

    candidate = v4.to_candidate(document, page, item, model)
    if candidate is not None:
        candidate.prompt_version = PROMPT_VERSION
    return candidate


def _action_items(text: str) -> tuple[NumberedActionSpan, ...]:
    """Enumerate numbered action rows as contiguous, exact source quotes."""

    return numbered_action_spans(text)


def _item_drawn_from_action_items(
    item: dict,
    action_items: tuple[NumberedActionSpan, ...],
    page_text: str,
) -> bool:
    """Recognize exact model spans wholly or partly drawn from Action Items."""

    rendered = str(item.get("quote") or "").strip()
    literal = literal_quote_on_page(rendered, page_text) if rendered else None
    if literal is None:
        return False
    occurrences = tuple(
        (match.start(), match.end())
        for match in re.finditer(re.escape(literal), page_text)
    )
    return bool(occurrences) and all(
        any(
            start < action_item.end and action_item.start < end
            for action_item in action_items
        )
        for start, end in occurrences
    )


def _action_item_candidate(
    document: Document,
    page: DocPage,
    quote: str,
    model: str | None,
) -> Candidate | None:
    """Conservatively classify one exact Action Item without model judgment."""

    document_party = v4._document_party(document)
    if not document_party or not _document_party_is_action_actor(
        document_party, quote
    ):
        return None

    timings = _exact_timings(quote)
    if v4._evidence_states_completion(quote):
        event_type = "closure"
        committed_date = None
        previous_timing = None
    elif len(timings) == 1:
        event_type = "commitment"
        committed_date = timings[0].value
        previous_timing = None
    elif len(timings) >= 2 and _DATE_CHANGE_SIGNAL.search(quote):
        event_type = "committed_date_change"
        previous_timing = timings[0].value
        committed_date = timings[-1].value
    else:
        return None

    return to_candidate(
        document,
        page,
        {
            "event_type": event_type,
            "external_org": document_party,
            "stated_party": document_party,
            "conflict_ref": None,
            "committed_date": committed_date,
            "previous_timing": previous_timing,
            "quote": quote,
            "confidence": None,
        },
        model,
    )


def _document_party_is_action_actor(document_party: str, quote: str) -> bool:
    """Require the registered party in the row's explicit actor position."""

    actor = re.escape(document_party.strip())
    return bool(
        re.match(
            rf"{actor}\s+(?:to\b|will\b|shall\b|agrees?\b|commits?\b|"
            r"has\b|have\b|had\b|is\b|are\b|was\b|were\b|provided\b|"
            r"delivered\b|completed\b|closed\b|cleared\b|changed?\b|"
            r"moved?\b|shifted?\b|revised?\b|rescheduled?\b|updated?\b|"
            r"extended?\b)",
            quote,
            re.IGNORECASE,
        )
    )








def _deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    """Keep first source-order occurrence of one exact Candidate meaning."""

    unique: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        semantic_payload = {
            key: value
            for key, value in candidate.payload_json.items()
            if key != "confidence"
        }
        signature = json.dumps(
            semantic_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(candidate)
    return unique


def main(argv: list[str]) -> int:
    from corridor.extract_batch import Noun, run_extraction

    return run_extraction(
        argv,
        doc_type="minutes",
        default_slug="sh99-grand-parkway",
        prompt_version=PROMPT_VERSION,
        system=PROMPT_PATH.read_text(),
        schema=SCHEMA,
        min_page_chars=MIN_PAGE_CHARS,
        to_candidate=to_candidate,
        page_candidates=extract_page_candidates,
        items_key="events",
        noun=Noun("notes", "External Party Statements"),
        extractor_registry_key="minutes",
    )


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
