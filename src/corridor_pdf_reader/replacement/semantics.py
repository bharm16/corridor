"""The semantics tier: which printed column is which canonical field, by ID.

ADR-0006 divides the reading of a Utility Conflict Matrix in two. The reader
(`replacement.reader`, engine `tagged`) turns the page into cells with
verbatim text and boxes. This tier shows those cells to a model and asks
only what they mean: which table is the matrix, which row carries the
headings, which column holds which canonical field, and which cell states a
fact for the whole page. Every answer is an index or an ID into the listing
the model was shown; the model never writes a value. Code then assembles
each conflict row from the reader's own cells, so every stored value is a
cell the page holds, with the cell's ID beside it as provenance.

What the model returns is checked before it is believed: an index or ID the
listing does not hold is refused, a field outside the canonical vocabulary
is reported unmapped, a second column claiming a field is unmapped, and a
page attribute pointing into the conflict rows is refused. Refusals are
recorded on the reading, not swallowed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from corridor_pdf_reader.replacement.pages import cell_id, outside_id
from corridor_pdf_reader.replacement.vocabulary import (
    MARK_CELLS,
    MARKED_COLUMN_FIELD,
    MIN_ROW_FIELDS,
    PAGE_FIELDS,
    REQUIRED,
    ROW_FIELDS,
    is_retired_row,
    is_sequencing_header,
    normalize_header,
)

PROMPT_VERSION = "matrix_structure_ids_v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / f"{PROMPT_VERSION}.md"
# Rows of each table shown to the model: enough to reach the heading row
# beneath a title block and a group band, and a few conflict rows beneath it.
LISTED_ROWS = 16
ANSWER_SEPARATOR = "; "
_CELL_ID = re.compile(r"^t(\d+)r(\d+)c(\d+)$")
_OUTSIDE_ID = re.compile(r"^o(\d+)$")
_WS = re.compile(r"\s+")

STRUCTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_utility_matrix", "matrix_table", "header_row", "columns", "page_attributes", "mapping_confidence"],
    "properties": {
        "is_utility_matrix": {"type": "boolean"},
        "matrix_table": {"type": ["integer", "null"]},
        "header_row": {"type": ["integer", "null"]},
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "canonical_field"],
                "properties": {"index": {"type": "integer"}, "canonical_field": {"type": ["string", "null"]}},
            },
        },
        "page_attributes": {
            "type": "object",
            "additionalProperties": False,
            "required": list(PAGE_FIELDS),
            "properties": {name: {"type": ["string", "null"]} for name in PAGE_FIELDS},
        },
        "mapping_confidence": {"type": "number"},
    },
}


class StructuredClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        images: Sequence[Path | str] = (),
    ) -> dict[str, Any]: ...


class SequencingSemanticsDetected(RuntimeError):
    """The document asserts work sequencing, which is not modelled; it is refused whole."""


@dataclass(frozen=True)
class ColumnMapping:
    """What each column of one printed header holds; `marks` is the marked group, if any."""

    fields: dict[int, str]
    unmapped: list[str]
    marks: dict[int, str]

    def signature(self) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...]]:
        return (tuple(sorted(self.fields.items())), tuple(sorted(self.marks.items())))


@dataclass(frozen=True)
class Value:
    """A value and the cell(s) it was read from."""

    text: str
    cells: tuple[str, ...]


@dataclass
class RowReading:
    row_id: str
    row: int
    disposition: str
    reason: str
    fields: dict[str, Value] = field(default_factory=dict)


@dataclass
class PageReading:
    number: int
    is_utility_matrix: bool
    matrix_table: int | None
    header_row: int | None
    mapping: ColumnMapping | None
    page_attributes: dict[str, Value]
    rows: list[RowReading]
    mapping_confidence: float | None
    refused: list[str]


def clean(text: str) -> str:
    return _WS.sub(" ", text.replace("\n", " ")).strip()


def table_grid(table: dict[str, Any]) -> dict[int, dict[int, dict[str, Any]]]:
    """Row -> column -> cell, for a table's cells."""
    grid: dict[int, dict[int, dict[str, Any]]] = {}
    for cell in table["cells"]:
        grid.setdefault(cell["row"], {})[cell["column"]] = cell
    return grid


def table_width(table: dict[str, Any]) -> int:
    return 1 + max((cell["column"] + cell["column_span"] - 1 for cell in table["cells"]), default=-1)


def listing(page: dict[str, Any], document: str) -> str:
    """The page as the model sees it: numbered tables, rows and cells, then outside text."""
    lines = [
        f"Page {page['number']} of {document}.",
        "IDs: t<table>r<row>c<column> names a table cell; o<k> names a string outside every table.",
        "Tables found on this page, cells as read from the page:",
    ]
    for index, table in enumerate(page["tables"]):
        grid = table_grid(table)
        rows = sorted(grid)
        shown = rows[:LISTED_ROWS]
        lines.append(f"\nTable {index} — {table_width(table)} columns, {len(rows)} rows" + (f" (rows {shown[0]}–{shown[-1]} shown)" if shown else "") + ":")
        for row in shown:
            cells = " | ".join(f"[{column}] {clean(cell['text'])}" for column, cell in sorted(grid[row].items()) if clean(cell["text"]))
            lines.append(f"  row {row}: {cells}")
        if len(rows) > len(shown):
            lines.append(f"  ... {len(rows) - len(shown)} more rows")
    if page["outside"]:
        lines.append("\nText outside every table:")
        for index, item in enumerate(page["outside"]):
            lines.append(f"  {outside_id(index)}: {clean(item['text'])}")
    return "\n".join(lines)


def headers_of(table: dict[str, Any], header_row: int) -> dict[int, str]:
    """The heading over each column; a heading merged across columns heads them all."""
    headers: dict[int, str] = {}
    for column, cell in table_grid(table).get(header_row, {}).items():
        for spanned in range(column, column + max(1, int(cell.get("column_span", 1)))):
            headers.setdefault(spanned, clean(cell["text"]))
    return headers


def column_mapping(headers: dict[int, str], width: int, structure: dict[str, Any], refused: list[str]) -> ColumnMapping:
    """What each column holds, after the model's answer is checked.

    A field outside the vocabulary or claimed twice is recorded by its printed
    heading for a human, never stored under a guessed name. The resolution
    strategy is held back until every column is read, because whether it is
    one column of prose or several marked ones is not knowable from one.
    """
    for printed in headers.values():
        if is_sequencing_header(printed):
            raise SequencingSemanticsDetected(f"column {printed!r} asserts work sequencing; the document is refused whole")
    fields: dict[int, str] = {}
    unmapped: list[str] = []
    strategy: dict[int, str] = {}
    for column in structure.get("columns") or []:
        index = column.get("index")
        if not isinstance(index, int) or not 0 <= index < width:
            refused.append(f"column index {index!r} is not in the table")
            continue
        printed = headers.get(index, "")
        canonical = column.get("canonical_field")
        if canonical == MARKED_COLUMN_FIELD:
            strategy[index] = printed
            continue
        if canonical in ROW_FIELDS:
            if canonical in fields.values():
                refused.append(f"column {index} ({printed!r}) claims {canonical} already taken")
                if normalize_header(printed):
                    unmapped.append(printed)
                continue
            fields[index] = canonical
            continue
        if canonical is not None:
            refused.append(f"column {index} ({printed!r}) named {canonical!r}, which is not a canonical field")
        if normalize_header(printed):
            unmapped.append(printed)
    if len(strategy) > 1:
        return ColumnMapping(fields, unmapped, strategy)
    fields.update({index: MARKED_COLUMN_FIELD for index in strategy})
    return ColumnMapping(fields, unmapped, {})


def resolve(page: dict[str, Any], reference: Any) -> tuple[str, dict[str, Any], tuple[int, int] | None] | None:
    """The text behind an ID, with its table and row when it is a cell."""
    if not isinstance(reference, str):
        return None
    match = _CELL_ID.match(reference)
    if match:
        table, row, column = (int(v) for v in match.groups())
        if table < len(page["tables"]):
            cell = table_grid(page["tables"][table]).get(row, {}).get(column)
            if cell is not None:
                return reference, cell, (table, row)
        return None
    match = _OUTSIDE_ID.match(reference)
    if match:
        index = int(match.group(1))
        if index < len(page["outside"]):
            return reference, page["outside"][index], None
    return None


def page_attributes(page: dict[str, Any], structure: dict[str, Any], matrix_table: int | None, header_row: int | None, refused: list[str]) -> dict[str, Value]:
    """Facts the page states once, each read from the cell the model named."""
    found: dict[str, Value] = {}
    attributes = structure.get("page_attributes") or {}
    for name in PAGE_FIELDS:
        reference = attributes.get(name)
        if reference is None:
            continue
        hit = resolve(page, reference)
        if hit is None:
            refused.append(f"page attribute {name} names {reference!r}, which the page does not hold")
            continue
        identifier, item, location = hit
        if location is not None and location[0] == matrix_table and header_row is not None and location[1] > header_row:
            refused.append(f"page attribute {name} names {reference!r}, a cell of the conflict rows")
            continue
        text = clean(item["text"])
        if text:
            found[name] = Value(text, (identifier,))
    return found


def read_page(page: dict[str, Any], structure: dict[str, Any], carried: tuple[ColumnMapping, int] | None = None) -> tuple[PageReading, tuple[ColumnMapping, int] | None]:
    """Assemble the page's conflict rows from its cells under the model's mapping."""
    refused: list[str] = []
    number = int(page["number"])
    is_matrix = bool(structure.get("is_utility_matrix"))
    table_index = structure.get("matrix_table")
    if table_index is not None and not (isinstance(table_index, int) and 0 <= table_index < len(page["tables"])):
        refused.append(f"matrix_table {table_index!r} is not a table on this page")
        table_index = None
    if not is_matrix or table_index is None:
        return PageReading(number, is_matrix, None, None, None, {}, [], structure.get("mapping_confidence"), refused), carried
    table = page["tables"][table_index]
    grid = table_grid(table)
    width = table_width(table)
    header_row = structure.get("header_row")
    if header_row is not None and not (isinstance(header_row, int) and header_row in grid):
        refused.append(f"header_row {header_row!r} is not a row of table {table_index}")
        header_row = None
    mapping: ColumnMapping | None = None
    if isinstance(header_row, int):
        mapping = column_mapping(headers_of(table, header_row), width, structure, refused)
        carried = (mapping, width)
    elif carried is not None and carried[1] == width:
        # A continuation page: the matrix runs on but its headings were
        # printed pages ago; a printed header outranks a mapping inferred
        # from data, so the carried one is kept whole.
        mapping = carried[0]
    else:
        refused.append("no header row and no mapping carried from an earlier page")
    inherited = page_attributes(page, structure, table_index, header_row, refused)
    rows: list[RowReading] = []
    if mapping is not None:
        body = [row for row in sorted(grid) if header_row is None or row > header_row]
        for ordinal, row in enumerate(body, start=1):
            rows.append(_row(number, table_index, row, ordinal, grid[row], mapping, inherited))
    return PageReading(number, True, table_index, header_row, mapping, inherited, rows, structure.get("mapping_confidence"), refused), carried


def _row(page: int, table: int, row: int, ordinal: int, cells: dict[int, dict[str, Any]], mapping: ColumnMapping, inherited: dict[str, Value]) -> RowReading:
    row_id = f"structure:{page}:table:{table}:row:{ordinal}"
    if not any(clean(cell["text"]) for cell in cells.values()):
        return RowReading(row_id, row, "blank", "blank_source_row")
    own: dict[str, Value] = {}
    for column, name in mapping.fields.items():
        cell = cells.get(column)
        if cell is None:
            continue
        text = clean(cell["text"])
        if text:
            own[name] = Value(text, (cell_id(table, row, column),))
    if mapping.marks:
        marked = [(cell_id(table, row, column), heading) for column, heading in sorted(mapping.marks.items()) if heading and column in cells and clean(cells[column]["text"])]
        if marked:
            own[MARKED_COLUMN_FIELD] = Value(ANSWER_SEPARATOR.join(heading for _, heading in marked), tuple(identifier for identifier, _ in marked))
    elif MARKED_COLUMN_FIELD in own and own[MARKED_COLUMN_FIELD].text.casefold() in MARK_CELLS:
        # A bare mark under a heading nobody kept says nothing.
        del own[MARKED_COLUMN_FIELD]
    if is_retired_row({name: value.text for name, value in own.items()}):
        return RowReading(row_id, row, "skipped", "retired_row", own)
    if len(own) < MIN_ROW_FIELDS:
        return RowReading(row_id, row, "skipped", "insufficient_mapped_fields", own)
    fields = {**inherited, **own}
    if not all(name in fields for name in REQUIRED):
        return RowReading(row_id, row, "skipped", "missing_required_fields", fields)
    return RowReading(row_id, row, "extracted", "candidate_recorded", fields)


def map_page(client: StructuredClient, page: dict[str, Any], document: str, image: Path | None = None, system: str | None = None) -> dict[str, Any]:
    """Ask the model for the page's structure; the answer is raw and unchecked here."""
    prompt = system if system is not None else PROMPT_PATH.read_text()
    return client.complete(system=prompt, user=listing(page, document), schema=STRUCTURE_SCHEMA, images=[image] if image else ())


def read_document(client: StructuredClient, pages: list[dict[str, Any]], document: str, images: dict[int, Path] | None = None) -> list[tuple[dict[str, Any], PageReading]]:
    """Every page's raw structure and checked reading, mappings carried forward."""
    carried: tuple[ColumnMapping, int] | None = None
    out: list[tuple[dict[str, Any], PageReading]] = []
    for page in pages:
        structure = map_page(client, page, document, (images or {}).get(int(page["number"])))
        reading, carried = read_page(page, structure, carried)
        out.append((structure, reading))
    return out


def reading_dict(reading: PageReading) -> dict[str, Any]:
    data = asdict(reading)
    if reading.mapping is not None:
        data["mapping"] = {"fields": {str(k): v for k, v in reading.mapping.fields.items()}, "unmapped": reading.mapping.unmapped, "marks": {str(k): v for k, v in reading.mapping.marks.items()}}
    return data


def _render(source: Path, numbers: list[int], directory: Path, dpi: int) -> dict[int, Path]:
    import pypdfium2 as pdfium

    images: dict[int, Path] = {}
    pdf = pdfium.PdfDocument(source)
    try:
        for number in numbers:
            target = directory / f"page-{number}.png"
            pdf[number - 1].render(scale=dpi / 72).to_pil().save(target)
            images[number] = target
    finally:
        pdf.close()
    return images


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Map a Utility Conflict Matrix PDF's structure by cell ID and assemble its rows.")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--pages", help="page range such as 1-3; every page when omitted")
    parser.add_argument("--output", type=Path, required=True, help="new directory for document.json and page renders")
    parser.add_argument("--model", default=None)
    parser.add_argument("--env-file", type=Path, default=None, help="dotenv file holding OPENAI_API_KEY when it is not in the environment")
    parser.add_argument("--dpi", type=int, default=110, help="render resolution of the page image shown to the model; 0 sends no image")
    parser.add_argument("--replay", type=Path, default=None, help="document.json of an earlier run: reuse its structures and assemble rows again without calling the model")
    args = parser.parse_args(argv)

    from corridor_pdf_reader.replacement.llm import DEFAULT_MODEL, OpenAIClient, api_key_from
    from corridor_pdf_reader.replacement.pages import slim_page
    from corridor_pdf_reader.replacement.reader import read_pdf

    if args.output.exists():
        raise SystemExit(f"{args.output} exists; choose a new directory")
    args.output.mkdir(parents=True)
    numbers = _page_numbers(args.pages, args.pdf)
    read = read_pdf(args.pdf, numbers, engine="tagged", dpi=36)
    pages = [slim_page(page) for page in read["pages"]]
    if args.replay is not None:
        earlier = json.loads(args.replay.read_text())
        stored = {int(page["number"]): page["structure"] for page in earlier["pages"]}
        results = []
        carried: tuple[ColumnMapping, int] | None = None
        for page in pages:
            reading, carried = read_page(page, stored[int(page["number"])], carried)
            results.append((stored[int(page["number"])], reading))
        model, usage = earlier["model"], earlier["usage"]
    else:
        images = _render(args.pdf, numbers, args.output, args.dpi) if args.dpi > 0 else {}
        client = OpenAIClient(model=args.model or DEFAULT_MODEL, api_key=api_key_from(args.env_file))
        try:
            results = read_document(client, pages, args.pdf.name, images)
        finally:
            client.close()
        model = client.model
        usage = {"calls": client.calls, "prompt_tokens": client.prompt_tokens, "completion_tokens": client.completion_tokens, "cached_tokens": client.cached_tokens}
    document = {
        "source": str(args.pdf),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "usage": usage,
        "pages": [{"number": reading.number, "structure": structure, "reading": reading_dict(reading), "listing": listing(page, args.pdf.name)} for page, (structure, reading) in zip(pages, results, strict=True)],
    }
    (args.output / "document.json").write_text(json.dumps(document, indent=1))
    for _, reading in results:
        extracted = sum(1 for row in reading.rows if row.disposition == "extracted")
        fields = sorted(set(reading.mapping.fields.values())) if reading.mapping else []
        print(f"page {reading.number}: matrix={reading.is_utility_matrix} table={reading.matrix_table} header_row={reading.header_row} rows={extracted}/{len(reading.rows)} fields={len(fields)} unmapped={len(reading.mapping.unmapped) if reading.mapping else 0} refused={len(reading.refused)}")
    print(f"{usage['calls']} calls, {usage['prompt_tokens']} prompt tokens ({usage['cached_tokens']} cached), {usage['completion_tokens']} completion tokens -> {args.output / 'document.json'}")
    return 0


def _page_numbers(spec: str | None, source: Path) -> list[int]:
    if spec:
        if "-" in spec:
            low, high = spec.split("-", 1)
            return list(range(int(low), int(high) + 1))
        return [int(part) for part in spec.split(",")]
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(source)
    try:
        return list(range(1, len(pdf) + 1))
    finally:
        pdf.close()


if __name__ == "__main__":
    sys.exit(main())
