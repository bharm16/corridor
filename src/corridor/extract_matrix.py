"""Vision extraction of a Utility Conflict Matrix.

The deterministic parser reads `find_tables()` output and maps column
headers by synonym. That works on the layouts it was built against and
fails on the rest — quietly, which is the part that costs (ADR-0004). A
vision model reads the rendered page instead, where the layout is still
intact.

Because the model now *transcribes* values rather than reading them off a
grid, verification gets stronger rather than weaker:

- extraction runs per page and **the page number is supplied by us**, so a
  citation cannot point at a page the model invented
- the quote is verified against the page text, as with every other
  extractor
- **every token of every stored field value must also appear on the page**,
  which catches a transcribed `1140+00` where the document says `1149+00`

The model reads pixels and the checks read characters, deliberately. Two
independent representations, so a value the model invented cannot appear in
a stream it never saw — and verifying its output against its own input
would catch fabrication but not misreading.

Nothing here writes to the Ledger. Extractors produce Candidates only.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.extract import NoMatrixFound
from corridor.llm import OpenAIClient, StructuredClient, complete_many
from corridor.models import Candidate, DocPage, Document
from corridor.verify import quote_appears_on, unverified_fields

PROMPT_VERSION = "matrix_vision_v1"
PROMPT_PATH = Path("prompts/matrix_v1.md")

# Canonical field names, shared with the deterministic parser so that eval,
# merge ranking and the queue read both paths' output the same way.
ROW_FIELDS = (
    "utility_id",
    "external_org",
    "utility_type",
    "size",
    "material",
    "oh_ug",
    "baseline",
    "orientation",
    "alignment",
    "location_start",
    "location_end",
    "station_from",
    "station_to",
    "offset_from",
    "offset_to",
    "offset_side",
    "potential_conflict",
    "sue_level",
    # Neither is in any PDF matrix in the corpus today, and both are in the
    # spreadsheet form. Asking costs nothing on a document that omits them
    # and the Ledger has nowhere else to get them (#59, stories 21 and 22).
    "external_org_contact",
    "committed_date",
    "notes",
)

# Facts a page states once for every row on it. FDOT SR 789 names its
# External Party in the page header — `UTILITY AGENCY OWNER: Comcast`, one
# utility per page — where TxDOT repeats the owner on every row. Both are
# ordinary, and an extractor has to handle a document that scopes a field
# to a region rather than to a record.
#
# Narrow on purpose. Returning the owner once is strictly better than
# asking the model to repeat it on 29 rows, because it becomes one string
# verified once with no per-row transcription surface — but that argument
# only holds for a value the page really does state for all its rows. A
# row-scoped value hoisted up here would be a fabrication applied 29 times.
PAGE_FIELDS = ("external_org",)

_ROW_PROPERTIES = {name: {"type": ["string", "null"]} for name in ROW_FIELDS}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_utility_matrix", "page_attributes", "rows"],
    "properties": {
        # The only way to tell "this page is not a matrix" from "this matrix
        # has no conflicts" once a table reader is out of the picture. Those
        # two outcomes mean opposite things.
        "is_utility_matrix": {"type": "boolean"},
        "page_attributes": {
            "type": "object",
            "additionalProperties": False,
            "required": list(PAGE_FIELDS),
            "properties": {
                name: {"type": ["string", "null"]} for name in PAGE_FIELDS
            },
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [*ROW_FIELDS, "quote", "confidence"],
                "properties": {
                    **_ROW_PROPERTIES,
                    "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
}


def extract_document(
    session: Session,
    document: Document,
    *,
    client: StructuredClient | None = None,
    max_pages: int | None = None,
) -> list[Candidate]:
    """Every conflict row on every page of one matrix.

    Raises `NoMatrixFound` when the document could not be read at all —
    no rendered pages to show the model, or no page that carries a matrix.
    Returns an empty list for a matrix with no conflicts.
    """
    client = client or OpenAIClient()
    system = PROMPT_PATH.read_text()

    pages = [
        page
        for page in session.scalars(
            select(DocPage)
            .where(DocPage.document_id == document.id)
            .order_by(DocPage.page_no)
        )
        if page.image_path
    ]
    if max_pages:
        pages = pages[:max_pages]
    if not pages:
        raise NoMatrixFound(
            f"no page image for {document.filename}; the vision extractor has "
            "nothing to read. Re-ingest before treating this as an empty matrix."
        )

    results = complete_many(
        client,
        system=system,
        schema=SCHEMA,
        users=[_user(document, page) for page in pages],
        images=[[page.image_path] for page in pages],
    )

    model = getattr(client, "model", None)
    candidates: list[Candidate] = []
    recognized = 0
    errors = 0

    for page, result in zip(pages, results):
        if "_error" in result:
            errors += 1
            continue
        if result.get("is_utility_matrix"):
            recognized += 1
        inherited = _page_attributes(result)
        for item in result.get("rows") or []:
            candidate = _to_candidate(document, page, item, model, inherited)
            if candidate is not None:
                session.add(candidate)
                candidates.append(candidate)

    if recognized == 0:
        raise NoMatrixFound(
            f"no page of {document.filename} carries a utility matrix"
            + (f" ({errors} of {len(pages)} pages failed)" if errors else "")
            + ". A layout variant is unhandled — do not treat this as an "
            "empty matrix."
        )

    session.flush()
    return candidates


def _user(document: Document, page: DocPage) -> str:
    """The page number is ours. The model is never asked for one."""
    return (
        f"Page {page.page_no} of {document.filename}. "
        "Transcribe this page's utility conflict rows."
    )


def _page_attributes(result: dict) -> dict[str, str]:
    attributes = result.get("page_attributes") or {}
    return {
        name: value.strip()
        for name in PAGE_FIELDS
        if (value := (attributes.get(name) or "").strip())
    }


def _to_candidate(
    document: Document,
    page: DocPage,
    item: dict,
    model: str | None,
    inherited: dict[str, str],
) -> Candidate | None:
    quote = (item.get("quote") or "").strip()
    if not quote:
        return None

    row = {
        name: value.strip()
        for name in ROW_FIELDS
        if (value := (item.get(name) or "").strip())
    }
    if not row:
        return None

    # The row wins. A document that states its External Party per row is
    # unaffected by a page that also states one, which is what keeps this
    # safe to run against every layout rather than only FDOT's.
    fields = {**inherited, **row}

    page_text = page.text or ""
    quote_ok = quote_appears_on(quote, page_text)
    suspect = sorted(unverified_fields(fields, page_text))

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
                    "verified": quote_ok,
                    "whole_row": True,
                }
            ],
            "confidence": item.get("confidence"),
            # Named rather than counted, so a reviewer sees *which* value is
            # not on the page instead of only that one of them is not.
            "unverified_fields": suspect,
            "dedupe_hint": _dedupe_hint(fields),
            # OCR text is materially noisier, and a citation resting on it
            # deserves to be visibly different when a reviewer weighs it.
            "text_source": page.text_source,
        },
        source_document_id=document.id,
        source_pages=[page.page_no],
        confidence=item.get("confidence"),
        prompt_version=PROMPT_VERSION,
        model=model,
        # A row whose quote is real but whose values are not is exactly the
        # defect this extractor exists to remove, so it sinks in the queue
        # alongside a fabricated quote. Neither is ever dropped.
        citations_verified=quote_ok and not suspect,
    )


def _dedupe_hint(fields: dict[str, str]) -> str:
    return "|".join(
        [
            fields.get("external_org", ""),
            fields.get("utility_type", ""),
            f"{fields.get('station_from', '')}-{fields.get('station_to', '')}",
        ]
    )
