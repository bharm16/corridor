"""Stable External Party Statement extraction from Meeting Minutes.

The model identifies exact source spans.  Registered Document context and
deterministic validation establish Candidate meaning.  General responses and
status observations remain in the source Document; this extractor emits only
the External Party Statement types the Project Record can preserve.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.candidates import dedupe_hint, propose
from corridor.llm import OpenAIClient, StructuredClient
from corridor.models import Candidate, DocPage, Document
from corridor.verify import literal_quote_on_page, normalize


PROMPT_VERSION = "minutes_v4"
PROMPT_PATH = Path("prompts/minutes_v4.md")
MIN_PAGE_CHARS = 200
EXTERNAL_PARTY_STATEMENT_TYPES = (
    "commitment",
    "committed_date_change",
    "closure",
)
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
                    "external_org",
                    "stated_party",
                    "conflict_ref",
                    "committed_date",
                    "previous_timing",
                    "quote",
                    "confidence",
                ],
                "properties": {
                    "event_type": {
                        "type": "string",
                        "enum": list(EXTERNAL_PARTY_STATEMENT_TYPES),
                    },
                    "external_org": {"type": ["string", "null"]},
                    "stated_party": {"type": ["string", "null"]},
                    "conflict_ref": {"type": ["string", "null"]},
                    "committed_date": TIMING_SCHEMA,
                    "previous_timing": TIMING_SCHEMA,
                    "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}

_COMPLETION = re.compile(
    r"\b(?:complete|completed|closed|cleared|delivered)\b", re.IGNORECASE
)
_COMPLETION_FUTURE = re.compile(
    r"(?:\bwill\b|\bshall\b|\bto\b|\bexpected\s+to\b|"
    r"\bscheduled\s+to\b)(?:\s+[A-Za-z'-]+){0,4}\s*$",
    re.IGNORECASE,
)
_COMPLETION_NEGATION = re.compile(
    r"(?:\bnot\b|\bnever\b|\bno\s+longer\b)"
    r"(?:\s+[A-Za-z'-]+){0,2}\s*$",
    re.IGNORECASE,
)
_COMPLETE_STATE = re.compile(
    r"(?:\bis\s+|\bare\s+|\bwas\s+|\bwere\s+|\bremains?\s+|"
    r"\bhas\s+been\s+|\bhave\s+been\s+|\bhad\s+been\s+)$",
    re.IGNORECASE,
)
_NUMERIC_MONTH = re.compile(r"\b(0?[1-9]|1[0-2])/(\d{4})\b")
_NUMERIC_DAY = re.compile(r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(\d{4})\b")
_ISO_DAY = re.compile(r"\b(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_MONTHS = {
    name.casefold(): number
    for number, name in enumerate(
        (
            "",
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
    )
    if name
}
_NAMED_DATE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+"
    r"(?:(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?(?:,\s*|\s+))?"
    r"(\d{4})\b",
    re.IGNORECASE,
)


def extract_document(
    session: Session,
    document: Document,
    *,
    client: StructuredClient | None = None,
    max_pages: int | None = None,
) -> list[Candidate]:
    """Extract stable External Party Statement Candidates from one Document."""
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
    document: Document,
    page: DocPage,
    item: dict,
    model: str | None,
) -> Candidate | None:
    quote = str(item.get("quote") or "").strip()
    if not quote:
        return None
    literal_quote = literal_quote_on_page(quote, page.text or "")
    if literal_quote is None:
        return None
    quote = literal_quote

    event_type = _stable_event_type(item.get("event_type"), quote)
    if event_type is None:
        return None
    timing = _supported_timing(item.get("committed_date"), quote)
    previous_timing = _supported_timing(item.get("previous_timing"), quote)
    if event_type == "commitment" and (timing is None or previous_timing is not None):
        return None
    if event_type == "committed_date_change" and (
        timing is None or previous_timing is None
    ):
        return None
    if event_type == "closure":
        timing = previous_timing = None

    document_party = _document_party(document)
    proposed_party = _supported_wording(item.get("external_org"), quote)
    affected_party = _affected_party(
        proposed_party=proposed_party,
        document_party=document_party,
        quote=quote,
    )
    if not affected_party:
        return None
    proposed_stated_party = _supported_wording(item.get("stated_party"), quote)
    stated_party = (
        proposed_stated_party
        if _party_is_actor(proposed_stated_party, quote)
        else None
    )
    if stated_party is None and _party_is_actor(document_party, quote):
        stated_party = document_party
    conflict_ref = _supported_token(item.get("conflict_ref"), quote)
    event_date = document.doc_date.isoformat() if document.doc_date else None

    fields = {
        "event_type": event_type,
        "description": quote,
        "external_org": affected_party,
    }
    if event_date:
        fields["event_date"] = event_date
    if stated_party:
        fields["stated_party"] = stated_party
    if conflict_ref:
        fields["conflict_ref"] = conflict_ref
    if timing:
        fields["committed_date"] = timing
    if previous_timing:
        fields["previous_timing"] = previous_timing

    return propose(
        document,
        kind="event",
        fields=fields,
        page_no=page.page_no,
        quote=quote,
        quote_verified=True,
        whole_row=False,
        confidence=item.get("confidence"),
        prompt_version=PROMPT_VERSION,
        model=model,
        dedupe=dedupe_hint(affected_party, conflict_ref or "", "-"),
        text_source=page.text_source,
    )


def _stable_event_type(value: object, quote: str) -> str | None:
    if _evidence_states_completion(quote):
        return "closure"
    rendered = str(value or "").strip()
    if rendered in {"commitment", "committed_date_change"}:
        return rendered
    return None


def _supported_timing(value: object, quote: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    text = str(value.get("text") or "").strip()
    precision = str(value.get("precision") or "").strip()
    if not text or normalize(text) not in normalize(quote):
        return None
    start = _date_value(value.get("start_date"))
    end = _date_value(value.get("end_date"))
    if precision == "day":
        if start is None or end != start:
            return None
    elif precision == "month":
        if start is None or start.day != 1:
            return None
        if end != date(start.year, start.month, monthrange(start.year, start.month)[1]):
            return None
    elif precision == "approximate":
        if start is not None or end is not None:
            return None
    else:
        return None
    canonical_text = _canonical_timing_text(
        precision=precision,
        start=start,
        quote=quote,
        selected_text=text,
    )
    if canonical_text is None:
        return None
    return {
        "text": canonical_text,
        "precision": precision,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
    }


def _canonical_timing_text(
    *, precision: str, start: date | None, quote: str, selected_text: str
) -> str | None:
    """Select one exact timing span from Evidence, never from model phrasing."""
    if precision == "approximate":
        return literal_quote_on_page(selected_text, quote)
    if start is None:
        return None
    if precision == "day":
        for match in _NUMERIC_DAY.finditer(quote):
            if (int(match[3]), int(match[1]), int(match[2])) == (
                start.year,
                start.month,
                start.day,
            ):
                return match.group(0)
        for match in _ISO_DAY.finditer(quote):
            if (int(match[1]), int(match[2]), int(match[3])) == (
                start.year,
                start.month,
                start.day,
            ):
                return match.group(0)
    if precision == "month":
        for match in _NUMERIC_MONTH.finditer(quote):
            if (int(match[2]), int(match[1])) == (start.year, start.month):
                return match.group(0)
    for match in _NAMED_DATE.finditer(quote):
        month = _MONTHS[match.group(1).casefold()]
        day = int(match.group(2)) if match.group(2) else None
        if int(match.group(3)) != start.year or month != start.month:
            continue
        if precision == "month" and day is None:
            return match.group(0)
        if precision == "day" and day == start.day:
            return match.group(0)
    return None


def _date_value(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _document_party(document: Document) -> str | None:
    parts = Path(document.filename).parts
    try:
        index = parts.index("Meeting Notes")
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return None
    value = parts[index + 1].strip()
    return value or None


def _affected_party(
    *, proposed_party: str | None, document_party: str | None, quote: str
) -> str | None:
    """Prefer the party acting in Evidence; use folder context only as fallback."""
    if _party_is_actor(proposed_party, quote):
        return proposed_party
    if _party_is_actor(document_party, quote):
        return document_party
    return proposed_party or document_party


def _party_is_actor(value: str | None, quote: str) -> bool:
    """Does the quote grammatically place this party in the actor position?"""
    if not value:
        return False
    actor = re.escape(value.strip())
    return bool(
        re.search(
            rf"(?:^|[.!?;,:\-–—]\s+){actor}\s+"
            r"(?:to\b|will\b|shall\b|agrees?\b|commits?\b|"
            r"has\b|have\b|had\b|is\b|are\b|was\b|were\b|"
            r"provided\b|delivered\b|completed\b|closed\b|cleared\b)",
            quote,
            re.IGNORECASE,
        )
    )


def _evidence_states_completion(quote: str) -> bool:
    """Recognize completed state without turning future delivery into closure."""
    for match in _COMPLETION.finditer(quote):
        prefix = quote[: match.start()].rstrip()
        window = prefix[-40:]
        if _COMPLETION_FUTURE.search(window) or _COMPLETION_NEGATION.search(window):
            continue
        word = match.group(0).casefold()
        if word == "complete" and not (
            not prefix
            or prefix.endswith((".", "!", "?", ":", ";"))
            or _COMPLETE_STATE.search(window)
        ):
            continue
        return True
    return False


def _supported_wording(value: object, quote: str) -> str | None:
    rendered = str(value or "").strip()
    return rendered if _wording_is_supported(rendered, quote) else None


def _wording_is_supported(value: str | None, quote: str) -> bool:
    return bool(value and normalize(value) in normalize(quote))


def _supported_token(value: object, quote: str) -> str | None:
    rendered = str(value or "").strip()
    if not rendered:
        return None
    pattern = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(rendered)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    match = pattern.search(quote)
    return match.group(0) if match else None


# Public adapter for orchestrators that need the exact deployed Minutes v4
# Candidate conversion without importing this module's private implementation.
to_candidate = _to_candidate


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
        items_key="events",
        noun=Noun("notes", "External Party Statements"),
        extractor_registry_key="minutes",
    )


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
