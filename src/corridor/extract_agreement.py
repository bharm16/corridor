"""LLM extraction of obligations from executed agreements.

Extraction runs **per page**, which is the whole design. The page number is
supplied by us and never by the model, so a citation cannot point at a page
the model invented — the only thing the model contributes to a citation is
the quote, and that quote is then mechanically verified against the page it
was drawn from. A hallucinated page number is impossible by construction;
a hallucinated quote is caught.

The one production path is ``main``: `make agreements` drives
``extract_batch.run_extraction`` with this module's prompt, schema and
``_to_candidate``. A sequential ``extract_document`` loop used to sit beside
it; nothing in ``src/`` called it, and its suite proved a loop the product
never ran, so it is gone and the suite drives ``main`` through the runner's
injectable client and session seams instead.

Nothing here writes to the Ledger. Extractors produce Candidates only.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from corridor.candidates import dedupe_hint, propose
from corridor.models import Candidate, DocPage, Document
from corridor.verify import quote_appears_on

PROMPT_VERSION = "agreement_v3"
PROMPT_PATH = Path("prompts/agreement_v3.md")

# Below this there is nothing to read — a mostly-blank scan or a page of
# furniture. Calling the model on it spends tokens to be told "no".
MIN_PAGE_CHARS = 200

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["obligations"],
    "properties": {
        "obligations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "external_org",
                    "obligation",
                    "notice_period",
                    "committed_date",
                    "evidence_required",
                    "quote",
                    "confidence",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "external_org": {"type": ["string", "null"]},
                    "obligation": {"type": "string"},
                    "notice_period": {"type": ["string", "null"]},
                    "committed_date": {"type": ["string", "null"]},
                    "evidence_required": {"type": ["string", "null"]},
                    "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}


def _to_candidate(
    document: Document, page: DocPage, item: dict, model: str | None
) -> Candidate | None:
    quote = (item.get("quote") or "").strip()
    if not quote:
        return None

    # The page is ours; only the quote came from the model, and it is checked
    # against the exact text the model was shown.
    verified = quote_appears_on(quote, page.text or "")

    fields = {
        key: value
        for key, value in (
            ("title", item.get("title")),
            ("external_org", item.get("external_org")),
            ("obligation", item.get("obligation")),
            ("notice_period", item.get("notice_period")),
            ("committed_date", item.get("committed_date")),
            ("evidence_required", item.get("evidence_required")),
        )
        if value
    }
    if not fields.get("title"):
        return None

    return propose(
        document,
        kind="dependency",
        fields=fields,
        page_no=page.page_no,
        quote=quote,
        quote_verified=verified,
        whole_row=False,
        confidence=item.get("confidence"),
        prompt_version=PROMPT_VERSION,
        model=model,
        dedupe=dedupe_hint(
            fields.get("external_org", ""), "agreement", fields.get("title", "")
        ),
        text_source=page.text_source,
    )


def main(
    argv: list[str],
    *,
    client_factory: Callable | None = None,
    session_factory: Callable | None = None,
) -> int:
    """`make agreements ARGS="<slug> [limit]"`

    ``client_factory`` and ``session_factory`` are the runner's own seams,
    passed through so the suite can drive this exact wiring with a recorded
    client and a rollback-scoped session; production passes neither.
    """
    from corridor.extract_batch import Noun, run_extraction

    return run_extraction(
        argv,
        doc_type="agreement",
        default_slug="nhhip-3c2",
        prompt_version=PROMPT_VERSION,
        system=PROMPT_PATH.read_text(),
        schema=SCHEMA,
        min_page_chars=MIN_PAGE_CHARS,
        to_candidate=_to_candidate,
        items_key="obligations",
        noun=Noun("agreements", "obligations"),
        extractor_registry_key="agreement",
        client_factory=client_factory,
        session_factory=session_factory,
    )


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
