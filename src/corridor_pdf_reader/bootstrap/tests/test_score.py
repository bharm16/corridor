"""The scorer must pass exact structure and name every way structure can fail."""

from __future__ import annotations

from typing import Any

from corridor_pdf_reader.bootstrap.score import score_pair


def sheet(cells: dict[tuple[int, int], str], **extra: Any) -> dict[str, Any]:
    return {
        "name": "S",
        "cells": [[r, c, text, "text"] for (r, c), text in sorted(cells.items())],
        "merged": extra.get("merged", []),
        "print_area": None,
        "outside_text": extra.get("outside_text", []),
        "uncached": extra.get("uncached", []),
        "print_titles": extra.get("print_titles"),
        "header_footer": extra.get("header_footer", []),
    }


def table(rows: list[list[str | None]], spans: dict[tuple[int, int], tuple[int, int]] | None = None) -> dict[str, Any]:
    cells = []
    for i, row in enumerate(rows):
        j = 0
        for text in row:
            if text is None:
                j += 1
                continue
            row_span, column_span = (spans or {}).get((i, j), (1, 1))
            cells.append(
                {
                    "row": i,
                    "column": j,
                    "row_span": row_span,
                    "column_span": column_span,
                    "text": text,
                    "box": [50.0 + j * 50.0, 100.0 + i * 20.0, 50.0 + (j + column_span) * 50.0, 100.0 + (i + row_span) * 20.0],
                }
            )
            j += column_span
    return {"method": "test", "box": [0, 0, 300, 20.0 * len(rows)], "cells": cells}


def page(number: int, tables: list[dict[str, Any]], outside: list[tuple[str, float]] | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "size": [612.0, 792.0],
        "rotation": 0,
        "tables": tables,
        "outside": [{"text": text, "box": [100.0, y, 200.0, y + 10.0]} for text, y in (outside or [])],
    }


def run(sheets: list[dict[str, Any]], pages: list[dict[str, Any]]) -> dict[str, Any]:
    return score_pair({"key": "t", "sheets": sheets}, {"pages": pages})


GRID = {
    (1, 1): "Item",
    (1, 2): "Qty",
    (1, 3): "Amount",
    (2, 1): "Pipe",
    (2, 2): "3",
    (2, 3): "$ 1,200.00",
    (3, 1): "Valve",
    (3, 2): "1",
    (3, 3): "$ -",
    (4, 1): "Total",
    (4, 3): "$ 1,200.00",
}
GRID_ROWS: list[list[str | None]] = [
    ["Item", "Qty", "Amount"],
    ["Pipe", "3", "$ 1,200.00"],
    ["Valve", "1", "$ -"],
    ["Total", "", "$1,200.00"],
]


def classes(result: dict[str, Any]) -> dict[str, int]:
    return result["counts"]


def test_exact_grid_passes_and_covers_every_cell() -> None:
    result = run([sheet(GRID)], [page(1, [table(GRID_ROWS)])])
    assert result["pass"], result
    assert result["exact_cells"] == result["reference_cells"] == len(GRID)
    assert not result["uncovered"]


def test_swapped_values_are_mismatches_not_silent() -> None:
    rows = [row[:] for row in GRID_ROWS]
    rows[1] = ["3", "Pipe", "$ 1,200.00"]
    result = run([sheet(GRID)], [page(1, [table(rows)])])
    assert not result["pass"]
    assert classes(result) == {"value_mismatch": 2}
    assert result["uncovered_counts"] == {"value_mismatch": 2}


def test_dropped_value_is_missing_and_uncovered() -> None:
    rows = [row[:] for row in GRID_ROWS]
    rows[2] = ["Valve", "", "$ -"]
    result = run([sheet(GRID)], [page(1, [table(rows)])])
    assert classes(result) == {"missing_value": 1}
    assert [(u["r"], u["c"], u["sub"]) for u in result["uncovered"]] == [(3, 2, "absent")]


def test_merged_header_needs_matching_span() -> None:
    cells = dict(GRID)
    cells[(0, 1)] = "Schedule"
    merged = [[0, 1, 0, 3]]
    rows: list[list[str | None]] = [["Schedule"], *GRID_ROWS]
    good = run([sheet(cells, merged=merged)], [page(1, [table(rows, {(0, 0): (1, 3)})])])
    assert good["pass"], good["pages"][0]["errors"]
    narrow_rows: list[list[str | None]] = [["Schedule", "", ""], *GRID_ROWS]
    narrow = run([sheet(cells, merged=merged)], [page(1, [table(narrow_rows)])])
    assert narrow["pass"], narrow["pages"][0]["errors"]
    assert narrow["pages"][0]["tables"][0]["merges_unreported"] == 1
    # A span past the range into a mapped plain column is wrong; a span into
    # a column no reader cell maps cannot be judged and is not.
    cells[(0, 4)] = "Note"
    cells[(2, 4)] = "rush"
    wide_rows: list[list[str | None]] = [["Schedule", "Note"], *[[*row, "rush" if row[0] == "Pipe" else ""] for row in GRID_ROWS]]
    wide = run([sheet(cells, merged=merged)], [page(1, [table(wide_rows, {(0, 0): (1, 4)})])])
    assert classes(wide) == {"merged_cells": 1}, wide["pages"][0]["errors"]


def test_continuation_pages_with_repeated_titles_pass() -> None:
    cells = dict(GRID)
    cells[(5, 1)] = "Fitting"
    cells[(5, 2)] = "2"
    cells[(5, 3)] = "$ 40.00"
    first = table(GRID_ROWS[:3])
    second = table([GRID_ROWS[0], GRID_ROWS[3], ["Fitting", "2", "$ 40.00"]])
    result = run([sheet(cells, print_titles=[1, 1])], [page(1, [first]), page(2, [second])])
    assert result["pass"], [p["errors"] for p in result["pages"]]
    untitled = run([sheet(cells)], [page(1, [first]), page(2, [second])])
    assert untitled["counts"] == {"missing_row": 6}


def test_wide_sheet_split_across_pages_by_columns_passes() -> None:
    cells = dict(GRID)
    cells[(1, 4)] = "Note"
    cells[(2, 4)] = "rush"
    cells[(3, 4)] = "spare"
    left = table([row[:2] for row in GRID_ROWS])
    right = table([["Amount", "Note"], ["$ 1,200.00", "rush"], ["$ -", "spare"], ["$1,200.00", ""]])
    result = run([sheet(cells)], [page(1, [left]), page(2, [right])])
    assert result["pass"], [p["errors"] for p in result["pages"]]


def test_cell_text_left_outside_tables_is_named() -> None:
    cells = dict(GRID)
    cells[(0, 1)] = "Project: Colman Dock"
    result = run([sheet(cells)], [page(1, [table(GRID_ROWS)], outside=[("Project: Colman Dock", 30.0)])])
    assert classes(result) == {"outside_table": 1}
    assert result["uncovered_counts"] == {"outside_table": 1}


def test_header_footer_template_explains_page_numbers() -> None:
    result = run(
        [sheet(GRID, header_footer=["&CPage &P of &N", "&R&D"])],
        [page(1, [table(GRID_ROWS)], outside=[("Page 1 of 2", 770.0), ("9/5/2026", 5.0)])],
    )
    assert result["pass"], result["pages"][0]["errors"]
    stray = run([sheet(GRID, header_footer=["&CPage &P of &N"])], [page(1, [table(GRID_ROWS)], outside=[("Page 1 of 2", 400.0)])])
    assert classes(stray) == {"unexplained_text": 1}


def test_row_not_in_workbook_inside_the_table_is_unaligned() -> None:
    rows = GRID_ROWS[:2] + [["Lien waiver attached", "", ""]] + GRID_ROWS[2:]
    result = run([sheet(GRID)], [page(1, [table(rows)])])
    assert classes(result) == {"unaligned_row": 1}
    assert result["pages"][0]["errors"][0]["sub"] == "not_in_workbook"


def test_row_not_in_workbook_after_the_table_is_prose() -> None:
    rows = GRID_ROWS + [["Lien waiver attached", "", ""]]
    result = run([sheet(GRID)], [page(1, [table(rows)])])
    assert result["pass"], result["pages"][0]["errors"]
    assert classes(result) == {"prose_row": 1}


def test_wide_reader_cell_over_two_sheet_cells_is_one_merged_error() -> None:
    rows: list[list[str | None]] = [row[:] for row in GRID_ROWS]
    rows[1] = ["Pipe 3", "$ 1,200.00"]
    result = run([sheet(GRID)], [page(1, [table(rows, {(1, 0): (1, 2)})])])
    assert classes(result) == {"merged_cells": 1}
    assert result["pages"][0]["errors"][0]["sub"] == "text_intact"


def test_vertical_merge_span_is_checked() -> None:
    cells = {(1, 1): "Group", (1, 2): "a", (2, 2): "b", (3, 1): "End", (3, 2): "c"}
    merged = [[1, 1, 2, 1]]
    good = run([sheet(cells, merged=merged)], [page(1, [table([["Group", "a"], [None, "b"], ["End", "c"]], {(0, 0): (2, 1)})])])
    assert good["pass"], good["pages"][0]["errors"]
    split = run([sheet(cells, merged=merged)], [page(1, [table([["Group", "a"], ["", "b"], ["End", "c"]])])])
    assert split["pass"], split["pages"][0]["errors"]
    assert split["pages"][0]["tables"][0]["merges_unreported"] == 1
    past = run([sheet(cells, merged=merged)], [page(1, [table([["Group", "a"], [None, "b"], [None, "c"]], {(0, 0): (3, 1)})])])
    assert classes(past) == {"span_mismatch": 1, "missing_cell": 1}


def test_clipped_wrapped_text_is_named_and_not_a_failure() -> None:
    cells = dict(GRID)
    cells[(2, 1)] = "Provide premium overtime labor to install electrical work at trestle level"
    rows: list[list[str | None]] = [row[:] for row in GRID_ROWS]
    rows[1] = ["electrical work at trestle level", "3", "$ 1,200.00"]
    result = run([sheet(cells)], [page(1, [table(rows)])])
    assert result["pass"], result["pages"][0]["errors"]
    assert result["pages"][0]["errors"][0]["sub"] == "clipped_suffix"
    assert result["clipped_cells"] == 1 and not result["uncovered"]
    rows[1] = ["Provide premium overtime labor to install electrical work at trestle lev", "3", "$ 1,200.00"]
    cut = page(1, [table(rows)])
    cut["tables"][0]["cells"][3]["box"][2] -= 20.0  # room for the missing letters before the next cell
    lossy = run([sheet(cells)], [cut])
    assert not lossy["pass"]
    assert lossy["pages"][0]["errors"][0]["sub"] == "reader_has_less"


def test_uncached_formula_result_is_unverifiable_not_wrong() -> None:
    rows: list[list[str | None]] = [row[:] for row in GRID_ROWS]
    rows[3] = ["Total", "4", "$1,200.00"]
    result = run([sheet(GRID, uncached=[[4, 2]])], [page(1, [table(rows)])])
    assert result["pass"], result["pages"][0]["errors"]
    assert result["pages"][0]["errors"][0]["class"] == "unverifiable"
    wrong = run([sheet(GRID)], [page(1, [table(rows)])])
    assert wrong["counts"] == {"extra_value": 1}


def test_identical_trailing_rows_take_the_nearest_sheet_rows() -> None:
    cells = dict(GRID)
    for r in (5, 6, 7, 8):
        cells[(r, 3)] = "$ -"
    first = table(GRID_ROWS + [["", "", "$ -"], ["", "", "$ -"]])
    second = table([GRID_ROWS[0], ["", "", "$ -"], ["", "", "$ -"]])
    result = run([sheet(cells, print_titles=[1, 1])], [page(1, [first]), page(2, [second])])
    assert result["pass"], [p["errors"] for p in result["pages"]]


def test_reader_columns_may_refine_one_sheet_column_without_collision() -> None:
    cells = {(1, 2): "Invoice No.", (1, 3): "58", (2, 2): "thru", (2, 3): "02/12/22"}
    rows: list[list[str | None]] = [["", "Invoice No.", None, "58"], ["", None, "thru", "02/12/22"]]
    result = run([sheet(cells)], [page(1, [table(rows)])])
    assert result["pass"], result["pages"][0]["errors"]
    assert result["pages"][0]["tables"][0]["columns_refined"] == 1


def test_page_number_cell_in_the_margin_is_pagination() -> None:
    footer = table([["Page 1 of 2"]])
    footer["cells"][0]["box"] = [280.0, 770.0, 340.0, 780.0]
    result = run([sheet(GRID, header_footer=["&CPage &P of &N"])], [page(1, [table(GRID_ROWS), footer])])
    assert result["pass"], result["pages"][0]["errors"]


def test_span_over_refined_columns_is_one_sheet_column() -> None:
    cells = {(1, 2): "Invoice No.", (1, 3): "58", (2, 2): "thru", (2, 3): "02/12/22", (3, 2): "0.22"}
    rows: list[list[str | None]] = [["", "Invoice No.", None, "58"], ["", None, "thru", "02/12/22"], ["", "0.22", None, None]]
    result = run([sheet(cells)], [page(1, [table(rows, {(2, 1): (1, 2)})])])
    assert result["pass"], result["pages"][0]["errors"]


def test_reader_reported_clipped_text_explains_a_truncated_cell() -> None:
    cells = dict(GRID)
    cells[(2, 1)] = "WSF CO 128"
    rows: list[list[str | None]] = [row[:] for row in GRID_ROWS]
    rows[1] = ["WSF CO 12", "3", "$ 1,200.00"]
    pg = page(1, [table(rows)])
    pg["clipped"] = [{"text": "8", "box": [98.0, 122.0, 102.0, 130.0]}]
    result = run([sheet(cells)], [pg])
    assert result["pass"], result["pages"][0]["errors"]
    assert result["pages"][0]["errors"][0]["sub"] == "clipped_evidence"
    apart = page(1, [table(rows)])
    apart["tables"][0]["cells"][3]["box"][2] -= 20.0
    bare = run([sheet(cells)], [apart])
    assert not bare["pass"]
    tight = run([sheet(cells)], [page(1, [table(rows)])])
    assert tight["pass"], tight["pages"][0]["errors"]
    assert tight["pages"][0]["errors"][0]["sub"] == "clipped_edge"


def test_hashes_for_a_too_wide_number_are_not_a_reader_fault() -> None:
    rows: list[list[str | None]] = [row[:] for row in GRID_ROWS]
    rows[1] = ["Pipe", "3", "########"]
    result = run([sheet(GRID)], [page(1, [table(rows)])])
    assert result["pass"], result["pages"][0]["errors"]
    assert result["pages"][0]["errors"][0]["sub"] == "overflow_hashes"


def test_value_on_any_column_of_a_merged_range_is_that_cell() -> None:
    cells = {(1, 1): "VENDOR#", (1, 2): "71280", (2, 1): "Item", (2, 2): "a", (2, 3): "b"}
    merged = [[1, 2, 1, 3]]
    result = run([sheet(cells, merged=merged)], [page(1, [table([["VENDOR#", "", "71280"], ["Item", "a", "b"]])])])
    assert result["pass"], result["pages"][0]["errors"]


def test_overflow_from_outside_the_print_area_is_an_artifact() -> None:
    rows: list[list[str | None]] = [["ORE FINAL PRINTING enter any digit"], *GRID_ROWS]
    result = run([sheet(GRID, outside_text=["BEFORE FINAL PRINTING enter any digit to remove"])], [page(1, [table(rows)])])
    assert result["pass"], result["pages"][0]["errors"]
    assert result["pages"][0]["errors"][0]["sub"] == "print_area_overflow"
