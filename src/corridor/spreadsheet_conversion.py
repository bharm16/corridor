"""Convert retained binary XLS data into a deterministic readable rendition.

The native reader intentionally rejected `.xls`: openpyxl cannot parse the
legacy binary container, and pretending otherwise registered silent failures.
Discarding the original or treating an ad-hoc conversion as an equivalent
source were also rejected. This module converts cell values only, produces
stable XLSX bytes, and leaves provenance registration to the corpus pipeline.

Pinning `Workbook.properties.modified` before `save()` was tried and is not
enough on its own: openpyxl overwrites it with the current UTC time while it
serializes `docProps/core.xml`, so the archive changed every second and the
corpus content-addressed each re-conversion as new bytes (#579). The canonical
rewrite therefore pins every date-typed core property in the serialized part,
where a later openpyxl release cannot quietly reintroduce a wall clock.

Cell values and merged ranges use the source's absolute coordinates. Calamine's
default trims leading empty rows and columns; applying absolute merges to that
shifted grid silently discarded values. Version 4 preserves the empty area.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from io import BytesIO
import re
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from python_calamine import CalamineWorkbook

CONVERTER_NAME = "corridor.xls-to-xlsx"
CONVERTER_VERSION = "4"
_FIXED_TIME = datetime(2000, 1, 1)
_FIXED_TIMESTAMP = _FIXED_TIME.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_CORE_PROPERTIES = "docProps/core.xml"
# Every core property carrying a timestamp is serialized as a W3CDTF-typed
# element, so the text between its tags is the wall clock to pin.
_W3CDTF_VALUE = re.compile(rb'(<[^<>]*xsi:type="dcterms:W3CDTF"[^<>]*>)[^<]*')


def convert_xls_bytes(source: bytes) -> bytes:
    """Return deterministic XLSX bytes containing every legacy cell value."""

    with CalamineWorkbook.from_filelike(BytesIO(source)) as workbook:
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
        rows = source_sheet.to_python(skip_empty_area=False)
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


def canonical_package(parts: Mapping[str, bytes]) -> bytes:
    """Write one OOXML package to bytes that depend only on its parts.

    Public because every writer that content-addresses its own workbook needs
    exactly this shaping and must not grow a second copy of it: a zip entry
    carries a modification time, a creating system, and a permission mask, and
    each of those is a wall clock or a host detail leaking into a digest. The
    caller supplies the finished part bytes; this decides nothing about them.
    """

    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, _ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            target.writestr(info, parts[name])
    return output.getvalue()


def _canonical_zip(value: bytes) -> bytes:
    with zipfile.ZipFile(BytesIO(value)) as source:
        return canonical_package(
            {
                name: _canonical_entry(name, source.read(name))
                for name in source.namelist()
            }
        )


def _canonical_entry(name: str, payload: bytes) -> bytes:
    """Return one archive part with every recorded wall clock pinned."""

    if name != _CORE_PROPERTIES:
        return payload
    return _W3CDTF_VALUE.sub(lambda match: match.group(1) + _FIXED_TIMESTAMP, payload)


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
