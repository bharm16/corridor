"""Deterministic conversion of retained binary spreadsheet values."""

from datetime import datetime
from openpyxl import load_workbook

from corridor.spreadsheet_conversion import _workbook_to_xlsx


class FakeSheet:
    merged_cell_ranges = (((0, 0), (0, 1)),)

    def to_python(self):
        return [
            ["Test Hole Index", None, None],
            ["TEST HOLE #", "DEPTH", "DATE"],
            ["169-A", 4.54, datetime(2024, 11, 13)],
        ]


class FakeBook:
    sheet_names = ["THDS INDEX"]

    def get_sheet_by_name(self, name):
        assert name == "THDS INDEX"
        return FakeSheet()


def test_binary_workbook_values_convert_to_stable_readable_xlsx(tmp_path):
    first = _workbook_to_xlsx(FakeBook())
    second = _workbook_to_xlsx(FakeBook())
    assert first == second

    path = tmp_path / "converted.xlsx"
    path.write_bytes(first)
    workbook = load_workbook(path, data_only=True, read_only=False)
    try:
        sheet = workbook["THDS INDEX"]
        assert sheet["A1"].value == "Test Hole Index"
        assert str(sheet.merged_cells) == "A1:B1"
        assert sheet["A3"].value == "169-A"
        assert sheet["B3"].value == 4.54
        assert sheet["C3"].value == datetime(2024, 11, 13)
        assert sheet["C3"].number_format == "yyyy-mm-dd"
        assert sheet.freeze_panes == "A2"
        assert sheet.auto_filter.ref == "A1:C3"
        assert sheet.column_dimensions["A"].width >= len("TEST HOLE #")
        assert sheet["B3"].number_format == "0.###"
    finally:
        workbook.close()
