"""Convert retained binary XLS data into a deterministic readable rendition.

The native reader intentionally rejected `.xls`: openpyxl cannot parse the
legacy binary container, and pretending otherwise registered silent failures.
Discarding the original or treating an ad-hoc conversion as an equivalent
source were also rejected. This module converts cell values only, produces
stable XLSX bytes, and leaves provenance registration to the corpus pipeline.
"""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from python_calamine import CalamineWorkbook

CONVERTER_NAME = "corridor.xls-to-xlsx"
CONVERTER_VERSION = "3"
_FIXED_TIME = datetime(2000, 1, 1)
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def convert_xls_bytes(source: bytes) -> bytes:
    """Return deterministic XLSX bytes containing every legacy cell value."""

    workbook = CalamineWorkbook.from_filelike(BytesIO(source))
    return _workbook_to_xlsx(workbook)


def _workbook_to_xlsx(source) -> bytes:
    target = Workbook()
    target.remove(target.active)
    target.properties.creator = CONVERTER_NAME
    target.properties.lastModifiedBy = CONVERTER_NAME
    target.properties.created = _FIXED_TIME
    target.properties.modified = _FIXED_TIME

    for sheet_name in source.sheet_names:
        source_sheet = source.get_sheet_by_name(sheet_name)
        sheet = target.create_sheet(sheet_name)
        rows = source_sheet.to_python()
        for row_index, row in enumerate(rows):
            for column_index, source_value in enumerate(row):
                value = _cell_value(source_value)
                if value is not None:
                    cell = sheet.cell(row=row_index + 1, column=column_index + 1)
                    cell.value = value
                    if isinstance(value, (date, datetime)):
                        cell.number_format = "yyyy-mm-dd"
        for (row_start, column_start), (row_end, column_end) in getattr(
            source_sheet, "merged_cell_ranges", ()
        ):
            sheet.merge_cells(
                start_row=row_start + 1,
                end_row=row_end + 1,
                start_column=column_start + 1,
                end_column=column_end + 1,
            )
        _format_readable_table(sheet, rows)

    if not target.worksheets:
        raise ValueError("legacy workbook has no worksheets")
    buffer = BytesIO()
    target.save(buffer)
    target.close()
    return _canonical_zip(buffer.getvalue())


def _cell_value(value):
    if value is None:
        return None
    if isinstance(value, (bool, date, datetime, int, float, str)):
        return value
    return str(value)


def _canonical_zip(value: bytes) -> bytes:
    source = zipfile.ZipFile(BytesIO(value))
    output = BytesIO()
    with source, zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as target:
        for name in sorted(source.namelist()):
            info = zipfile.ZipInfo(name, _ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            target.writestr(info, source.read(name))
    return output.getvalue()


def _format_readable_table(sheet, rows) -> None:
    if not rows:
        return
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.row_dimensions[1].height = 42
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    column_count = max((len(row) for row in rows), default=0)
    for column_index in range(column_count):
        displayed = [
            str(row[column_index])
            for row in rows
            if column_index < len(row) and row[column_index] not in (None, "")
        ]
        width = min(max((len(value) for value in displayed), default=8) + 2, 32)
        sheet.column_dimensions[get_column_letter(column_index + 1)].width = max(
            width, 8
        )
        for cell in sheet[get_column_letter(column_index + 1)][1:]:
            if isinstance(cell.value, float):
                cell.number_format = "0.###"
