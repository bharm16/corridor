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

from dataclasses import dataclass
from pathlib import Path

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.candidates import propose
from corridor.docs import stored_pdf
from corridor.geometry import (
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
from corridor.models import ANSWER_SEPARATOR, Candidate, DocPage, Document
from corridor.verify import quote_appears_on, unverified_fields

# Re-exported deliberately. The canonical vocabulary moved out when a second
# reader needed it (ADR-0005, #60) and it is still what a reader of *this*
# module is looking for, so the names stay reachable where they have always
# been rather than every caller learning a new home.
from corridor.vocabulary import (  # noqa: F401
    DECLINED_COLUMNS,
    LOCAL_FIELDS,
    MIN_ROW_FIELDS,
    REQUIRED,
    ROW_FIELDS,
    TEMPLATE_FIELDS,
    is_retired_row,
)

# v2 widens the canonical vocabulary to TxDOT's published template (#97).
# v3 lets a resolution strategy recorded as marked columns be read as one
# (#105). A version bump rather than an edit in place, for the same reason
# both times: the field a value lands in changes — three of WSDOT's four
# resolution columns move out of `unmapped_columns` and the fourth stops
# being a bare `X` — so v2 and v3 candidates are different readings of the
# same page and pooling them would make every eval number meaningless.
PROMPT_VERSION = "matrix_tiered_v3"
# Superseded prompts are kept beside the current one rather than edited:
# their Candidates are still in the database, and a prompt that has been
# overwritten cannot say what produced them (ADR-0003).
STRUCTURE_PROMPT = Path("prompts/matrix_structure_v3.md")
TRANSCRIBE_PROMPT = Path("prompts/matrix_v1.md")

TIER_STRUCTURE = "structure"
TIER_TRANSCRIBE = "transcribe"


# The one canonical field several columns may claim at once, because it is
# the one this corpus records as marked columns: WSDOT prints four of them
# beneath a spanning `RECOMMENDED RESOLUTION` group cell, each marked `X`
# (#105, ADR-0009).
#
# Every other field keeps one column. A model naming two `external_org`
# columns is making a mistake rather than describing a group, and the
# second silently overwriting the first is how a wrong owner reaches every
# row on a page.
MARKED_COLUMN_FIELD = "resolution_strategy"

# A cell holding this and nothing else is *only* a mark — a value that says
# nothing because the heading it belongs to was not kept.
#
# Note this is a narrower question than "is this row marked here", which
# `_marked_strategy` answers with any non-empty cell. It can afford to,
# because it knows the heading: under `Retain and Protect`, anything at all
# in the cell means the row was marked. This set is for the case with no
# heading to lean on, where the value has to be recognised on its own.
#
# Enumerated from the only marked-column document in the corpus rather than
# guessed at: across its 11 pages the resolution cells hold `X` 171 times
# and `x` twice, and nothing else. A new glyph is a one-line change with a
# real document behind it.
MARK_CELLS = frozenset({"x"})


@dataclass(frozen=True)
class ColumnMapping:
    """What each column of one printed header holds.

    `marks` is empty for every layout that prints its resolution strategy
    as a value — which is every project in the corpus but WSDOT. When it is
    not, the mapping cannot say what the column *contains*, only which
    heading a mark under it would mean, so the heading travels beside the
    index instead of being discarded (#105).
    """

    fields: dict[int, str]
    unmapped: list[str]
    marks: dict[int, str]

    def signature(self) -> tuple[tuple, tuple]:
        """What two readings of one header must match on to be one reading.

        Both halves, because two pages that agree on the value columns and
        disagree on which are marked have not agreed on the header (#101).
        """
        return (
            tuple(sorted(self.fields.items())),
            tuple(sorted(self.marks.items())),
        )


# Facts a page states once for every row on it. Narrow on purpose: the
# argument for hoisting only holds for a value the page really does state
# for all its rows, and a row-scoped value hoisted here would be a
# fabrication applied to every other row on the page.
PAGE_FIELDS = ("external_org",)


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

# Shorter fragments than this are matched into so many values that they say
# nothing about any one row.
MIN_TOKEN_FRAGMENT = 3

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
        "mapping_confidence",
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
        # One number for the page, not one per row: on this tier the model's
        # only judgement is what the columns mean, so that is the only thing
        # it can be more or less sure of. Every row read under a mapping
        # inherits the mapping's confidence.
        "mapping_confidence": {"type": "number"},
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
    errors = 0
    # Zero rather than null: a document read entirely by transcription has
    # no printed header to disagree about, which is a different fact from
    # one nobody has read.
    document.header_disagreements = 0

    if structure_pages:
        results = complete_many(
            client,
            system=STRUCTURE_PROMPT.read_text(),
            schema=STRUCTURE_SCHEMA,
            users=[_structure_user(document, p, grids[p.page_no]) for p in structure_pages],
            images=[[p.image_path] for p in structure_pages],
        )
        # One header, one mapping. The model is asked per page, so pages
        # reprinting an identical header were each answered independently —
        # and disagreed: SH 99's `UCM_to_RIDs` gave three readings of one
        # 16-column header across 17 pages, WSDOT 9424 three across 11.
        # That is not unreliability, it is the same question asked eleven
        # times. Resolved by majority before any row is read (#101).
        resolved, disagreements = _resolve_headers(structure_pages, grids, results)
        document.header_disagreements = disagreements

        # Pages in order, so a header printed on page 1 can carry to the
        # continuation pages that follow it.
        carried: tuple[ColumnMapping, int] | None = None
        for page, completion in zip(structure_pages, results):
            if completion.failed:
                errors += 1
                continue
            result = completion.value
            if result.get("is_utility_matrix"):
                recognized += 1
            made, carried = _structure_candidates(
                document, page, grids[page.page_no], result, model, session,
                carried, resolved=resolved,
            )
            candidates.extend(made)

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
        for page, completion in zip(transcribe_pages, results):
            if completion.failed:
                errors += 1
                continue
            result = completion.value
            if result.get("is_utility_matrix"):
                recognized += 1
            inherited = _page_attributes(result)
            unsure = _low_confidence_tokens(completion)
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

    # An outage is not an unhandled layout. Reporting every page failing
    # as `NoMatrixFound` would tell a reader the document is unreadable
    # when nothing was ever read — the conflation #59 story 2 forbids, one
    # level up from the empty-versus-unreadable one.
    if errors == len(pages):
        raise RuntimeError(
            f"every page of {document.filename} failed: {errors} of "
            f"{len(pages)}. This says nothing about the document."
        )

    if recognized == 0:
        raise NoMatrixFound(
            f"no page of {document.filename} carries a utility matrix"
            + (f" ({errors} of {len(pages)} pages also failed)" if errors else "")
            + ". A layout variant is unhandled — do not treat this as an "
            "empty matrix."
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


def _header_key(grid, header_row) -> tuple[str, ...] | None:
    """The printed header, normalised, as the thing a mapping belongs to.

    None when this page states no header of its own — a continuation page,
    or a table that starts straight into data. Those keep the existing
    carry-forward path; they have no header to agree with.
    """
    if not isinstance(header_row, int) or header_row >= len(grid):
        return None
    return tuple(normalize_header(cell or "") for cell in grid[header_row])


def _resolve_headers(pages, grids, results):
    """One mapping per printed header, by majority across the pages sharing it.

    Returns the resolved mapping per header key, and how many keys were
    read more than one way. The count is not decoration: a resolved mapping
    now governs every page under that header, so on SH 99 it carries 457
    rows rather than 28, and a majority a reviewer cannot see is a guess.

    Ties break toward the first page's reading — earliest wins — because
    the alternative is an ordering nobody can predict from the document.
    """
    votes: dict[tuple[str, ...], list] = {}
    for page, completion in zip(pages, results):
        if completion.failed:
            continue
        result = completion.value
        grid_list = grids.get(page.page_no) or []
        table_index = result.get("matrix_table")
        if table_index is None or not (0 <= table_index < len(grid_list)):
            continue
        grid = grid_list[table_index]
        key = _header_key(grid, result.get("header_row"))
        if key is None:
            continue
        votes.setdefault(key, []).append(_column_mapping(grid, result))

    resolved: dict[tuple[str, ...], ColumnMapping] = {}
    disagreements = 0
    for key, cast in votes.items():
        signatures = [mapping.signature() for mapping in cast]
        if len(set(signatures)) > 1:
            disagreements += 1
        best = max(
            range(len(cast)),
            key=lambda i: (signatures.count(signatures[i]), -i),
        )
        resolved[key] = cast[best]
    return resolved, disagreements


def _structure_candidates(
    document: Document,
    page: DocPage,
    grids,
    result: dict,
    model: str | None,
    session: Session,
    carried: tuple[ColumnMapping, int] | None = None,
    resolved: dict | None = None,
) -> tuple[list[Candidate], tuple[ColumnMapping, int] | None]:
    table_index = result.get("matrix_table")
    if table_index is None or not (0 <= table_index < len(grids)):
        return [], carried
    grid = grids[table_index]
    if not grid:
        return [], carried

    header_row = result.get("header_row")
    mapping = _column_mapping(grid, result)
    confidence = result.get("mapping_confidence")

    if isinstance(header_row, int):
        # The mapping every page under this header agreed on, not this
        # page's own reading of it (#101).
        key = _header_key(grid, header_row)
        if resolved and key in resolved:
            mapping = resolved[key]
        carried = (mapping, len(grid[0]))
    elif carried is not None and carried[1] == len(grid[0]):
        # A continuation page: the matrix runs on but its headings were
        # printed once, pages ago. A printed header outranks a mapping
        # inferred from data, and the difference is not academic — on the
        # last page of Project A's oldest revision every owner cell reads
        # `NA`, which the model reasonably took for a size. That mapped the
        # owner column to the wrong field and dropped all 44 rows.
        mapping, _ = carried

    body = grid[header_row + 1 :] if isinstance(header_row, int) else grid
    inherited = _page_attributes(result)
    page_text = page.text or ""

    candidates = []
    for raw in body:
        own = row_to_fields(raw, mapping.fields)
        _settle_strategy(own, raw, mapping)
        if is_retired_row(own):
            # The form's own bookkeeping — a printed number whose only
            # content says the number is not in use (ADR-0012). Skipped by
            # rule, not by luck: page-inherited owners would otherwise
            # carry these past REQUIRED as phantom conflicts.
            continue
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
            confidence=confidence,
            model=model,
            tier=TIER_STRUCTURE,
            unmapped=mapping.unmapped,
        )
        session.add(candidate)
        candidates.append(candidate)
    return candidates, carried


def _settle_strategy(own: dict[str, str], raw, mapping: ColumnMapping) -> None:
    """Settle this row's resolution strategy, in place.

    Two layouts and one rule: whatever is stored has to be something the
    document said. A marked group answers with the heading the row is
    marked under; a value column answers with its cell, unless that cell is
    a bare mark — one column of a marked group with its siblings left
    unmapped, which is precisely how 9424 extracted before this.
    """
    if mapping.marks:
        if strategy := _marked_strategy(raw, mapping.marks):
            own[MARKED_COLUMN_FIELD] = strategy
        return
    _drop_bare_mark(own)


def _drop_bare_mark(fields: dict[str, str]) -> None:
    """Forget a resolution strategy that is only a mark, in place.

    `X` under a heading nobody kept says nothing: storing it would put a
    mark in the Ledger's `resolution_strategy` and in an Assertion as
    though the document had claimed it, which is what 9424 did on 71 rows.
    Dropping it leaves the row asserting nothing, the same answer a blank
    cell gives.
    """
    if (fields.get(MARKED_COLUMN_FIELD) or "").strip().casefold() in MARK_CELLS:
        del fields[MARKED_COLUMN_FIELD]


def _column_mapping(grid, result: dict) -> ColumnMapping:
    """What each column of this page's header holds.

    A field the model named that is not in the canonical vocabulary is
    treated exactly like one it declined to map: recorded by its printed
    name for a human to judge, never stored under a guessed heading. A
    wrong mapping is silent and applies to every row on the page.

    The resolution strategy is held back until every column has been read,
    because whether it is one column of prose or one of several marked
    columns is not knowable from any single column (#105).
    """
    header_row = result.get("header_row")
    headers = grid[header_row] if isinstance(header_row, int) and header_row < len(grid) else []

    fields: dict[int, str] = {}
    unmapped: list[str] = []
    strategy: dict[int, str] = {}
    for column in result.get("columns") or []:
        index = column.get("index")
        if not isinstance(index, int) or index < 0:
            continue
        field = column.get("canonical_field")
        # Coerced, because PyMuPDF returns None for a cell it read nothing
        # in and a real document carries one in its header row. #105
        # replaced `normalize_header`'s coercion here with a bare `.strip()`
        # and no fixture caught it; the M7 cold run did, mid-extraction.
        printed = (headers[index] or "").strip() if index < len(headers) else ""
        if field == MARKED_COLUMN_FIELD:
            strategy[index] = printed
            continue
        if field in ROW_FIELDS and field not in fields.values():
            fields[index] = field
            continue
        if normalize_header(printed):
            unmapped.append(printed)

    if len(strategy) > 1:
        # A marked group. The cells hold marks, so the answer is which
        # heading a row is marked under and the heading has to travel with
        # the index — which is exactly what a column-to-field map discards.
        return ColumnMapping(fields, unmapped, strategy)

    # One column: the prose layout every other project in the corpus uses,
    # read exactly as before.
    fields.update({index: MARKED_COLUMN_FIELD for index in strategy})
    return ColumnMapping(fields, unmapped, {})


def _marked_strategy(row, marks: dict[int, str]) -> str | None:
    """Which of the marked columns this row is marked under.

    A non-empty cell is a mark rather than a literal `X`: 9424 prints both
    `X` and `x`, and what makes a cell an answer is that the row was marked
    under that heading at all, not which glyph the typist reached for.

    Every marked heading is kept, in printed order. Twelve of 9424's rows
    carry two marks and one of those two disagree — `ST Relocation Needed`
    beside `Retain and Protect`, opposite sides of ADR-0009's line — so
    choosing between them here would assert a strategy the document does
    not. What they mean together is the vocabulary's question.
    """
    marked = [
        heading
        for index, heading in sorted(marks.items())
        if heading and index < len(row) and (row[index] or "").strip()
    ]
    return ANSWER_SEPARATOR.join(marked) or None


def _page_attributes(result: dict) -> dict[str, str]:
    attributes = result.get("page_attributes") or {}
    return {
        name: value.strip()
        for name in PAGE_FIELDS
        if (value := (attributes.get(name) or "").strip())
    }


def _low_confidence_tokens(completion) -> list[str]:
    """Numeric tokens the model was not sure of, from its own logprobs.

    Numeric only: prose wanders harmlessly and a low-probability word says
    little, but a hesitant digit is exactly the failure the gate measured
    and the one nothing else catches. Metadata about the call arrives on
    `Completion.meta`, beside the schema's answer rather than inside it,
    so it cannot reach a Candidate.
    """
    entries = (completion.meta.get("logprobs")) or []
    unsure = []
    for entry in entries:
        token = (entry.get("token") or "").strip()
        if len(token) < MIN_TOKEN_FRAGMENT:
            continue
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
    # This tier has no marked-group concept — the model writes what it sees
    # in the cell, and on a marked layout that is the mark. The same guard
    # as the structure tier, because the field is the same field and `X`
    # under a heading nobody kept is not a strategy however it was read.
    _drop_bare_mark(row)
    if not row:
        return None
    if is_retired_row(row):
        # The same rule as the structure tier (ADR-0012): a transcribed
        # `Not Used` row is still the form's bookkeeping.
        return None

    fields = {**inherited, **row}
    # Only tokens this row actually used: one shaky digit on a page must
    # not sink every other row on it.
    #
    # Matched as a substring, which over-attributes — the model's tokenizer
    # splits `1149+00` into fragments, so nothing else would match at all,
    # and a hesitant `114` will also flag a different row's `1149+00`.
    # Deliberately the safe direction: an extra sunk row costs a reviewer a
    # second look, a missed one costs a wrong number in the Ledger. Short
    # fragments are dropped so `9` does not flag the page.
    mine = sorted(
        {t for t in unsure if any(t in value for value in fields.values())}
    )

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

    return propose(
        document,
        kind="dependency",
        fields=fields,
        page_no=page.page_no,
        quote=quote,
        quote_verified=quote_appears_on(quote, page_text),
        whole_row=whole_row,
        confidence=confidence,
        prompt_version=PROMPT_VERSION,
        model=model,
        tier=tier,
        dedupe=dedupe_hint(fields),
        text_source=page.text_source,
        unverified=sorted(unverified_fields(fields, page_text)),
        unmapped=unmapped,
        low_confidence=low_confidence or [],
    )
