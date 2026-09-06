"""The imported reader on the imported fixture PDFs, through the package's own entry (#729).

The eleven fixtures under `corpus/fixtures/` were generated independently of
every engine at commit c39363e and carry declared outcomes in
`corpus/fixtures.json`: four rotations with and without a CropBox holding a
known sentence and a known 2x2 drawn table, a raster-only page, malformed
bytes, and a password. The source repository's own reader tests built these
with reportlab and compared against the PyMuPDF baseline; neither may enter
Corridor, so the same expectations are asserted here against the committed
bytes, with no corpus, node or network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corridor_pdf_reader.execution import read_document
from corridor_pdf_reader.replacement.pages import cell_id, outside_id, slim_page

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "corridor_pdf_reader"
FIXTURES = {
    fixture["id"]: fixture
    for fixture in json.loads((PACKAGE_ROOT / "corpus" / "fixtures.json").read_text(encoding="utf-8"))
}
TEXT_FIXTURES = sorted(name for name, fixture in FIXTURES.items() if fixture["expected"]["kind"] == "text")


def _path(name: str) -> Path:
    return PACKAGE_ROOT / FIXTURES[name]["path"]


DRAWN = {"Owner", "Status", "Gas", "Open"}
SENTENCE = "UTILITY 1149+00"


@pytest.mark.parametrize("engine", ["tagged", "pdfium"])
@pytest.mark.parametrize("name", TEXT_FIXTURES)
def test_the_reader_respects_rotation_and_crop_and_recovers_the_known_table(name: str, engine: str):
    """Both engines read the drawn 2x2 grid in the displayed frame.

    They differ, as measured, on the sentence outside it: the `tagged`
    engine's reconstructor makes every leftover run a cell (loop-006 in
    bootstrap/LOOP-LOG.md), so the sentence joins the table as a cell of its
    own; the drawn-grid engine leaves it outside every table.
    """

    expected = FIXTURES[name]["expected"]
    result = read_document(_path(name), [1], engine=engine, dpi=72)
    page = result["pages"][0]

    assert result["source_sha256"] == FIXTURES[name]["sha256"]
    assert page["geometry"]["rotation"] == expected["rotation"]
    assert page["geometry"]["width"] == expected["width"]
    assert page["geometry"]["height"] == expected["height"]
    displayed = [expected["width"], expected["height"]]
    if expected["rotation"] % 180:
        displayed.reverse()
    assert page["render"]["value"]["size"] == displayed
    assert expected["text"] in page["text"]["value"]
    characters = page["characters"]["value"]
    assert abs(characters[0]["box"][0] - expected["first_x"]) < 2

    slim = slim_page(page)
    assert len(slim["tables"]) == 1
    table = slim["tables"][0]
    cells = {cell["text"]: (cell["row"], cell["column"]) for cell in table["cells"]}
    drawn = {text: place for text, place in cells.items() if text in DRAWN}
    assert set(drawn) == DRAWN
    assert len({row for row, _ in drawn.values()}) == 2
    assert len({column for _, column in drawn.values()}) == 2
    assert len(set(drawn.values())) == 4
    assert all(cell["row_span"] == 1 and cell["column_span"] == 1 for cell in table["cells"])
    if engine == "tagged":
        assert table["method"] == "drawn-grid+runs-v1"
        assert set(cells) == DRAWN | {SENTENCE}
        assert slim["outside"] == []
    else:
        assert table["method"] == "drawn-grid-v1"
        assert set(cells) == DRAWN
        assert [item["text"] for item in slim["outside"]] == [SENTENCE]
    assert slim["clipped"] == []


@pytest.mark.parametrize(
    ("engine", "cells", "outside"),
    [
        (
            "tagged",
            {"t0r0c1": SENTENCE, "t0r1c1": "Owner", "t0r1c2": "Status", "t0r2c1": "Gas", "t0r2c2": "Open"},
            [],
        ),
        (
            "pdfium",
            {"t0r0c0": "Owner", "t0r0c1": "Status", "t0r1c0": "Gas", "t0r1c1": "Open"},
            ["o0"],
        ),
    ],
)
def test_the_slim_page_addresses_cells_and_outside_text_by_id(engine: str, cells: dict, outside: list):
    """`t<table>r<row>c<column>` and `o<index>`: the IDs the semantics tier answers with."""

    page = slim_page(read_document(_path("fixture-rotation-0"), [1], engine=engine, dpi=72)["pages"][0])

    by_id = {
        cell_id(0, cell["row"], cell["column"]): cell["text"] for cell in page["tables"][0]["cells"]
    }
    assert by_id == cells
    assert [outside_id(index) for index in range(len(page["outside"]))] == outside
    assert [item["text"] for item in page["outside"]] == [SENTENCE] * len(outside)
    assert page["size"] == [300, 200] and page["rotation"] == 0


def test_a_raster_only_page_renders_and_reads_no_text():
    page = read_document(_path("fixture-raster"), [1], dpi=72)["pages"][0]

    assert page["text"]["value"] == ""
    assert page["characters"]["value"] == []
    assert page["tables"]["value"] == []
    assert page["render"]["value"]["size"] == [300, 200]


def test_malformed_bytes_are_rejected_before_any_page_is_read():
    with pytest.raises(ValueError, match="open/password"):
        read_document(_path("fixture-malformed"))


def test_a_password_protected_document_is_rejected():
    with pytest.raises(ValueError, match="password"):
        read_document(_path("fixture-encrypted"), [1])


def test_every_page_is_read_when_none_is_named():
    result = read_document(_path("fixture-rotation-90-crop"), dpi=72)

    assert result["page_count"] == 1
    assert [page["number"] for page in result["pages"]] == [1]
