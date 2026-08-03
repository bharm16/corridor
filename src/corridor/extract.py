"""Deterministic extractor for the TxDOT Utility Conflict Matrix family.

There is no single TxDOT UCM layout. Across five revisions of *the same
document* on Project A the title alternates between "Utility Inventory
Matrix" and "Utility Conflict Matrix", a `Data Source` column comes and
goes, `Potential Conflict` and `SUE Level` appear partway through, and
`Size (inches, strands)` becomes `Size (in)`. So headers are mapped by
synonym rather than by position, and a column that is absent is absent —
not empty.

The critical failure mode is silence. A parser keyed to one layout returns
zero rows on the others *without erroring*, and a zero-row extraction is
indistinguishable from a document that genuinely has no conflicts. So the
two cases are distinguished explicitly: no recognizable table is an error,
a recognizable table with no data rows is a legitimate empty result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

from corridor.verify import quote_appears_on

PROMPT_VERSION = "txdot_ucm_v1"

# Longest matching prefix wins, so "START STA OFFSET" beats "START STA".
HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "utility_id": ("UTILITY ID",),
    "external_org": ("UTILITY OWNER", "OWNER"),
    "utility_type": ("UTILITY TYPE", "FACILITY TYPE"),
    "size": ("SIZE & MATERIAL", "SIZE"),
    "material": ("MATERIAL",),
    "oh_ug": ("OH/ UG", "OH/UG", "UG/OH"),
    "baseline": ("BASELINE",),
    "orientation": ("PARALLEL, CROSSING", "LONGITUDINAL"),
    "alignment": ("ALIGNMENT",),
    "location_start": ("LOCATION START",),
    "location_end": ("LOCATION END",),
    "station_from": ("START STATION", "START STA"),
    "station_to": ("END STATION", "END STA"),
    "offset_from": ("START STA OFFSET", "START OFFSET"),
    "offset_to": ("END STA OFFSET", "END OFFSET"),
    "offset_side": ("OFFSET L/R", "L/R"),
    "potential_conflict": ("POTENTIAL",),
    "sue_level": ("SUE LEVE",),
    "data_source": ("DATA SOURCE",),
    "notes": ("NOTES",),
}

# A table without both of these is not a utility matrix.
REQUIRED = ("utility_id", "external_org")

_WS = re.compile(r"\s+")


class NoMatrixFound(Exception):
    """No table on any page had recognizable utility-matrix headers.

    Raised rather than returning an empty list, because those two outcomes
    mean completely different things and only one of them is a bug.
    """


@dataclass(frozen=True)
class MatrixRow:
    fields: dict[str, str]
    page_no: int
    quote: str
    cells: tuple[str, ...] = ()


# A quote shorter than this is too weak to be evidence of anything.
MIN_QUOTE_CHARS = 12
# How far into the row to try starting a fallback quote. The observed cause
# is a leading column (Data Source) that sits elsewhere in reading order.
MAX_QUOTE_START = 4


def normalize_header(header: str | None) -> str:
    return _WS.sub(" ", (header or "").replace("\n", " ")).strip().upper()


def canonical_field(header: str | None) -> str | None:
    normalized = normalize_header(header)
    if not normalized:
        return None
    best: tuple[str, int] | None = None
    for field, patterns in HEADER_SYNONYMS.items():
        for pattern in patterns:
            if normalized.startswith(pattern):
                if best is None or len(pattern) > best[1]:
                    best = (field, len(pattern))
    return best[0] if best else None


def map_headers(header_row: list[str | None]) -> dict[int, str]:
    """Column index -> canonical field, for the columns we recognize."""
    mapping: dict[int, str] = {}
    for index, header in enumerate(header_row):
        field = canonical_field(header)
        if field and field not in mapping.values():
            mapping[index] = field
    return mapping


def row_to_fields(row: list[str | None], mapping: dict[int, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for index, field in mapping.items():
        if index >= len(row):
            continue
        value = _WS.sub(" ", (row[index] or "").replace("\n", " ")).strip()
        if value:
            fields[field] = value
    return fields


def row_quote(row: list[str | None]) -> str:
    """The whole row is the evidence.

    PyMuPDF reads these tables cell-by-cell in row order, so the non-empty
    cells joined by spaces appear verbatim in the page text — verified at
    ratio 1.000 against the real matrices.
    """
    cells = [_WS.sub(" ", (c or "").replace("\n", " ")).strip() for c in row]
    return " ".join(c for c in cells if c)


def cell_from_words(bbox, words) -> str:
    """Cell text rebuilt from the page's word boxes.

    PyMuPDF builds a cell by concatenating text spans, and where a span
    boundary falls mid-value it emits no separator — `City` + `of Houston`
    arrives as `Cityof Houston`. Word boxes are cut on glyph gaps instead,
    so joining them puts the space back.
    """
    if bbox is None:
        return ""
    x0, y0, x1, y1 = bbox
    inside = [
        w
        for w in words
        if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1
    ]
    # Line-major, then left to right. The y bucket keeps a slightly ragged
    # line from scattering into per-word "lines".
    inside.sort(key=lambda w: (round(w[1] / 3), w[0]))
    return " ".join(w[4] for w in inside)


def restore_separator(extracted: str | None, rebuilt: str) -> str | None:
    """`extracted` respaced from `rebuilt`, but only if nothing else differs.

    Deliberately narrow: the rebuild is trusted for whitespace alone, and
    only where the two agree on every other character.

    The guard is load-bearing, not defensive. Two of Project A's five
    revisions are rotated 90 degrees, and there `find_tables()` bboxes and
    `get_text("words")` coordinates are in different spaces — 98% of cells
    disagree and the rebuild is unrelated text (`Utility ID` rebuilds as
    `No) Conflict Y`). Requiring character equality makes those pages fall
    through untouched instead of being overwritten with a confident-looking
    wrong value, which would be this bug again in a worse form.
    """
    if not extracted or not rebuilt:
        return extracted
    if _WS.sub("", extracted) != _WS.sub("", rebuilt):
        return extracted
    return rebuilt


def respace_table(data: list[list], table, words) -> list[list]:
    """`table.extract()` with dropped separators restored, cell by cell."""
    repaired = []
    for row_index, raw in enumerate(data):
        if row_index >= len(table.rows):
            repaired.append(list(raw))
            continue
        row = table.rows[row_index]
        # Narrow to this row's band first: every cell would otherwise scan
        # every word on the page, which is ~800 for these matrices.
        band = [w for w in words if row.bbox[1] <= (w[1] + w[3]) / 2 <= row.bbox[3]]
        repaired.append(
            [
                restore_separator(cell, cell_from_words(row.cells[i], band))
                if i < len(row.cells)
                else cell
                for i, cell in enumerate(raw)
            ]
        )
    return repaired


def dedupe_hint(fields: dict[str, str]) -> str:
    return "|".join(
        [
            fields.get("external_org", ""),
            fields.get("utility_type", ""),
            f"{fields.get('station_from', '')}-{fields.get('station_to', '')}",
        ]
    )


def extract_rows(path) -> list[MatrixRow]:
    """Every data row of every recognizable matrix table in the document.

    A matrix spans many pages and the header is printed only on the first.
    The mapping is therefore carried forward to same-width tables on later
    pages, where the first row is data rather than a header. Without this
    the extractor reads page 1 and silently drops the rest — 75 rows
    instead of 622 on Project A's first revision, with no error, because
    page 1 *was* recognized.
    """
    rows: list[MatrixRow] = []
    recognized_tables = 0
    carried: dict[int, str] | None = None
    carried_width: int | None = None

    with pymupdf.open(path) as pdf:
        for index, page in enumerate(pdf):
            page_no = index + 1
            words = page.get_text("words")
            for table in page.find_tables().tables:
                data = table.extract()
                if not data:
                    continue
                data = respace_table(data, table, words)

                mapping = map_headers(data[0])
                if all(f in mapping.values() for f in REQUIRED):
                    recognized_tables += 1
                    carried, carried_width = mapping, len(data[0])
                    body = data[1:]
                elif carried is not None and carried_width == len(data[0]):
                    # Continuation of the matrix: no header row, so nothing
                    # is skipped. Width must match, which excludes the
                    # unrelated legend tables that share these pages.
                    mapping, body = carried, data
                else:
                    continue

                for raw in body:
                    fields = row_to_fields(raw, mapping)
                    # Guards the carried mapping: a same-width table that is
                    # not the matrix will not produce these two fields.
                    if not all(fields.get(f) for f in REQUIRED):
                        continue
                    cells = tuple(
                        _WS.sub(" ", (c or "").replace("\n", " ")).strip()
                        for c in raw
                    )
                    rows.append(
                        MatrixRow(fields, page_no, row_quote(raw), cells)
                    )

    if recognized_tables == 0:
        raise NoMatrixFound(
            f"no table with utility-matrix headers in {path}. "
            "A layout variant is unhandled — do not treat this as an empty matrix."
        )
    return rows


def best_verifiable_quote(row: MatrixRow, page_text: str) -> tuple[str, bool]:
    """The strongest quote for this row that actually appears on the page.

    The whole row is preferred, and usually works: PyMuPDF reads these
    tables cell-by-cell in row order. On a minority of pages it does not —
    a page carrying two tables side by side, or a leading `Data Source`
    column that lands elsewhere in reading order — and there the full row
    is not contiguous text even though every cell is genuinely present.

    Falling back to the longest contiguous window that does verify keeps
    the citation real. The alternative is citing text that is not on the
    page, which is precisely the failure the verifier exists to catch.

    Returns (quote, is_whole_row).
    """
    if quote_appears_on(row.quote, page_text):
        return row.quote, True

    tokens = row.quote.split(" ")
    best = ""
    for start in range(min(MAX_QUOTE_START, len(tokens))):
        for end in range(len(tokens), start + 1, -1):
            candidate = " ".join(tokens[start:end])
            if len(candidate) < MIN_QUOTE_CHARS:
                break
            if quote_appears_on(candidate, page_text):
                if len(candidate) > len(best):
                    best = candidate
                break
    return (best or row.quote), False


def to_candidates(
    rows: list[MatrixRow], *, document_id: int, page_text: dict[int, str]
) -> list[dict]:
    """Candidate payloads in the shared extractor schema.

    Citations are verified here rather than downstream, so a candidate
    carrying an unfindable quote is visibly unverified in the queue instead
    of looking like every other row. Nothing is ever dropped.
    """
    candidates = []
    for row in rows:
        text = page_text.get(row.page_no, "")
        quote, whole_row = best_verifiable_quote(row, text)
        candidates.append(
            {
                "kind": "dependency",
                "fields": row.fields,
                "citations": [
                    {
                        "document_id": document_id,
                        "page": row.page_no,
                        "quote": quote,
                        "verified": quote_appears_on(quote, text),
                        # False means the citation covers part of the row.
                        # Surfaced so a reviewer can see that some asserted
                        # fields sit outside the quoted span.
                        "whole_row": whole_row,
                    }
                ],
                "confidence": 1.0,
                "dedupe_hint": dedupe_hint(row.fields),
            }
        )
    return candidates
