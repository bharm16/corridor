"""The measured native-glyph assignment, with everything it decided kept (#810, ADR-0094).

ADR-0094 measured lane A with the imported `textract.remap.remap_page`: each
of the reader's glyphs goes to the Textract cell whose polygon holds the
glyph's own ink-box centre, the nearer cell centre wins a shared border, and a
cell's text is `ordered_text` over the glyphs it holds. That module is
imported bytes and stays frozen, and its result was built for the experiment
rather than for the record: it writes the assigned text over the cell, keeps a
glyph *count* in place of the glyphs, drops the OCR `word_ids`, and folds the
loose glyphs into strings. Production needs what it threw away. A locator has
to be built from the boxes of the very glyphs that supplied a value, the
Textract observation and its own words have to stay retrievable beside the
native value, and an empty cell, a hidden run and a glyph no cell holds have
to be stated rather than lost.

This module is the same assignment with its result kept whole. It calls the
frozen module's placement primitives (`polygon_of`, `contains`, `centre`,
`glyph_box`, `union_box`) and the reader's own `ordered_text`, so a cell's
text here is the text `remap_page` would have written — `tests/
test_textract_adapter.py` holds that equality — and it never mutates the page
it was given. Two things it does not do. It does not implement the two
measured variants (`remap_margin`, `rescue_runs`); both are off in the
specified lane A and a caller wanting them still has `identity.normalize`.
And it does not decide whether a cell *should* take its native value: that is
the consuming route's composition per region, and the authorization that
composition needs is checked there, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from corridor_pdf_reader.replacement.layout import ordered_text
from corridor_pdf_reader.textract.remap import (
    TEXT_SOURCE,
    centre,
    contains,
    glyph_box,
    polygon_of,
    union_box,
)
from corridor_pdf_reader.textract_adapter.identity import NativeGlyphs

Box = tuple[float, float, float, float]


def _box(values: list[float]) -> Box:
    x0, y0, x1, y1 = (float(value) for value in values)
    return (x0, y0, x1, y1)


@dataclass(frozen=True)
class AssignedGlyph:
    """One of the reader's glyphs, by the identity and box the assignment placed it with."""

    text: str
    # The box the glyph was placed by: its ink box when the reader gave one,
    # its display box otherwise, in the reader's displayed crop. The locator
    # of a cell is the union of these.
    box: Box
    object_id: int | None
    source_index: int | None


@dataclass(frozen=True)
class AssignedCell:
    """One Textract cell after the assignment: the glyphs it holds and the words it read.

    Both sides are kept. `text` and `locator` are the document's own — the
    glyphs in the reader's order and the union of their boxes — and are `""`
    and `None` for a cell no glyph fell in, which is a stated fact about that
    cell rather than a missing one. `ocr_text`, `ocr_word_ids` and
    `ocr_confidence` are what the observation read there, untouched.
    """

    table: int
    row: int
    column: int
    box: Box
    glyphs: tuple[AssignedGlyph, ...]
    text: str
    locator: Box | None
    ocr_text: str
    ocr_word_ids: tuple[str, ...]
    ocr_confidence: float | None

    @property
    def empty(self) -> bool:
        return not self.glyphs


@dataclass(frozen=True)
class NativeGlyphAssignment:
    """The assignment of one page's glyphs to one Textract observation of it.

    `observation` is the page the adapter returned, not a copy with the text
    replaced. `unassigned` are the visible glyphs no cell polygon held;
    `clipped` are the reader's hidden runs, kept as the clipped evidence they
    are. Nothing here is written back onto the observation.
    """

    observation: dict[str, Any]
    cells: tuple[AssignedCell, ...]
    unassigned: tuple[AssignedGlyph, ...]
    clipped: tuple[AssignedGlyph, ...]
    text_source: str = TEXT_SOURCE

    def cell(self, table: int, row: int, column: int) -> AssignedCell | None:
        for cell in self.cells:
            if (cell.table, cell.row, cell.column) == (table, row, column):
                return cell
        return None

    @property
    def assigned_cells(self) -> tuple[AssignedCell, ...]:
        return tuple(cell for cell in self.cells if not cell.empty)


def _glyph(char: dict[str, Any]) -> AssignedGlyph:
    object_id = char.get("object_id")
    source_index = char.get("source_index")
    return AssignedGlyph(
        text=str(char["text"]),
        box=_box(glyph_box(char)),
        object_id=None if object_id is None else int(object_id),
        source_index=None if source_index is None else int(source_index),
    )


def assign_native_glyphs(page: dict[str, Any], glyphs: NativeGlyphs) -> NativeGlyphAssignment:
    """Place every visible glyph in the cell whose polygon holds its centre, and keep the result.

    The placement is `remap_page`'s, in the specified lane A: whitespace
    glyphs are not placed, a glyph is held by every cell polygon containing
    its ink-box centre (a point on the border counts), and where two cells
    hold it the nearer cell centre wins. Cells are visited in the page's own
    order — every table, every cell — so the result lines up with the
    observation index for index.
    """

    tables = page.get("tables") or ()
    located: list[tuple[int, dict[str, Any]]] = [
        (table_no, cell) for table_no, table in enumerate(tables) for cell in table.get("cells") or ()
    ]
    polygons = [polygon_of(cell) for _, cell in located]
    centres = [centre([float(value) for value in cell["box"]]) for _, cell in located]
    assigned: list[list[dict[str, Any]]] = [[] for _ in located]
    loose: list[dict[str, Any]] = []
    for char in glyphs.characters:
        if not str(char.get("text", "")).strip():
            continue
        x, y = centre(glyph_box(char))
        holders = [k for k, polygon in enumerate(polygons) if contains(polygon, x, y)]
        if not holders:
            loose.append(char)
            continue
        best = min(holders, key=lambda k: (centres[k][0] - x) ** 2 + (centres[k][1] - y) ** 2)
        assigned[best].append(char)
    cells = tuple(
        AssignedCell(
            table=table_no,
            row=int(cell["row"]),
            column=int(cell["column"]),
            box=_box(cell["box"]),
            glyphs=tuple(_glyph(char) for char in held),
            text=ordered_text(held) if held else "",
            locator=_box(union_box([glyph_box(char) for char in held])) if held else None,
            ocr_text=str(cell.get("text") or ""),
            ocr_word_ids=tuple(str(word) for word in cell.get("word_ids") or ()),
            ocr_confidence=None if cell.get("confidence") is None else float(cell["confidence"]),
        )
        for (table_no, cell), held in zip(located, assigned, strict=True)
    )
    return NativeGlyphAssignment(
        observation=page,
        cells=cells,
        unassigned=tuple(_glyph(char) for char in loose),
        clipped=tuple(_glyph(char) for char in glyphs.clipped if str(char.get("text", "")).strip()),
    )
