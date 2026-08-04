"""Tiered extraction of a Utility Conflict Matrix (ADR-0006).

The validation gate (#68) measured both earlier designs on the layout the
deterministic parser handles best, and each turned out to be best at
exactly what the other was worst at. The parser reads values off the page's
word boxes and got 3 of 3,235 rows wrong; it also mapped **zero** columns
on FDOT SR 789, whose printed headers match no synonym table. A vision
model transcribing the same pages got 164 of 3,240 rows wrong — digits
dropped and transposed, in the stationing fields merge ranking depends on,
at confidences of 0.98 and 0.99 — and it read SR 789 without configuration.

So the model does not transcribe. Per page it answers only what it is good
at: is there a matrix here, what does the page state once for all its rows,
and which printed column is which canonical field. Code then reads every
cell from the word boxes. **The model never writes a digit**, and the
transcription error class is gone by construction rather than reduced by
tuning.

Two tiers, chosen per page:

- **structure** — the page has a text layer, so geometry can supply values.
  The default, and every matrix in the corpus today.
- **transcribe** — no text layer, or no table geometry to read. The model
  writes the values and every token is verified against the OCR text. The
  fallback is recorded on the Candidate so its rate is a number to watch
  rather than a surprise.

Both tiers end at the same gate: the quote must appear on the page and
every token of every stored value must too. Nothing here writes to the
Ledger — extractors produce Candidates only.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.docs import stored_pdf
from corridor.extract import (
    MatrixRow,
    NoMatrixFound,
    best_verifiable_quote,
    dedupe_hint,
    normalize_header,
    page_tables,
    row_quote,
    row_to_fields,
)
from corridor.llm import OpenAIClient, StructuredClient, complete_many
from corridor.models import Candidate, DocPage, Document
from corridor.verify import quote_appears_on, unverified_fields

PROMPT_VERSION = "matrix_tiered_v1"
STRUCTURE_PROMPT = Path("prompts/matrix_structure_v1.md")
TRANSCRIBE_PROMPT = Path("prompts/matrix_v1.md")

TIER_STRUCTURE = "structure"
TIER_TRANSCRIBE = "transcribe"

# Canonical field names, shared with the deterministic parser so that eval,
# merge ranking and the queue read every path's output the same way. The
# model may name one of these and nothing else; anything outside the set is
# treated as unmapped rather than stored, because the vocabulary is
# versioned and a new field is a deliberate change, not an extractor's
# improvisation.
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
    "external_org_contact",
    "committed_date",
    "notes",
)

# Facts a page states once for every row on it. Narrow on purpose: the
# argument for hoisting only holds for a value the page really does state
# for all its rows, and a row-scoped value hoisted here would be a
# fabrication applied to every other row on the page.
PAGE_FIELDS = ("external_org",)

# A row needs both to be a Dependency: an identifier to be tracked by, and
# an External Party to be owed by. Either may arrive from the page rather
# than the row. Also guards a legend table the model mistook for the matrix.
REQUIRED = ("utility_id", "external_org")

# ...and at least this many of them must come from the row's own cells.
#
# The deterministic parser got this for free by requiring both fields from
# the row, which page-scoped inheritance then took away: FDOT prints a
# full-width group-title band mid-table (`FROM C/L CONST GULF OF MEXICO
# DR.`), and a band inheriting the page's External Party satisfies both
# required fields off a single cell. That is nine phantom rows on SR 789,
# one per page. A band has one non-empty cell of ten; a conflict row has
# eight.
MIN_ROW_FIELDS = 2

# How many rows of each table to show the model when asking where the
# header is. Deeper than this is a data row that happens to look like one.
HEADER_CANDIDATES = 3

# A transcribed numeric token below this log-probability is treated as a
# guess and sinks its row.
#
# The gate (#68) established that the model's own confidence cannot do this
# job: of 164 rows carrying a value that was not on the page, 96 were
# reported at 0.98 and 62 at 0.99. Token probabilities are a measurement
# rather than a self-report, and asking for them is free. -3.0 is roughly
# a 5% token probability — deliberately loose, because this only sinks a
# row in the queue and a false flag costs a second look while a missed one
# costs a wrong number in the Ledger.
MIN_TOKEN_LOGPROB = -3.0

_PAGE_ATTRIBUTES = {
    "type": "object",
    "additionalProperties": False,
    "required": list(PAGE_FIELDS),
    "properties": {name: {"type": ["string", "null"]} for name in PAGE_FIELDS},
}

STRUCTURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "is_utility_matrix",
        "page_attributes",
        "matrix_table",
        "header_row",
        "columns",
    ],
    "properties": {
        # The only way to tell "this page is not a matrix" from "this matrix
        # has no conflicts" once a synonym table is out of the picture.
        "is_utility_matrix": {"type": "boolean"},
        "page_attributes": _PAGE_ATTRIBUTES,
        "matrix_table": {"type": ["integer", "null"]},
        # Null on a page that continues a matrix headed on an earlier page.
        "header_row": {"type": ["integer", "null"]},
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "canonical_field"],
                "properties": {
                    # The index we showed it, so a mapping never depends on
                    # the model counting columns correctly.
                    "index": {"type": "integer"},
                    "canonical_field": {"type": ["string", "null"]},
                },
            },
        },
    },
}

_ROW_PROPERTIES = {name: {"type": ["string", "null"]} for name in ROW_FIELDS}

TRANSCRIBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_utility_matrix", "page_attributes", "rows"],
    "properties": {
        "is_utility_matrix": {"type": "boolean"},
        "page_attributes": _PAGE_ATTRIBUTES,
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

    Raises `NoMatrixFound` when the document could not be read at all — no
    rendered pages to show the model, or no page carrying a matrix. Returns
    an empty list for a matrix with no conflicts.
    """
    client = client or OpenAIClient()

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
            f"no page image for {document.filename}; the extractor has nothing "
            "to read. Re-ingest before treating this as an empty matrix."
        )

    grids = _read_geometry(document, pages)
    structure_pages = [p for p in pages if grids.get(p.page_no)]
    transcribe_pages = [p for p in pages if not grids.get(p.page_no)]

    model = getattr(client, "model", None)
    candidates: list[Candidate] = []
    recognized = 0

    if structure_pages:
        results = complete_many(
            client,
            system=STRUCTURE_PROMPT.read_text(),
            schema=STRUCTURE_SCHEMA,
            users=[_structure_user(document, p, grids[p.page_no]) for p in structure_pages],
            images=[[p.image_path] for p in structure_pages],
        )
        for page, result in zip(structure_pages, results):
            if "_error" in result:
                continue
            if result.get("is_utility_matrix"):
                recognized += 1
            candidates.extend(
                _structure_candidates(
                    document, page, grids[page.page_no], result, model, session
                )
            )

    if transcribe_pages:
        results = complete_many(
            client,
            system=TRANSCRIBE_PROMPT.read_text(),
            schema=TRANSCRIBE_SCHEMA,
            users=[_transcribe_user(document, p) for p in transcribe_pages],
            images=[[p.image_path] for p in transcribe_pages],
            # Only this tier writes values, so only this tier needs a
            # measured signal about how sure the model was of each digit.
            logprobs=True,
        )
        for page, result in zip(transcribe_pages, results):
            if "_error" in result:
                continue
            if result.get("is_utility_matrix"):
                recognized += 1
            inherited = _page_attributes(result)
            unsure = _low_confidence_tokens(result)
            for item in result.get("rows") or []:
                candidate = _transcribed_candidate(
                    document, page, item, model, inherited, unsure
                )
                if candidate is not None:
                    session.add(candidate)
                    candidates.append(candidate)

    # A run has to be able to say how much of a document it could read
    # properly. A fallback nobody counts is a fallback nobody notices.
    document.extraction_tiers = {
        tier: count
        for tier, count in (
            (TIER_STRUCTURE, len(structure_pages)),
            (TIER_TRANSCRIBE, len(transcribe_pages)),
        )
        if count
    }

    if recognized == 0:
        raise NoMatrixFound(
            f"no page of {document.filename} carries a utility matrix. "
            "A layout variant is unhandled — do not treat this as an empty matrix."
        )

    session.flush()
    return candidates


def _read_geometry(
    document: Document, pages: list[DocPage]
) -> dict[int, list[list[list[str]]]]:
    """Table cells per page, read from word boxes.

    A page with no text layer has no word boxes to read, and a page whose
    tables geometry cannot find has nothing to map — both fall to the
    transcription tier by returning nothing here.
    """
    path = stored_pdf(document)
    if path is None:
        return {}

    wanted = {page.page_no for page in pages if page.text_source == "text_layer"}
    grids: dict[int, list[list[list[str]]]] = {}
    with pymupdf.open(path) as pdf:
        for index, page in enumerate(pdf):
            page_no = index + 1
            if page_no in wanted:
                found = page_tables(page)
                if found:
                    grids[page_no] = found
    return grids


def _structure_user(document: Document, page: DocPage, grids) -> str:
    """The page number is ours, and so are the cells the model maps.

    Showing the cells geometry read — numbered — means the model's answer
    joins back by index rather than by matching a string it retyped, and it
    never has to count columns.
    """
    lines = [
        f"Page {page.page_no} of {document.filename}.",
        "Tables found on this page, cells as read from the page:",
    ]
    for table_index, grid in enumerate(grids):
        width = len(grid[0]) if grid else 0
        lines.append(f"\nTable {table_index} — {width} columns, {len(grid)} rows:")
        for row_index, row in enumerate(grid[:HEADER_CANDIDATES]):
            cells = " | ".join(
                f"[{i}] {(cell or '').strip()}" for i, cell in enumerate(row)
            )
            lines.append(f"  row {row_index}: {cells}")
    return "\n".join(lines)


def _transcribe_user(document: Document, page: DocPage) -> str:
    return (
        f"Page {page.page_no} of {document.filename}. "
        "Transcribe this page's utility conflict rows."
    )


def _structure_candidates(
    document: Document,
    page: DocPage,
    grids,
    result: dict,
    model: str | None,
    session: Session,
) -> list[Candidate]:
    table_index = result.get("matrix_table")
    if table_index is None or not (0 <= table_index < len(grids)):
        return []
    grid = grids[table_index]
    if not grid:
        return []

    mapping, unmapped = _column_mapping(grid, result)
    header_row = result.get("header_row")
    body = grid[header_row + 1 :] if isinstance(header_row, int) else grid
    inherited = _page_attributes(result)
    page_text = page.text or ""

    candidates = []
    for raw in body:
        own = row_to_fields(raw, mapping)
        if len(own) < MIN_ROW_FIELDS:
            continue
        fields = {**inherited, **own}
        if not all(fields.get(name) for name in REQUIRED):
            continue
        row = MatrixRow(fields, page.page_no, row_quote(raw), tuple(raw))
        quote, whole_row = best_verifiable_quote(row, page_text)
        candidate = _candidate(
            document,
            page,
            fields,
            quote=quote,
            whole_row=whole_row,
            confidence=None,
            model=model,
            tier=TIER_STRUCTURE,
            unmapped=unmapped,
        )
        session.add(candidate)
        candidates.append(candidate)
    return candidates


def _column_mapping(grid, result: dict) -> tuple[dict[int, str], list[str]]:
    """Column index to canonical field, plus the headers we could not use.

    A field the model named that is not in the canonical vocabulary is
    treated exactly like one it declined to map: recorded by its printed
    name for a human to judge, never stored under a guessed heading. A
    wrong mapping is silent and applies to every row on the page.
    """
    header_row = result.get("header_row")
    headers = grid[header_row] if isinstance(header_row, int) and header_row < len(grid) else []

    mapping: dict[int, str] = {}
    unmapped: list[str] = []
    for column in result.get("columns") or []:
        index = column.get("index")
        if not isinstance(index, int) or index < 0:
            continue
        field = column.get("canonical_field")
        if field in ROW_FIELDS and field not in mapping.values():
            mapping[index] = field
            continue
        printed = normalize_header(headers[index]) if index < len(headers) else ""
        if printed:
            unmapped.append(headers[index].strip())
    return mapping, unmapped


def _page_attributes(result: dict) -> dict[str, str]:
    attributes = result.get("page_attributes") or {}
    return {
        name: value.strip()
        for name in PAGE_FIELDS
        if (value := (attributes.get(name) or "").strip())
    }


def _low_confidence_tokens(result: dict) -> list[str]:
    """Numeric tokens the model was not sure of, from its own logprobs.

    Numeric only: prose wanders harmlessly and a low-probability word says
    little, but a hesitant digit is exactly the failure the gate measured
    and the one nothing else catches. `_meta` is the client's reserved key
    and never reaches a Candidate.
    """
    entries = ((result.get("_meta") or {}).get("logprobs")) or []
    unsure = []
    for entry in entries:
        token = (entry.get("token") or "").strip()
        if not any(character.isdigit() for character in token):
            continue
        if entry.get("logprob", 0.0) < MIN_TOKEN_LOGPROB:
            unsure.append(token)
    return unsure


def _transcribed_candidate(
    document: Document,
    page: DocPage,
    item: dict,
    model: str | None,
    inherited: dict[str, str],
    unsure: list[str],
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

    fields = {**inherited, **row}
    # Only tokens this row actually used: one shaky digit on a page must
    # not sink every other row on it.
    mine = sorted({t for t in unsure if any(t in value for value in fields.values())})

    return _candidate(
        document,
        page,
        fields,
        quote=quote,
        whole_row=True,
        confidence=item.get("confidence"),
        model=model,
        tier=TIER_TRANSCRIBE,
        unmapped=[],
        low_confidence=mine,
    )


def _candidate(
    document: Document,
    page: DocPage,
    fields: dict[str, str],
    *,
    quote: str,
    whole_row: bool,
    confidence: float | None,
    model: str | None,
    tier: str,
    unmapped: list[str],
    low_confidence: list[str] | None = None,
) -> Candidate:
    page_text = page.text or ""
    quote_ok = quote_appears_on(quote, page_text)
    suspect = sorted(unverified_fields(fields, page_text))
    unsure = low_confidence or []

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
                    "whole_row": whole_row,
                }
            ],
            "confidence": confidence,
            # Named rather than counted, so a reviewer sees *which* value is
            # not on the page instead of only that one of them is not.
            "unverified_fields": suspect,
            # What the document says that the Ledger has no field for. The
            # trigger for a deliberate vocabulary extension, not something
            # an extractor may decide for itself.
            "unmapped_columns": unmapped,
            # Transcribed digits the model hesitated on. Empty on the
            # structure tier, which transcribes nothing.
            "low_confidence_tokens": unsure,
            "tier": tier,
            "dedupe_hint": dedupe_hint(fields),
            # OCR text is materially noisier, and a citation resting on it
            # deserves to be visibly different when a reviewer weighs it.
            "text_source": page.text_source,
        },
        source_document_id=document.id,
        source_pages=[page.page_no],
        confidence=confidence,
        prompt_version=PROMPT_VERSION,
        model=model,
        citations_verified=quote_ok and not suspect and not unsure,
    )
