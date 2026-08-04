"""Reading a spreadsheet source natively (ADR-0005).

TxDOT's Utility Conflict Matrix *is* a spreadsheet, and the PDFs everywhere
else in this corpus are printouts of it. Every defect ADR-0004 catalogues is
print damage to data that was structured before someone exported it —
separators lost between text spans, cells clipped at their boundaries, four
columns collapsed into `Start Station, Offset`, an owner pushed out of the
table into a page header.

So nothing here uses a model. The structure is explicit in the file: named
columns, a header row, one value per cell. Running a vision model over a
spreadsheet would be recovering from pixels what the file already states,
and it would reintroduce the transcription error class ADR-0006 removed by
construction.

Two things this deliberately does **not** do:

- **Assume a conflict id is unique.** The form's own data dictionary says
  `Utility Conflict ID` is "unique within the transportation project"; real
  documents disagree, and one Project A revision reuses 47 of them. The
  intent is worth recording precisely because the data does not honour it.
- **Assume a workbook has one table.** The published template has seven
  sheets, two of which head a column `Utility Conflict ID`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from corridor.extract_matrix import REQUIRED, TEMPLATE_FIELDS
from corridor.geometry import NoMatrixFound

# How deep a header can sit. The published template puts a merged title
# band on row 1 and the real headings on row 2; nothing in this corpus goes
# deeper, and a "header" found ten rows down is a data row that happens to
# read like one.
HEADER_SEARCH_ROWS = 6

# ...and how many canonical fields a row must name to be the header rather
# than a title band. A band populates one cell of the row it spans, which
# is the shape being excluded.
MIN_HEADER_FIELDS = 2

# canonical field -> the template column it holds, inverted for lookup by
# printed heading. Built from `TEMPLATE_FIELDS` rather than restated, so a
# spreadsheet and a printed page cannot drift into two vocabularies — the
# whole argument of ADR-0005 is that they are one document in two forms.
_BY_HEADING = {
    " ".join(column.split()).casefold(): field
    for field, column in TEMPLATE_FIELDS.items()
}


class NoConflictSheet(NoMatrixFound):
    """No sheet in this workbook is a utility conflict matrix.

    Raised rather than returning nothing, for the reason #59 story 2 gives
    about the page extractor: a workbook nobody could read must not report
    as a workbook with no conflicts.

    A `NoMatrixFound`, because it is the same answer about a different
    container — which is what lets `extract_project` report a workbook it
    could not read through the path it already has, rather than growing a
    second one that means the same thing.
    """


@dataclass(frozen=True)
class Sheet:
    """One worksheet, as rows of text.

    Text rather than cell values, because every reader downstream treats a
    cell as something a citation could quote. A `None` or a float leaking
    through would make each of them defend against it separately.
    """

    name: str
    rows: tuple[tuple[str, ...], ...]


def read_workbook(path: Path | str) -> list[Sheet]:
    """Every sheet of the workbook, in the order the file lists them.

    `data_only` so a formula cell yields its last computed value rather
    than its formula: the value is what the document asserts, and `=B2*2`
    is not something a reviewer can check a quote against.
    """
    workbook = load_workbook(Path(path), data_only=True, read_only=True)
    try:
        return [
            Sheet(
                name=name,
                rows=tuple(
                    tuple(_cell(value) for value in row)
                    for row in workbook[name].iter_rows(values_only=True)
                ),
            )
            for name in workbook.sheetnames
        ]
    finally:
        workbook.close()


def header_row(sheet: Sheet) -> int | None:
    """Which row carries the column headings, or None if none does.

    The first row naming at least `MIN_HEADER_FIELDS` canonical fields.
    Declining is the answer for a sheet whose headings this vocabulary does
    not carry — a project-information block, a dropdown list — because
    guessing at the first populated row would file its cells under whatever
    the row above happened to say.
    """
    for index, row in enumerate(sheet.rows[:HEADER_SEARCH_ROWS]):
        if len(column_mapping(row)) >= MIN_HEADER_FIELDS:
            return index
    return None


def column_mapping(headings) -> dict[int, str]:
    """Column index to canonical field, by printed name.

    Exact after collapsing whitespace, and no synonyms: this reads a
    published form whose columns are named in its own data dictionary, so a
    heading that does not match is a column the vocabulary has not ruled on
    rather than a spelling to guess at. The page extractor needs a model
    for this precisely because a printout does not carry the form's names.
    """
    mapping: dict[int, str] = {}
    for index, heading in enumerate(headings):
        field = _BY_HEADING.get(" ".join(str(heading or "").split()).casefold())
        if field and field not in mapping.values():
            mapping[index] = field
    return mapping


def conflict_sheet(sheets: list[Sheet]) -> Sheet:
    """The one sheet that is a utility conflict matrix.

    Identified by the conflict id, which is ADR-0009's distinction
    mechanised: an inventory records what is out there and a conflict
    matrix records conflicts, and `Utility Feature ID` is not a conflict
    id. The published template ships both, and reading the inventory as a
    matrix is the error ADR-0009 was written about.

    Where more than one sheet qualifies — the template also carries
    `Utility Conflict List-Print`, a rendering of the same rows — the
    fuller sheet wins. That is ADR-0005 applied inside one workbook: a
    printed view does not outrank the structured original it was made
    from, and being narrower is what makes it the view.
    """
    best: tuple[int, Sheet] | None = None
    for sheet in sheets:
        index = header_row(sheet)
        if index is None:
            continue
        mapping = column_mapping(sheet.rows[index])
        if not all(field in mapping.values() for field in REQUIRED):
            continue
        if best is None or len(mapping) > best[0]:
            best = (len(mapping), sheet)

    if best is None:
        raise NoConflictSheet(
            f"no sheet of this workbook heads a utility conflict matrix; "
            f"read {', '.join(s.name for s in sheets) or 'nothing'}. A "
            "layout variant is unhandled — do not treat this as a workbook "
            "with no conflicts."
        )
    return best[1]


def sheet_text(sheet: Sheet) -> str:
    """The sheet as the text a citation is checked against.

    A sheet has no page image, so this is the rendering that stands in for
    one — which is why it has to carry every cell a stored value could come
    from. Rows stay on their own lines: flattening them would let a quote
    match across two conflicts that never appeared on one row together.
    """
    return "\n".join(
        " ".join(cell for cell in row if cell) for row in sheet.rows
    ).strip()


def _cell(value) -> str:
    """A cell as the text it shows.

    Integral floats lose the `.0` openpyxl gives them: a station reads
    `1149` on the form, and storing `1149.0` would put a value in the
    Ledger that appears nowhere in the document a reviewer opens.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()
