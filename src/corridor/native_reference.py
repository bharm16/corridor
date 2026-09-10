"""Author a native machine reference from source cells, independently of proposals.

The model's column mapping cannot author its own denominator. This recipe
uses the existing printed WSDOT header, marked-resolution and retirement
rules over a fresh original-byte reading. It never reads Candidates, model
answers or extraction runs. Shared reconstruction and vocabulary still make
the resulting enumeration a semi-independent ceiling, not field gold.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from corridor.adjudicate import WSDOT_APPENDIX_U
from corridor.models import CRITICAL_STRATEGIES, is_critical
from corridor.reader_segments import native_segment_values
from corridor.reference_methods import NATIVE_AUTHORING_SCHEMA
from corridor.token_layers import read_native_pdf
from corridor.vocabulary import RETIREMENT_PHRASES, is_retired_row

# The behaviour of the three borrowed rules -- WSDOT's printed resolution
# reading, the critical-strategy test and the retirement test -- as a declared
# version rather than as their source text. See ``authoring_identity``.
NATIVE_REFERENCE_RULES_VERSION = "native-reference-rules-v1"

_ANCHOR = "recommended resolution"
_OWNER_HEADINGS = ("owner", "utility owner")
_ID_HEADINGS = ("conflict id", "id conflict", "utility id")


@dataclass(frozen=True)
class NativeReferenceRows:
    rows: tuple[tuple[str, int, str], ...]
    retired: int
    empty_slots: int
    pages: tuple[int, ...]
    reading_json: str


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def authoring_identity() -> dict:
    """Pin this recipe and its actual rules, excluding legacy modules and logs.

    **Why the rules digest no longer hashes source text.** It used to hash
    ``inspect.getsource`` of ``WSDOT_APPENDIX_U.read``, ``is_critical`` and
    ``is_retired_row``. ``getsource`` returns the text as written, so a
    reflowed condition, a renamed local or a corrected comment anywhere in
    ``adjudicate``, ``models`` or ``vocabulary`` changed ``rules_sha256`` while
    every enumeration those rules produce stayed identical. A published native
    reference records this identity, and ``replay_machine_reference`` refuses a
    recipe whose identity differs, so an edit of that kind retired references
    that nothing had actually invalidated. The same reasoning retired the
    equivalent hashing in ``token_layers.native_integration_digest``.

    The three rules are not this module's to hash by bytes either: each lives
    in a large module full of behaviour a native reference does not read, so
    ``sha256`` of those files would fire even more often than ``getsource``
    did. What pins them instead is the declarative half they actually read --
    the printed phrase lists and the critical-strategy set -- plus
    ``NATIVE_REFERENCE_RULES_VERSION`` for the reading code itself.

    The cost is that the constant is declared rather than derived: a behaviour
    change to any of those three readers must bump it in the same change,
    exactly as a prompt version or a migration revision must be advanced
    deliberately. ``recipe_sha256`` still covers this module byte for byte, and
    the recorded reading identity still covers the reader's actual result.
    """
    rules = {
        "rules_version": NATIVE_REFERENCE_RULES_VERSION,
        "resolution_phrases": WSDOT_APPENDIX_U.phrases,
        "critical_strategies": sorted(CRITICAL_STRATEGIES),
        "retirement_phrases": RETIREMENT_PHRASES,
    }
    return {
        "schema_version": NATIVE_AUTHORING_SCHEMA,
        "recipe_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "rules_sha256": sha256(_canonical(rules).encode()).hexdigest(),
    }


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold()


def _cell_at(cells: list, column: int | None):
    if column is None:
        return None
    found = [
        cell
        for cell in cells
        if cell.cell_column <= column < cell.cell_column + cell.column_span
    ]
    if len(found) > 1:
        raise ValueError("native reference has ambiguous source cell membership")
    return found[0] if found else None


def _text_at(cells: list, column: int | None) -> str:
    cell = _cell_at(cells, column)
    return cell.exact_text.strip() if cell is not None else ""


def _column(headers: list, names: tuple[str, ...]) -> int | None:
    matches = [cell.cell_column for cell in headers if _norm(cell.exact_text) in names]
    if len(matches) > 1:
        raise ValueError("native reference header column is ambiguous")
    return matches[0] if matches else None


def _header_row(grid: list) -> int | None:
    found = []
    for row in sorted({cell.cell_row for cell in grid}):
        headers = [cell for cell in grid if cell.cell_row == row]
        if (
            any(WSDOT_APPENDIX_U.read(cell.exact_text) for cell in headers)
            and _column(headers, _OWNER_HEADINGS) is not None
            and _column(headers, _ID_HEADINGS) is not None
        ):
            found.append(row)
    if len(found) > 1:
        # The reader can combine disconnected drawn grids into one table.
        # A second complete header is still a second eligible grid, not data.
        raise ValueError(
            "native reference has multiple eligible WSDOT grids on one page"
        )
    return found[0] if found and found[0] < 6 else None


def read_reference_document(path: Path, document_sha256: str) -> NativeReferenceRows:
    """Enumerate one source's cells without consulting its extracted population."""
    reading = read_native_pdf(
        path, source_sha256=document_sha256, engine="tagged", dpi=36
    )
    cells = defaultdict(list)
    for value in native_segment_values(reading):
        if value.kind == "pdf_cell":
            if value.row_span != 1:
                raise ValueError(
                    "native reference recipe does not support row-spanning cells"
                )
            cells[(value.page_no, value.table_index)].append(value)
    expected = Counter(
        (page["number"], table_index, cell["row"], cell["column"])
        for page in reading.pages
        for table_index, table in enumerate(page["tables"]["value"])
        for cell in table["structured_cells"]
        if cell["text"]
    )
    actual = Counter(
        (page_no, table, cell.cell_row, cell.cell_column)
        for (page_no, table), values in cells.items()
        for cell in values
    )
    if expected != actual:
        raise ValueError(
            "native reference has nonempty reader cells without unique typed source values"
        )
    grids = {}
    for page in reading.pages:
        tables = [
            values
            for (page_no, _), values in cells.items()
            if page_no == page["number"]
        ]
        eligible = [table for table in tables if _header_row(table) is not None]
        if len(eligible) > 1:
            raise ValueError(
                f"native reference page {page['number']} has multiple eligible WSDOT grids"
            )
        if any(
            any(_norm(cell.exact_text) == _ANCHOR for cell in table)
            and _header_row(table) is None
            for table in tables
        ):
            raise ValueError(
                "native reference has an anchored grid without supported headings"
            )
        if eligible:
            grids[page["number"]] = eligible[0]
        elif page["characters"]["value"] or page["clipped"]["value"]:
            raise ValueError(
                f"native reference page {page['number']} has populated content without supported headings"
            )
    if not any(
        _norm(cell.exact_text) == _ANCHOR for grid in grids.values() for cell in grid
    ):
        raise ValueError("native reference source has no recommended resolution anchor")

    rows = []
    retired = empty_slots = 0
    anchored = False
    for page_no, grid in sorted(grids.items()):
        grouped = defaultdict(list)
        for cell in sorted(grid, key=lambda item: (item.cell_row, item.cell_column)):
            grouped[cell.cell_row].append(cell)
        header_row = _header_row(grid)
        if header_row is None:
            # A continuation page without recognized headings is outside
            # this recipe; never silently publish a partial denominator.
            raise ValueError(
                f"native reference page {page_no} has no recognized resolution headings"
            )
        headers = grouped[header_row]
        owner_col = _column(headers, _OWNER_HEADINGS)
        id_col = _column(headers, _ID_HEADINGS)
        notes_col = _column(headers, ("notes",))
        if owner_col is None or id_col is None:
            raise ValueError(
                "native reference needs printed owner and identifier headings"
            )
        mark_headers = [
            cell for cell in headers if WSDOT_APPENDIX_U.read(cell.exact_text)
        ]
        anchored = True
        for row_number, body in sorted(grouped.items()):
            if row_number <= header_row:
                continue
            owner, ref = _text_at(body, owner_col), _text_at(body, id_col)
            notes = _text_at(body, notes_col)
            if not owner and not ref:
                continue
            if not owner:
                if is_retired_row({"utility_id": ref, "notes": notes}):
                    retired += 1
                else:
                    empty_slots += 1
                continue
            if not ref:
                raise ValueError(
                    f"native reference page {page_no} has an occupied row without an identifier"
                )
            strategies = {
                WSDOT_APPENDIX_U.read(header.exact_text)
                for header in mark_headers
                if any(
                    _text_at(body, column)
                    for column in range(
                        header.cell_column, header.cell_column + header.column_span
                    )
                )
            }
            sides = {is_critical(strategy) for strategy in strategies}
            critical = ("yes" if sides == {True} else "no") if len(sides) == 1 else ""
            rows.append((ref, page_no, critical))
    if not anchored:
        raise ValueError("native reference has no supported WSDOT header")
    return NativeReferenceRows(
        tuple(rows),
        retired,
        empty_slots,
        tuple(page["number"] for page in reading.pages),
        _canonical(
            {
                "document_sha256": document_sha256,
                "reading_sha256": reading.reading_sha256,
                "reader_identity": reading.identity,
            }
        ),
    )
