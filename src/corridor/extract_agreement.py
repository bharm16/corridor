"""LLM extraction of obligations from executed agreements.

Extraction runs **per page**, which is the whole design. The page number is
supplied by us and never by the model, so a citation cannot point at a page
the model invented — the only thing the model contributes to a citation is
the quote, and that quote is then mechanically verified against the page it
was drawn from. A hallucinated page number is impossible by construction;
a hallucinated quote is caught.

Nothing here writes to the Ledger. Extractors produce Candidates only.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.llm import OpenAIClient, StructuredClient
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
        for item in result.get("obligations") or []:
            candidate = _to_candidate(document, page, item, getattr(client, "model", None))
            if candidate is not None:
                session.add(candidate)
                candidates.append(candidate)

    session.flush()
    return candidates


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

    return Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
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
            "dedupe_hint": "|".join(
                [fields.get("external_org", ""), "agreement", fields.get("title", "")]
            ),
            # OCR text is materially noisier, and a citation resting on it
            # deserves to be visibly different when a reviewer weighs it.
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
    """Extract obligations from every agreement in a project."""
    import sys

    from corridor.db import Session as SessionFactory

    slug = argv[0] if argv else "nhhip-3c2"
    limit = int(argv[1]) if len(argv) > 1 else None

    from corridor.models import Project

    with SessionFactory() as session:
        project = session.scalars(
            select(Project).where(Project.slug == slug)
        ).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        documents = session.scalars(
            select(Document)
            .where(Document.project_id == project.id, Document.doc_type == "agreement")
            .order_by(Document.doc_date)
        ).all()
        if limit:
            documents = documents[:limit]

        client = OpenAIClient()
        total = verified = 0
        # flush on every line: this run takes ~40 minutes over 269 pages, and
        # Python buffers stdout when it is not a TTY. Without this the whole
        # job looks identical to a hung one until it finishes.
        print(f"{len(documents)} agreements, model {client.model}", flush=True)
        for document in documents:
            candidates = extract_document(session, document, client=client)
            ok = sum(1 for c in candidates if c.citations_verified)
            total += len(candidates)
            verified += ok
            print(
                f"  {ok:>3}/{len(candidates):<3} verified  {document.filename[:56]}",
                flush=True,
            )
            session.commit()

        pct = 100 * verified / total if total else 0.0
        print(f"{total} obligations, {verified} verified ({pct:.1f}%)", flush=True)
        print(
            f"tokens: {client.usage.prompt_tokens:,} in / "
            f"{client.usage.completion_tokens:,} out",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
