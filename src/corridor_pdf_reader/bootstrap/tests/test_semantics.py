"""The semantics tier, exercised with recorded answers: no model is called."""

from __future__ import annotations

from typing import Any

import pytest

from corridor_pdf_reader.replacement.semantics import (
    STRUCTURE_SCHEMA,
    SequencingSemanticsDetected,
    listing,
    read_document,
    read_page,
)

Rows = list[list[str | None]]
Columns = list[dict[str, Any]]


def cell(row: int, column: int, text: str, column_span: int = 1) -> dict[str, Any]:
    return {"row": row, "column": column, "row_span": 1, "column_span": column_span, "text": text, "box": [50.0 + column * 60.0, 100.0 + row * 20.0, 100.0 + column * 60.0, 118.0 + row * 20.0]}


def page(rows: Rows, outside: list[str] | None = None, number: int = 1) -> dict[str, Any]:
    cells = [cell(i, j, text) for i, row in enumerate(rows) for j, text in enumerate(row) if text is not None]
    return {
        "number": number,
        "size": [612.0, 792.0],
        "rotation": 0,
        "tables": [{"method": "test", "box": [40.0, 90.0, 500.0, 400.0], "cells": cells}],
        "outside": [{"text": text, "box": [30.0, 20.0 + k * 12.0, 200.0, 30.0 + k * 12.0]} for k, text in enumerate(outside or [])],
        "clipped": [],
    }


MATRIX: Rows = [
    ["Utility Listing", None, None, None, None],
    ["Project Owner:", "WSDOT", None, None, None],
    [None, None, "RECOMMENDED RESOLUTION", None, None],
    ["Owner", "Conflict ID", "Relocation Needed", "Retain and Protect", "Notes"],
    ["HWD", "1", "X", "", "Water main 16 in"],
    ["HWD", "2", "", "X", ""],
    ["", "3", "", "", "Not used"],
    ["", "", "", "", ""],
]


def structure(columns: Columns, header_row: int | None = 3, table: int | None = 0, owner: str | None = None, matrix: bool = True) -> dict[str, Any]:
    return {"is_utility_matrix": matrix, "matrix_table": table, "header_row": header_row, "columns": columns, "page_attributes": {"external_org": owner}, "mapping_confidence": 0.9}


MARKED: Columns = [
    {"index": 0, "canonical_field": "external_org"},
    {"index": 1, "canonical_field": "utility_id"},
    {"index": 2, "canonical_field": "resolution_strategy"},
    {"index": 3, "canonical_field": "resolution_strategy"},
    {"index": 4, "canonical_field": "notes"},
]


class StubClient:
    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system: str, user: str, schema: dict[str, Any], images: Any = ()) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "schema": schema, "images": list(images)})
        return self.answers.pop(0)


def test_listing_shows_ids_rows_and_outside_text() -> None:
    text = listing(page(MATRIX, outside=["Page 1 of 3"]), "listing.pdf")
    assert "Table 0 — 5 columns, 8 rows" in text
    assert "row 3: [0] Owner | [1] Conflict ID | [2] Relocation Needed | [3] Retain and Protect | [4] Notes" in text
    assert "o0: Page 1 of 3" in text
    assert "t<table>r<row>c<column>" in text


def test_rows_are_assembled_from_cells_with_provenance() -> None:
    reading, carried = read_page(page(MATRIX), structure(MARKED))
    assert reading.is_utility_matrix and reading.matrix_table == 0 and reading.header_row == 3
    assert reading.mapping is not None and reading.mapping.marks == {2: "Relocation Needed", 3: "Retain and Protect"}
    assert not reading.refused
    first = reading.rows[0]
    assert first.disposition == "extracted"
    assert first.fields["utility_id"].text == "1" and first.fields["utility_id"].cells == ("t0r4c1",)
    assert first.fields["external_org"].text == "HWD"
    assert first.fields["notes"].text == "Water main 16 in"
    assert first.fields["resolution_strategy"].text == "Relocation Needed"
    assert first.fields["resolution_strategy"].cells == ("t0r4c2",)
    second = reading.rows[1]
    assert second.fields["resolution_strategy"].text == "Retain and Protect"
    assert carried is not None and carried[1] == 5


def test_a_retired_row_and_a_blank_row_are_not_conflicts() -> None:
    reading, _ = read_page(page(MATRIX), structure(MARKED))
    assert [row.disposition for row in reading.rows] == ["extracted", "extracted", "skipped", "blank"]
    assert reading.rows[2].reason == "retired_row"
    assert reading.rows[3].reason == "blank_source_row"


def test_page_attribute_is_read_from_the_named_cell() -> None:
    rows: Rows = [["UTILITY AGENCY OWNER:", "Comcast", None], ["Conflict #", "Station", "Notes"], ["7", "10+00", "pole"]]
    columns: Columns = [{"index": 0, "canonical_field": "utility_id"}, {"index": 1, "canonical_field": "station_from"}, {"index": 2, "canonical_field": "notes"}]
    reading, _ = read_page(page(rows), structure(columns, header_row=1, owner="t0r0c1"))
    assert reading.page_attributes["external_org"].text == "Comcast"
    assert reading.page_attributes["external_org"].cells == ("t0r0c1",)
    assert reading.rows[0].disposition == "extracted"
    assert reading.rows[0].fields["external_org"].text == "Comcast"


def test_page_attribute_from_an_outside_string() -> None:
    rows: Rows = [["Conflict #", "Station", "Notes"], ["7", "10+00", "pole"]]
    columns: Columns = [{"index": 0, "canonical_field": "utility_id"}, {"index": 1, "canonical_field": "station_from"}, {"index": 2, "canonical_field": "notes"}]
    reading, _ = read_page(page(rows, outside=["UAO: Comcast"]), structure(columns, header_row=0, owner="o0"))
    assert reading.page_attributes["external_org"].text == "UAO: Comcast"


def test_page_attribute_pointing_into_the_rows_is_refused() -> None:
    columns: Columns = [{"index": 0, "canonical_field": "external_org"}, {"index": 1, "canonical_field": "utility_id"}]
    reading, _ = read_page(page(MATRIX), structure(columns, owner="t0r4c0"))
    assert reading.page_attributes == {}
    assert any("a cell of the conflict rows" in note for note in reading.refused)


def test_unknown_ids_and_indexes_are_refused_not_believed() -> None:
    columns: Columns = [{"index": 0, "canonical_field": "external_org"}, {"index": 1, "canonical_field": "utility_id"}, {"index": 9, "canonical_field": "notes"}]
    reading, _ = read_page(page(MATRIX), structure(columns, owner="t0r99c0"))
    assert reading.page_attributes == {}
    assert reading.mapping is not None and 9 not in reading.mapping.fields
    assert sum("does not hold" in note for note in reading.refused) == 1
    assert sum("not in the table" in note for note in reading.refused) == 1


def test_a_field_outside_the_vocabulary_or_claimed_twice_is_unmapped() -> None:
    columns: Columns = [
        {"index": 0, "canonical_field": "external_org"},
        {"index": 1, "canonical_field": "utility_id"},
        {"index": 2, "canonical_field": "utility_id"},
        {"index": 4, "canonical_field": "remarks"},
    ]
    reading, _ = read_page(page(MATRIX), structure(columns))
    assert reading.mapping is not None
    assert reading.mapping.fields == {0: "external_org", 1: "utility_id"}
    assert reading.mapping.unmapped == ["Relocation Needed", "Notes"]
    assert len(reading.refused) == 2


def test_a_bare_mark_under_one_strategy_column_is_dropped() -> None:
    columns: Columns = [{"index": 0, "canonical_field": "external_org"}, {"index": 1, "canonical_field": "utility_id"}, {"index": 2, "canonical_field": "resolution_strategy"}]
    reading, _ = read_page(page(MATRIX), structure(columns))
    assert reading.mapping is not None and reading.mapping.marks == {}
    assert "resolution_strategy" not in reading.rows[0].fields


def test_rows_needing_fields_are_skipped_with_a_reason() -> None:
    columns: Columns = [{"index": 1, "canonical_field": "utility_id"}, {"index": 4, "canonical_field": "notes"}]
    reading, _ = read_page(page(MATRIX), structure(columns))
    assert reading.rows[0].disposition == "skipped" and reading.rows[0].reason == "missing_required_fields"
    assert reading.rows[1].reason == "insufficient_mapped_fields"


def test_a_continuation_page_keeps_the_carried_mapping() -> None:
    first, carried = read_page(page(MATRIX), structure(MARKED))
    continued: Rows = [["HWD", "4", "", "X", "later page"]]
    second, _ = read_page(page(continued, number=2), structure([], header_row=None), carried)
    assert second.mapping is first.mapping
    assert second.rows[0].disposition == "extracted"
    assert second.rows[0].fields["utility_id"].text == "4"
    assert second.rows[0].row_id == "structure:2:table:0:row:1"
    alone, _ = read_page(page(continued, number=3), structure([], header_row=None), None)
    assert alone.rows == [] and any("no mapping carried" in note for note in alone.refused)


def test_no_matrix_reads_nothing() -> None:
    reading, _ = read_page(page(MATRIX), structure([], table=None, matrix=False))
    assert not reading.is_utility_matrix and reading.rows == [] and reading.mapping is None


def test_a_sequencing_column_refuses_the_document() -> None:
    rows: Rows = [["Conflict ID", "Dependent Activity"], ["1", "2"]]
    columns: Columns = [{"index": 0, "canonical_field": "utility_id"}, {"index": 1, "canonical_field": None}]
    with pytest.raises(SequencingSemanticsDetected):
        read_page(page(rows), structure(columns, header_row=0))


def test_read_document_sends_the_listing_and_the_strict_schema() -> None:
    client = StubClient([structure(MARKED), structure([], header_row=None)])
    later: Rows = [["HWD", "4", "", "X", ""]]
    results = read_document(client, [page(MATRIX), page(later, number=2)], "doc.pdf")
    assert [len(reading.rows) for _, reading in results] == [4, 1]
    assert client.calls[0]["schema"] is STRUCTURE_SCHEMA
    assert "Page 1 of doc.pdf" in client.calls[0]["user"]
    assert "canonical field" in client.calls[0]["system"]
    assert STRUCTURE_SCHEMA["additionalProperties"] is False
    assert set(STRUCTURE_SCHEMA["required"]) == set(STRUCTURE_SCHEMA["properties"])


def test_a_heading_merged_over_marked_columns_heads_each_of_them() -> None:
    rows: Rows = [["Owner", "Conflict ID", "Relocation", None, "Protect"], ["HWD", "1", "", "X", ""]]
    heading = page(rows)
    for item in heading["tables"][0]["cells"]:
        if item["row"] == 0 and item["column"] == 2:
            item["column_span"] = 2
    columns: Columns = [
        {"index": 0, "canonical_field": "external_org"},
        {"index": 1, "canonical_field": "utility_id"},
        {"index": 2, "canonical_field": "resolution_strategy"},
        {"index": 3, "canonical_field": "resolution_strategy"},
        {"index": 4, "canonical_field": "resolution_strategy"},
    ]
    reading, _ = read_page(heading, structure(columns, header_row=0))
    assert reading.mapping is not None and reading.mapping.marks == {2: "Relocation", 3: "Relocation", 4: "Protect"}
    assert reading.rows[0].fields["resolution_strategy"].text == "Relocation"
    assert reading.rows[0].fields["resolution_strategy"].cells == ("t0r1c3",)
