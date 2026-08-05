"""Reading a page's table geometry — cells, not meanings.

What survives of the deterministic matrix extractor after #63. It answers
"what characters are in this cell", and deliberately not "what does this
column mean". The half that answered the second question mapped column
headings by synonym, and it went because that is what could not
generalise: two FDOT documents in this corpus do not share a header row,
and on SR 789 it found the table and mapped none of its columns. ADR-0006
gives that question to a model.

The half that stayed is the half that was measured right. Reading values
off word boxes got 3 of 3,235 rows wrong on Project A where a model
transcribing the same pages got 164 of 3,240, and Tier 1 depends on it for
every value it stores — so this is load-bearing now in a way it never was
when a parser sat on top of it.

Two behaviours here were bought with real defects and must not be lost:

- **Cells are rebuilt from word boxes, not text spans** (#43, #44).
  `table.extract()` drops a leading character (`Rothwell Street` ->
  `othwell Street`), absorbs the first character of the next cell, and
  omits separators mid-value (`City` + `of Houston` -> `Cityof Houston`).
  Word boxes are cut on glyph gaps and assigned to exactly one cell.
- **Word boxes are transformed into the table's coordinate space.**
  `get_text("words")` measures against the mediabox and `find_tables()`
  against the displayed, rotated page. On the two 90-degree revisions
  those are different spaces, and the two readings described unrelated
  content until the transform.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

import pymupdf

from corridor.candidates import dedupe_hint as join_hint
from corridor.verify import quote_appears_on

# Share of a table's cells whose two readings must match before the word-box
# reading is trusted for any of them. Aligned tables sit at ~99.5%; a table
# read in the wrong coordinate space sat at ~2%. Anywhere in between should
# be looked at rather than silently repaired.
AGREEMENT_FLOOR = 0.5

_WS = re.compile(r"\s+")

# `135+58.68, 236.85' LT` — stationing, offset, side, in one cell.
_STATION_OFFSET = re.compile(
    r"^(?P<station>\d{1,5}\s*\+\s*\d{1,2}(?:\.\d+)?)"
    r"(?:\s*,\s*(?P<offset>\d+(?:\.\d+)?)\s*'?)?"
    r"\s*(?P<side>LT|RT|[LR])?\.?\s*$",
    re.IGNORECASE,
)


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


def row_to_fields(row: list[str | None], mapping: dict[int, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for index, field in mapping.items():
        if index >= len(row):
            continue
        value = _WS.sub(" ", (row[index] or "").replace("\n", " ")).strip()
        if value:
            fields[field] = value
    return split_station_offset(fields)


def split_station_offset(fields: dict[str, str]) -> dict[str, str]:
    """Separate `135+58.68, 236.85' LT` into stationing, offset and side.

    FDOT and CDOT carry all three in one column where TxDOT uses four. A
    header map moves a column, it cannot divide one, so the split happens
    after mapping — and only when the value actually carries an offset, so
    that a plain TxDOT `1149+00` passes through untouched.
    """
    for station_key, offset_key in (
        ("station_from", "offset_from"),
        ("station_to", "offset_to"),
    ):
        value = fields.get(station_key)
        if not value:
            continue
        match = _STATION_OFFSET.match(value)
        if not match or not match.group("offset"):
            continue
        fields[station_key] = match.group("station")
        fields.setdefault(offset_key, match.group("offset"))
        side = match.group("side")
        if side:
            fields.setdefault("offset_side", side.upper())
    return fields


def row_quote(row: list[str | None]) -> str:
    """The whole row is the evidence.

    PyMuPDF reads these tables cell-by-cell in row order, so the non-empty
    cells joined by spaces appear verbatim in the page text — verified at
    ratio 1.000 against the real matrices.
    """
    cells = [_WS.sub(" ", (c or "").replace("\n", " ")).strip() for c in row]
    return " ".join(c for c in cells if c)


def page_words(page) -> list[tuple[float, float, float, float, str]]:
    """Word boxes in the coordinate space `find_tables()` reports bboxes in.

    `get_text("words")` measures against the mediabox; `find_tables()`
    measures against the displayed, rotated page. On the two 90-degree
    revisions those are different spaces, which is why word boxes and table
    cells appeared to describe unrelated content until this transform.
    """
    rotation = page.rotation_matrix
    words = []
    for word in page.get_text("words"):
        box = pymupdf.Rect(word[:4]) * rotation
        words.append((box.x0, box.y0, box.x1, box.y1, word[4]))
    return words


def cell_from_words(bbox, words) -> str:
    """Cell text rebuilt from the page's word boxes.

    A word belongs to the cell its centre falls in, so it lands in exactly
    one — which is what `table.extract()` gets wrong in both directions.
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


def cell_text(extracted: str | None, rebuilt: str) -> str | None:
    """The word-box reading, falling back to the span reading.

    `table.extract()` concatenates text spans and is sloppy at cell
    boundaries in both directions: it drops a leading character (`Rothwell
    Street` -> `othwell Street`) and absorbs the first character of the
    next cell (`Canal Street` -> `Canal Street o`). It also omits the
    separator where a span boundary falls mid-value (`City` + `of Houston`
    -> `Cityof Houston`).

    Word boxes are cut on glyph gaps and assigned to exactly one cell, so
    they can do none of those. Across the eight real matrices the two
    readings differ on 393 of 79,588 cells, and the word-box reading is
    right in every one.

    An empty rebuild is no evidence, not contrary evidence — a cell whose
    words could not be located keeps whatever the span reader found rather
    than being blanked.
    """
    return rebuilt or extracted


def readings_agree(pairs: Collection[tuple[str | None, str]]) -> bool:
    """Do the two readings describe the same table?

    Trusting word boxes per cell is only safe while both readings agree
    about what the table says. When the coordinate spaces do not line up
    they disagree wholesale — 98% of cells, with `Utility ID` reading as
    `No) Conflict Y` — and repairing cell by cell would confidently
    overwrite every one. So agreement is established once per table and a
    table that fails is left entirely alone.

    Compared ignoring whitespace, because a missing separator is the most
    common real difference and is not disagreement about content.
    """
    agree = total = 0
    for extracted, rebuilt in pairs:
        if not extracted and not rebuilt:
            continue
        total += 1
        if _WS.sub("", extracted or "") == _WS.sub("", rebuilt):
            agree += 1
    return total == 0 or agree / total >= AGREEMENT_FLOOR


def reread_table(data: list[list], table, words) -> list[list]:
    """`table.extract()` re-read from word boxes, when the two agree."""
    rebuilt: list[list[str]] = []
    for row_index, raw in enumerate(data):
        if row_index >= len(table.rows):
            rebuilt.append(["" for _ in raw])
            continue
        row = table.rows[row_index]
        # Narrow to this row's band first: every cell would otherwise scan
        # every word on the page, which is ~800 for these matrices.
        band = [w for w in words if row.bbox[1] <= (w[1] + w[3]) / 2 <= row.bbox[3]]
        rebuilt.append(
            [
                cell_from_words(row.cells[i], band) if i < len(row.cells) else ""
                for i, _ in enumerate(raw)
            ]
        )

    pairs = [
        (cell, rebuilt[r][c])
        for r, raw in enumerate(data)
        for c, cell in enumerate(raw)
    ]
    if not readings_agree(pairs):
        return data

    return [
        [cell_text(cell, rebuilt[r][c]) for c, cell in enumerate(raw)]
        for r, raw in enumerate(data)
    ]


def page_tables(page) -> list[list[list[str]]]:
    """Every table on one page, cells read from the page's word boxes.

    The whole public point of this module. A caller supplies the column
    mapping — Tier 1 gets it from a model, which is the part no synonym
    table could generalise — and these are the values it maps.
    """
    words = page_words(page)
    grids = []
    for table in page.find_tables().tables:
        data = table.extract()
        if data:
            grids.append(reread_table(data, table, words))
    return grids


def dedupe_hint(fields: dict[str, str]) -> str:
    """What discriminates one matrix row from another: party, kind, where.

    The join itself is `candidates.dedupe_hint`, shared with the extractors
    that read prose and discriminate on different parts. `merge` blocks on
    the result, so the separator is one rule even where the parts are not.
    """
    return join_hint(
        fields.get("external_org", ""),
        fields.get("utility_type", ""),
        f"{fields.get('station_from', '')}-{fields.get('station_to', '')}",
    )


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
