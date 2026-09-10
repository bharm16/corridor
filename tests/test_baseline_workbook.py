"""The Corridor operations reading of an offered workbook, against bytes alone.

``baseline_workbook`` states that nothing may be dropped: a retired row, a row
missing a required field, an unknown column, and a populated cell no released
transformation can type are each carried to the adoption act with a reason.
Those four cases were only ever reached through the database-bound adoption
tests, so nothing pinned them directly.  These tests build small workbooks and
read them; no engine, no store, no model.
"""

from pathlib import Path

import pytest
from openpyxl import Workbook

from corridor.baseline_workbook import (
    IMPORTER_IDENTITY,
    IMPORTER_VERSION,
    PARSER,
    SOURCE_ROW_KEY_RULE,
    BaselineWorkbookUnsupported,
    read_baseline_workbook,
)


HEADINGS = [
    "Utility Conflict ID",
    "Utility Owner",
    "Utility Type",
    "Size",
    "Material",
    "Start Station",
    "End Station",
    "Resolution Strategy Selected (from Resolution Alternatives)",
    "Promised For",
    "Action Due Date",
]

CONFLICT = [
    "UC-1", "CenterPoint Energy", "Electric", "12 in", "Steel",
    "1149+00", "1150+00", "Relocate", "2026-03-01", "2026-02-01",
]


def _workbook(tmp_path: Path, rows, *, headings=None, name="ucm.xlsx") -> Path:
    path = tmp_path / name
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) — Utility Conflicts"])
    sheet.append(list(HEADINGS if headings is None else headings))
    for row in rows:
        sheet.append(list(row))
    book.save(path)
    return path


def _row(reading, row_number):
    return next(row for row in reading.rows if row.row_number == row_number)


def test_an_ordinary_workbook_reads_cleanly_and_names_its_own_reading(tmp_path):
    reading = read_baseline_workbook(_workbook(tmp_path, [CONFLICT]))

    assert (reading.parser, reading.importer_identity, reading.importer_version) == (
        PARSER, IMPORTER_IDENTITY, IMPORTER_VERSION
    )
    assert reading.adopted_sheet == "Utility Conflicts"
    assert reading.header_row_number == 2
    assert reading.source_row_key_rule == SOURCE_ROW_KEY_RULE
    assert reading.resolved
    assert reading.round_trip.clean and reading.round_trip.rows_checked == 1
    [row] = reading.rows
    assert row.source_row_key == "Utility Conflicts!3"
    assert row.exclusion_reason is None and not row.excluded
    assert row.business_identity == "UC-1"
    assert row.value("external_org") == "CenterPoint Energy"
    assert row.value("committed_date") == "2026-03-01"


def test_a_retired_row_is_carried_with_its_reason_and_never_dropped(tmp_path):
    """The form's own bookkeeping: a printed number carrying `Not Used`."""

    retired = ["UC-4", "Not Used", "", "", "", "", "", "", "", ""]
    reading = read_baseline_workbook(_workbook(tmp_path, [CONFLICT, retired]))

    assert [row.row_number for row in reading.rows] == [3, 4]
    assert _row(reading, 4).exclusion_reason == "retired_row"
    assert _row(reading, 4).excluded
    assert _row(reading, 4).value("utility_id") == "UC-4"


def test_a_row_missing_a_required_field_is_carried_with_its_reason(tmp_path):
    """No owner: the record could not be built on it, and it is still reported."""

    unowned = ["UC-5", "", "Gas", "6 in", "Steel", "1200+00", "1201+00", "", "", ""]
    reading = read_baseline_workbook(_workbook(tmp_path, [CONFLICT, unowned]))

    assert _row(reading, 4).exclusion_reason == "missing_required_fields"
    assert _row(reading, 4).value("utility_type") == "Gas"


def test_a_row_mapping_too_few_fields_is_carried_with_its_own_reason(tmp_path):
    """A full-width band, not a conflict: one mapped cell of ten."""

    band = ["", "FROM C/L CONST GULF OF MEXICO DR.", "", "", "", "", "", "", "", ""]
    reading = read_baseline_workbook(_workbook(tmp_path, [CONFLICT, band]))

    assert _row(reading, 4).exclusion_reason == "insufficient_mapped_fields"


def test_an_unknown_column_is_retained_by_heading_and_counted(tmp_path):
    headings = [*HEADINGS, "Right of Way Agent"]
    reading = read_baseline_workbook(
        _workbook(
            tmp_path,
            [[*CONFLICT, "R. Alvarez"], [*CONFLICT[:1], *CONFLICT[1:], ""]],
            headings=headings,
        )
    )

    [unknown] = [
        column for column in reading.unknown_columns
        if column.heading == "Right of Way Agent"
    ]
    assert (unknown.column, unknown.populated_cells) == ("K", 1)
    [retained] = _row(reading, 3).retained
    assert (retained.heading, retained.cell_range, retained.exact_text) == (
        "Right of Way Agent", "K3", "R. Alvarez"
    )
    assert _row(reading, 4).retained == ()


def test_an_untypeable_populated_cell_is_reported_with_its_materiality(tmp_path):
    """`Promised For` is material; `Action Due Date` is not, and both are read."""

    promised = ["UC-6", "Atmos Energy", "Gas", "6 in", "Steel",
                "1210+00", "1211+00", "Relocate", "TBD", "2026-06-01"]
    due = ["UC-7", "Google Fiber", "Telecom", "2 in", "HDPE",
           "1220+00", "1221+00", "Adjust", "2026-07-01", "TBD"]
    reading = read_baseline_workbook(_workbook(tmp_path, [promised, due]))

    by_field = {item.field: item for item in reading.unsupported_values}
    assert set(by_field) == {"committed_date", "action_due_date"}
    assert by_field["committed_date"].material is True
    assert by_field["action_due_date"].material is False
    assert by_field["committed_date"].exact_text == "TBD"
    assert by_field["committed_date"].cell_range == "I3"
    assert by_field["committed_date"].reason
    # Nothing is dropped: the untypeable cell leaves the row's typed values and
    # is carried on the row itself, so the adoption act sees both.
    assert _row(reading, 3).value("committed_date") is None
    assert [item.field for item in _row(reading, 3).unsupported] == ["committed_date"]


def test_a_declared_external_reference_leaves_the_unknown_columns(tmp_path):
    headings = [*HEADINGS, "UCM Record ID"]
    reading = read_baseline_workbook(
        _workbook(tmp_path, [[*CONFLICT, "UCM-1001"]], headings=headings),
        external_references={"ucm record id": "external_system_id"},
    )

    assert _row(reading, 3).external_system_id == "UCM-1001"
    assert "UCM Record ID" not in {
        column.heading for column in reading.unknown_columns
    }
    assert _row(reading, 3).retained == ()


def test_an_undeclared_reference_role_refuses_the_reading(tmp_path):
    with pytest.raises(BaselineWorkbookUnsupported, match="external-reference role"):
        read_baseline_workbook(
            _workbook(tmp_path, [CONFLICT]),
            external_references={"ucm record id": "invented_role"},
        )


def test_a_sheet_that_heads_no_conflict_matrix_refuses_the_reading(tmp_path):
    path = tmp_path / "notes.xlsx"
    book = Workbook()
    book.active.title = "Notes"
    book.active.append(["Meeting", "Attendee"])
    book.active.append(["2026-03-01", "R. Alvarez"])
    book.save(path)

    with pytest.raises(BaselineWorkbookUnsupported):
        read_baseline_workbook(path)


def test_a_conflict_sheet_with_no_populated_rows_blocks_the_reading(tmp_path):
    reading = read_baseline_workbook(_workbook(tmp_path, []))

    assert not reading.resolved
    assert [item.code for item in reading.blocking_diagnostics] == ["no_source_rows"]


def test_a_file_that_is_not_a_workbook_refuses_the_reading(tmp_path):
    path = tmp_path / "ucm.xlsx"
    path.write_bytes(b"not a workbook")

    with pytest.raises(BaselineWorkbookUnsupported, match="cannot be read as a workbook"):
        read_baseline_workbook(path)
