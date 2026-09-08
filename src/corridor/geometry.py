"""Shared row formatting for workbook extraction and legacy citation readback.

PDF word boxes and table detection were retired in #766. Workbooks still use
these value/quote helpers, and old citations retain their original text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from corridor.candidates import dedupe_hint as join_hint
from corridor.verify import quote_appears_on

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
