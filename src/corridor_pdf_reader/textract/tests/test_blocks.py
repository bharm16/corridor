"""Textract blocks into the reader's page shape: cells, merges, frames, outside text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from corridor_pdf_reader.textract.blocks import METHOD, page_from_blocks
from corridor_pdf_reader.textract.tests.helpers import Page

FIXTURES = Path(__file__).parent / "fixtures"


def grid_page() -> tuple[Page, dict[str, Any]]:
    """A title line, a 2x3 table whose top row merges its last two columns, a footer."""
    page = Page()
    title = [page.word("Schedule", (72, 40, 130, 52)), page.word("of", (134, 40, 146, 52)), page.word("Values", (150, 40, 195, 52))]
    page.line(title)
    item = page.word("Item", (80, 100, 110, 112))
    amount = page.word("Amount", (210, 100, 260, 112))
    one = page.word("Conduit", (80, 130, 130, 142))
    dollars = page.word("$1,250.00", (210, 130, 280, 142))
    note = page.word("see", (330, 130, 350, 142))
    note2 = page.word("note", (354, 130, 380, 142))
    for group in ([item], [amount], [one], [dollars], [note, note2]):
        page.line(group)
    c00 = page.cell(1, 1, (72, 96, 200, 120), [item], entity_types=["COLUMN_HEADER"])
    c01 = page.cell(1, 2, (200, 96, 320, 120), [amount])
    c02 = page.cell(1, 3, (320, 96, 440, 120))
    c10 = page.cell(2, 1, (72, 120, 200, 150), [one])
    c11 = page.cell(2, 2, (200, 120, 320, 150), [dollars])
    c12 = page.cell(2, 3, (320, 120, 440, 150), [note, note2])
    merged = page.merged(1, 2, (200, 96, 440, 120), [c01, c02], column_span=2)
    page.table((72, 96, 440, 150), [c00, c01, c02, c10, c11, c12], [merged])
    footer = [page.word("Page", (280, 760, 310, 770)), page.word("1", (314, 760, 320, 770))]
    page.line(footer)
    return page, page.response()


def test_cells_are_zero_based_with_textract_text_and_points_boxes() -> None:
    _, response = grid_page()
    out = page_from_blocks(response["Blocks"], number=3, size=(612.0, 792.0), rotation=0)
    assert out["number"] == 3 and out["size"] == [612.0, 792.0] and out["rotation"] == 0 and out["clipped"] == []
    assert len(out["tables"]) == 1
    table = out["tables"][0]
    assert table["method"] == METHOD
    assert table["box"] == [72.0, 96.0, 440.0, 150.0]
    cells = {(c["row"], c["column"]): c for c in table["cells"]}
    assert cells[0, 0]["text"] == "Item" and cells[0, 0]["box"] == [72.0, 96.0, 200.0, 120.0]
    assert cells[0, 0]["entity_types"] == ["COLUMN_HEADER"]
    assert cells[1, 1]["text"] == "$1,250.00"
    assert cells[1, 2]["text"] == "see note"
    assert cells[1, 2]["row_span"] == 1 and cells[1, 2]["column_span"] == 1
    assert [c["row"] for c in table["cells"]] == sorted(c["row"] for c in table["cells"])


def test_merged_cell_stands_in_for_its_parts() -> None:
    _, response = grid_page()
    out = page_from_blocks(response["Blocks"], number=1, size=(612.0, 792.0), rotation=0)
    cells = {(c["row"], c["column"]): c for c in out["tables"][0]["cells"]}
    assert (0, 2) not in cells, "a covered cell is not listed twice"
    merged = cells[0, 1]
    assert merged["column_span"] == 2 and merged["row_span"] == 1
    assert merged["text"] == "Amount"
    assert merged["box"] == [200.0, 96.0, 440.0, 120.0]
    assert len(merged["block_ids"]) == 3, "the merged block and both covered cells"
    assert len(cells) == 5


def test_confidence_is_the_mean_of_the_words() -> None:
    page = Page()
    a = page.word("12", (100, 100, 120, 112), confidence=90.0)
    b = page.word("units", (124, 100, 160, 112), confidence=70.0)
    page.line([a, b])
    cell = page.cell(1, 1, (90, 96, 200, 120), [a, b])
    empty = page.cell(1, 2, (200, 96, 300, 120))
    page.table((90, 96, 300, 120), [cell, empty])
    out = page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=0)
    cells = out["tables"][0]["cells"]
    assert cells[0]["confidence"] == 80.0 and cells[0]["word_ids"] == [a["Id"], b["Id"]] and cells[0]["block_ids"] == [cell["Id"]]
    assert cells[1]["confidence"] is None and cells[1]["text"] == "" and cells[1]["word_ids"] == []


def test_words_from_different_lines_break_the_cell_text() -> None:
    page = Page()
    first = page.word("Traffic", (100, 100, 150, 112))
    second = page.word("Control", (100, 114, 150, 126))
    page.line([first])
    page.line([second])
    cell = page.cell(1, 1, (90, 96, 200, 130), [first, second])
    page.table((90, 96, 200, 130), [cell])
    out = page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=0)
    assert out["tables"][0]["cells"][0]["text"] == "Traffic\nControl"


def test_outside_strings_are_the_lines_no_cell_owns() -> None:
    _, response = grid_page()
    out = page_from_blocks(response["Blocks"], number=1, size=(612.0, 792.0), rotation=0)
    assert [item["text"] for item in out["outside"]] == ["Schedule of Values", "Page 1"]
    assert out["outside"][0]["box"] == [72.0, 40.0, 195.0, 52.0]
    assert out["outside"][1]["box"][1] == 760.0


def test_a_partly_owned_line_leaves_only_its_free_words_outside() -> None:
    page = Page()
    label = page.word("Total:", (100, 100, 140, 112))
    value = page.word("99.00", (300, 100, 340, 112))
    page.line([label, value])
    cell = page.cell(1, 1, (290, 96, 350, 120), [value])
    page.table((290, 96, 350, 120), [cell])
    out = page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=0)
    assert out["outside"] == [{"text": "Total:", "box": [100.0, 100.0, 140.0, 112.0], "confidence": 99.0, "block_id": out["outside"][0]["block_id"]}]


def test_rotated_page_scales_ratios_with_the_displayed_frame() -> None:
    # A landscape print stored as a portrait page rotated 90: the image
    # Textract saw is 792 wide and 612 tall, and so is the reader's frame.
    page = Page(frame=(792.0, 612.0))
    word = page.word("X", (700, 500, 710, 510))
    page.line([word])
    cell = page.cell(1, 1, (690, 490, 720, 520), [word])
    page.table((690, 490, 720, 520), [cell])
    out = page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=90)
    assert out["size"] == [612.0, 792.0] and out["rotation"] == 90
    assert out["tables"][0]["cells"][0]["box"] == [690.0, 490.0, 720.0, 520.0]
    assert out["tables"][0]["cells"][0]["polygon"][0] == [690.0, 490.0]


def test_a_response_without_tables_is_an_empty_page_with_outside_text() -> None:
    page = Page()
    page.line([page.word("Nothing", (100, 100, 150, 112))])
    out = page_from_blocks(page.blocks, number=1, size=(612.0, 792.0), rotation=0)
    assert out["tables"] == [] and [item["text"] for item in out["outside"]] == ["Nothing"]
    assert page_from_blocks([], number=1, size=(612.0, 792.0), rotation=0)["outside"] == []


def test_recorded_fixtures_map_to_well_formed_pages() -> None:
    """Every recorded response maps: cells inside the page, words owned once, spans positive."""
    for path in sorted(FIXTURES.glob("*.json")):
        entry = json.loads(path.read_text())
        response = entry["response"]
        width, height = entry["displayed_size"]
        out = page_from_blocks(response["Blocks"], number=1, size=(width, height), rotation=0)
        assert out["tables"], path.name
        seen: set[str] = set()
        for table in out["tables"]:
            assert table["method"] == METHOD
            for cell in table["cells"]:
                assert cell["row"] >= 0 and cell["column"] >= 0 and cell["row_span"] >= 1 and cell["column_span"] >= 1
                assert -1 <= cell["box"][0] <= cell["box"][2] <= width + 1 and -1 <= cell["box"][1] <= cell["box"][3] <= height + 1
                for word in cell["word_ids"]:
                    assert word not in seen, "a word belongs to one cell"
                    seen.add(word)
        assert any(cell["text"] for table in out["tables"] for cell in table["cells"]), path.name
