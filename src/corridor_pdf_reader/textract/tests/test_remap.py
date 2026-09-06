"""Lane A: the document's glyphs fill Textract's cells; Textract's words never do."""

from __future__ import annotations

from typing import Any

from corridor_pdf_reader.textract.blocks import page_from_blocks
from corridor_pdf_reader.textract.remap import TEXT_SOURCE, contains, remap_page
from corridor_pdf_reader.textract.tests.helpers import Page


def glyph(text: str, x: float, y: float, *, width: float = 6.0, height: float = 10.0, object_id: int = 0, break_before: bool = False) -> dict[str, Any]:
    box = [x, y, x + width, y + height]
    return {"text": text, "display_box": box, "ink_display_box": [x + 0.5, y + 1, x + width - 0.5, y + height], "object_id": object_id, "break_before": break_before, "angle": 0}


def word(text: str, x: float, y: float, object_id: int, spaced: bool = False) -> list[dict[str, Any]]:
    return [glyph(ch, x + k * 6.0, y, object_id=object_id, break_before=(k == 0 and spaced)) for k, ch in enumerate(text)]


def textract_page() -> dict[str, Any]:
    page = Page()
    header = page.word("ITEM", (80, 100, 110, 112))
    amount = page.word("AMOUNT", (210, 100, 260, 112))
    value = page.word("1.250,00", (210, 130, 280, 142))
    for w in (header, amount, value):
        page.line([w])
    c00 = page.cell(1, 1, (72, 96, 200, 120), [header])
    c01 = page.cell(1, 2, (200, 96, 320, 120), [amount])
    c10 = page.cell(2, 1, (72, 120, 200, 150))
    c11 = page.cell(2, 2, (200, 120, 320, 150), [value])
    page.table((72, 96, 320, 150), [c00, c01, c10, c11])
    page.line([page.word("Titel", (72, 40, 110, 52))])
    return page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=0)


def test_glyphs_inside_a_cell_polygon_become_its_text_in_reading_order() -> None:
    chars = word("Item", 80, 100, 1) + word("Amount", 210, 100, 2) + word("Conduit", 80, 130, 3) + word("$1,250.00", 210, 130, 4)
    out = remap_page(textract_page(), chars)
    cells = {(c["row"], c["column"]): c for c in out["tables"][0]["cells"]}
    assert cells[0, 0]["text"] == "Item" and cells[0, 1]["text"] == "Amount"
    assert cells[1, 0]["text"] == "Conduit", "a cell Textract read nothing from still takes the glyphs it holds"
    assert cells[1, 1]["text"] == "$1,250.00", "Textract's 1.250,00 is never stored"
    assert all("word_ids" not in cell for cell in cells.values())
    assert cells[1, 1]["glyphs"] == 9 and cells[1, 1]["block_ids"]
    assert out["text_source"] == TEXT_SOURCE
    assert out["tables"][0]["method"] == "textract-analyze-document-tables-v1"


def test_a_cell_with_no_glyph_is_empty_even_when_textract_read_a_word() -> None:
    out = remap_page(textract_page(), word("Item", 80, 100, 1))
    cells = {(c["row"], c["column"]): c for c in out["tables"][0]["cells"]}
    assert cells[0, 1]["text"] == "" and cells[1, 1]["text"] == ""


def test_glyphs_in_no_cell_are_outside_strings_grouped_by_object() -> None:
    chars = word("Title", 72, 40, 7) + word("Block", 110, 40, 7, spaced=True) + word("Item", 80, 100, 1) + word("Page", 280, 760, 9) + word("1", 310, 760, 9, spaced=True)
    out = remap_page(textract_page(), chars)
    assert [item["text"] for item in out["outside"]] == ["Title Block", "Page 1"]
    assert out["outside"][0]["box"][0] == 72.5 and out["outside"][0]["box"][2] == 139.5
    assert out["outside"][1]["box"][1] == 761.0


def test_two_lines_of_one_object_keep_their_line_break() -> None:
    chars = word("Traffic", 80, 128, 3) + word("Control", 80, 140, 3)
    out = remap_page(textract_page(), chars)
    cells = {(c["row"], c["column"]): c for c in out["tables"][0]["cells"]}
    assert cells[1, 0]["text"] == "Traffic\nControl"


def test_overlapping_cells_take_the_glyph_by_the_nearer_centre() -> None:
    page = Page()
    left = page.cell(1, 1, (72, 96, 202, 120))
    right = page.cell(1, 2, (198, 96, 320, 120))
    page.table((72, 96, 320, 120), [left, right])
    textract = page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=0)
    out = remap_page(textract, word("a", 195, 102, 1) + word("b", 200, 102, 2))
    cells = {(c["row"], c["column"]): c for c in out["tables"][0]["cells"]}
    assert cells[0, 0]["text"] == "a" and cells[0, 1]["text"] == "b"


def test_hidden_runs_are_carried_as_clipped_evidence() -> None:
    out = remap_page(textract_page(), word("Item", 80, 100, 1), clipped=word("hidden", 80, 160, 5))
    assert out["clipped"] == [{"text": "hidden", "box": [80.5, 161.0, 115.5, 170.0]}]


def test_contains_is_a_point_in_polygon_test() -> None:
    square = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    assert contains(square, 5, 5) and not contains(square, 15, 5) and not contains(square, 5, -1)
    skewed = [[0.0, 0.0], [10.0, 1.0], [10.0, 11.0], [0.0, 10.0]]
    assert contains(skewed, 9, 10.5) and not contains(skewed, 1, 10.5)


def test_a_margin_recovers_punctuation_the_tight_polygon_left_out() -> None:
    page = Page()
    cell = page.cell(1, 1, (72, 96, 200, 104.5))
    page.table((72, 96, 200, 104.5), [cell])
    textract = page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=0)
    # Digits sit inside the polygon; the period hangs a hair below its bottom edge.
    chars = word("285", 100, 97, 1) + [glyph(".", 118, 103, height=4, object_id=1)] + word("64", 124, 97, 1)
    strict = remap_page(textract, chars)
    grown = remap_page(textract, chars, margin=3.0)
    assert strict["tables"][0]["cells"][0]["text"] == "28564" and [o["text"] for o in strict["outside"]] == ["."]
    assert grown["tables"][0]["cells"][0]["text"] == "285.64" and grown["outside"] == [] and grown["remap_margin"] == 3.0


def test_rescued_glyphs_join_the_cell_that_holds_their_run() -> None:
    page = Page()
    cell = page.cell(1, 1, (72, 96, 200, 104.5))
    page.table((72, 96, 200, 104.5), [cell])
    textract = page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=0)
    chars = word("285", 100, 97, 1) + [glyph(".", 118, 103, height=4, object_id=1)] + word("64", 124, 97, 1) + word("Title", 72, 40, 7)
    out = remap_page(textract, chars, rescue_runs=True)
    assert out["tables"][0]["cells"][0]["text"] == "285.64" and out["remap_rescue_runs"] is True
    assert [o["text"] for o in out["outside"]] == ["Title"], "a run wholly outside stays outside"
