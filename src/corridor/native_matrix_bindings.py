"""Bind measured matrix IDs to one immutable native source reading (#737).

The measured model speaks page-local IDs, which are deliberately kept in its
unchanged listing. They cannot be used as database addresses. This module
translates them into #736's document/reading-scoped cell index and exact span
locators, validates every proposed value against that source, and seals the
whole mapping before an appender sees it. It has no model client. The model
runner may choose references and classifications, never a stored value.

Marked resolutions carry both the selected mark and its actual header cell,
including a header retained from a continuation page. Outside attributes use
an exactly matching visible native span; an unavailable or ambiguous span is
a refusal, never a fabricated cell or an incumbent-text fallback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
import hmac
import json
import re
import secrets
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.materializer import clean_pdf_source_text
from corridor.models import Document, SourceSegment
from corridor.reader_segments import (
    NativeCellIndex,
    native_segment_values,
    pdf_cell_id,
    select_pdf_cell_segment,
)
from corridor.row_accounting import RowAccounting
from corridor.source_append import SegmentValues
from corridor.source_segment_errors import SourceSegmentLocatorMismatch
from corridor.token_layers import NativePdfReading, PDF_SEGMENT_SCHEME
from corridor_pdf_reader.replacement.pages import slim_page

READER_VERSION = "matrix_structure_ids_v1"
READER_PATH = "native_matrix_cells"
ACCOUNTING_SCHEMA = "native-matrix-row-accounting-v1"
NATIVE_MATRIX_SCHEMA_VERSION = "native-matrix-source-fields-v1"
MODEL_IMAGE_DPI = 110
_CELL = re.compile(r"^t(\d+)r(\d+)c(\d+)$")
_OUTSIDE = re.compile(r"^o(\d+)$")
_MAPPING_KEY = secrets.token_bytes(32)


class NativeMatrixRefused(ValueError):
    """The semantic reading could not be bound; no OCR fallback is implied."""

    def __init__(self, reason: str, *, pages=()):
        super().__init__(reason)
        self.reason = reason
        self.pages_json = _canonical(list(pages))

    def record(self) -> dict[str, Any]:
        return {"kind": "native_matrix_refusal", "reason": self.reason,
                "pages": json.loads(self.pages_json)}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@lru_cache(maxsize=2)
def native_replay_index(reading: NativePdfReading):
    """Two immutable readings at most; replay many Facts without re-segmenting."""
    return MappingProxyType({
        (value.kind, value.ordinal): _canonical(asdict(value))
        for value in native_segment_values(reading)
    })


@dataclass(frozen=True)
class NativeSourceReference:
    model_id: str
    scoped_id: str
    expected_json: str

    @property
    def expected(self) -> SegmentValues:
        return SegmentValues(**json.loads(self.expected_json))

    def select(self, session: Session, document: Document, index: NativeCellIndex) -> SourceSegment:
        expected = self.expected
        if expected.kind == "pdf_cell":
            return select_pdf_cell_segment(
                session, document=document, index=index,
                page_no=expected.page_no, table_index=expected.table_index,
                cell_id=self.scoped_id,
            )
        if (document.id != index.document_id or document.project_id != index.project_id
                or document.sha256 != index.rendition_sha256
                or expected.reading_sha256 != index.reading_sha256):
            raise SourceSegmentLocatorMismatch("native span reference crosses its reading")
        segment = session.scalar(select(SourceSegment).where(
            SourceSegment.project_id == document.project_id,
            SourceSegment.document_id == document.id,
            SourceSegment.kind == "pdf_span",
            SourceSegment.reading_sha256 == index.reading_sha256,
            SourceSegment.ordinal == expected.ordinal,
        ))
        if segment is None or any(
            getattr(segment, name) != getattr(expected, name)
            for name in expected.__dataclass_fields__
        ):
            raise SourceSegmentLocatorMismatch("native span does not match the bound source")
        return segment


@dataclass(frozen=True)
class NativeFieldBinding:
    name: str
    text: str
    value_sources: tuple[NativeSourceReference, ...]
    context_sources: tuple[NativeSourceReference, ...] = ()


@dataclass(frozen=True)
class NativeRowBinding:
    row_id: str
    local_row_id: str
    page_no: int
    table_index: int
    row: int
    disposition: str
    reason: str
    fields: tuple[NativeFieldBinding, ...]
    confidence: float | None
    unmapped: tuple[str, ...]


@dataclass(frozen=True)
class NativeMatrixMapping:
    """A sealed mapping; callers receive copies of every nested JSON value."""

    project_id: int
    document_id: int
    native_reading: NativePdfReading
    rows: tuple[NativeRowBinding, ...]
    pages_json: str
    config_json: str
    seal: bytes

    def __post_init__(self) -> None:
        if not hmac.compare_digest(self.seal, _mapping_seal(
            self.project_id, self.document_id, self.native_reading, self.rows,
            self.pages_json, self.config_json,
        )):
            raise NativeMatrixRefused("native mapping was not bound to its source reading")

    @property
    def pages(self) -> list[dict[str, Any]]:
        return json.loads(self.pages_json)

    @property
    def config_record(self) -> dict[str, Any]:
        return json.loads(self.config_json)

    @property
    def identity(self) -> str:
        return sha256(_mapping_bytes(
            self.project_id, self.document_id, self.native_reading, self.rows,
            self.pages_json, self.config_json,
        )).hexdigest()

    @property
    def row_accounting(self) -> dict[str, Any]:
        accounting = RowAccounting(READER_VERSION, READER_PATH)
        for row in self.rows:
            accounting.detect(row.row_id, page=row.page_no, row_number=row.row)
            accounting.account(row.row_id, disposition=row.disposition, reason=row.reason)
        extracted = [row for row in self.rows if row.disposition == "extracted"]
        return accounting.finish(extracted).row_accounting

    def certify(self, document: Document) -> None:
        self.__post_init__()
        if (document.id != self.document_id or document.project_id != self.project_id
                or document.sha256 != self.native_reading.rendition_sha256):
            raise SourceSegmentLocatorMismatch("native matrix mapping belongs to another Document")


def _mapping_bytes(project, document, reading, rows, pages_json, config_json) -> bytes:
    return _canonical({
        "project_id": project, "document_id": document,
        "reading_sha256": reading.reading_sha256,
        "rows": [asdict(row) for row in rows],
        "pages": json.loads(pages_json), "configuration": json.loads(config_json),
    }).encode("utf-8")


def _mapping_seal(project, document, reading, rows, pages_json, config_json) -> bytes:
    return hmac.new(_MAPPING_KEY, _mapping_bytes(
        project, document, reading, rows, pages_json, config_json,
    ), sha256).digest()


class _References:
    def __init__(self, document: Document, reading: NativePdfReading):
        self.document = document
        self.reading = reading
        self.index = NativeCellIndex(document, reading)
        self.spans = tuple(value for value in native_segment_values(reading) if value.kind == "pdf_span")
        self.pages = {page["number"]: page for page in reading.pages}
        self.cache: dict[tuple[int, str], NativeSourceReference] = {}

    def cell(self, page_no: int, table: int, row: int, column: int) -> NativeSourceReference:
        model_id = f"t{table}r{row}c{column}"
        key = (page_no, model_id)
        if key not in self.cache:
            scoped = pdf_cell_id(self.document, self.reading.reading_sha256, page_no, table, row, column)
            expected = self.index.expected(scoped)
            self.cache[key] = NativeSourceReference(model_id, scoped, _canonical(asdict(expected)))
        return self.cache[key]

    def resolve(self, page_no: int, model_id: str) -> NativeSourceReference:
        match = _CELL.fullmatch(model_id)
        if match:
            return self.cell(page_no, *(int(value) for value in match.groups()))
        match = _OUTSIDE.fullmatch(model_id)
        if match:
            return self.outside(page_no, int(match[1]))
        raise NativeMatrixRefused(f"unrecognised model-local source ID {model_id!r}")

    def outside(self, page_no: int, number: int) -> NativeSourceReference:
        outside = slim_page(self.pages[page_no])["outside"]
        if number >= len(outside):
            raise NativeMatrixRefused("outside ID is absent from its page")
        item = outside[number]
        matches = []
        for value in self.spans:
            if value.page_no != page_no or value.span_stream != "page":
                continue
            if clean_pdf_source_text(value.exact_text) != clean_pdf_source_text(item["text"]):
                continue
            location = value.location_json
            glyphs = location["glyphs"]
            if set(location["outside_source_indices"]) != {g["source_index"] for g in glyphs}:
                continue
            boxes = [g.get("ink_display_box") or g["display_box"] for g in glyphs]
            box = [round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2),
                   round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2)]
            if box == item["box"]:
                matches.append(value)
        if len(matches) != 1:
            raise NativeMatrixRefused("outside attribute has no unique exact native span")
        value = matches[0]
        scoped = (f"{PDF_SEGMENT_SCHEME}:project{self.document.project_id}:document{self.document.id}:"
                  f"{self.reading.reading_sha256}:p{page_no}:span:page:{value.start_offset}:{value.end_offset}")
        return NativeSourceReference(f"o{number}", scoped, _canonical(asdict(value)))

    def header(self, page_no: int, table: int, row: int, column: int) -> NativeSourceReference:
        cells = self.pages[page_no]["tables"]["value"][table]["structured_cells"]
        matches = [cell for cell in cells if cell["row"] == row
                   and cell["column"] <= column < cell["column"] + cell["column_span"]]
        if len(matches) != 1:
            raise NativeMatrixRefused("marked column has no unique source header")
        return self.cell(page_no, table, row, matches[0]["column"])


def bind_native_matrix(
    document: Document, reading: NativePdfReading, pages: list[dict[str, Any]],
    config_record: dict[str, Any],
) -> NativeMatrixMapping:
    """Certify every field against indexed source components before sealing."""
    refs = _References(document, reading)
    if [page["number"] for page in pages] != list(refs.pages):
        raise NativeMatrixRefused("semantic page sequence does not cover the native reading")
    rows = []
    header_context: dict[int, NativeSourceReference] = {}
    for page in pages:
        body = page["reading"]
        table = body["matrix_table"]
        if table is None:
            if body["rows"]:
                raise NativeMatrixRefused("a page without a matrix cannot supply rows")
            continue
        mapping = body["mapping"]
        if mapping is None:
            raise NativeMatrixRefused("matrix mapping was refused; no OCR fallback is permitted")
        # The measured assembler ignores marked columns without a printed
        # heading. Such empty cells intentionally have no Source Segment.
        marks = {int(column): text for column, text in mapping["marks"].items() if text}
        if body["header_row"] is not None:
            header_context = {
                column: refs.header(page["number"], table, body["header_row"], column)
                for column in marks
            }
        for column, text in marks.items():
            if (column not in header_context
                    or clean_pdf_source_text(header_context[column].expected.exact_text) != text):
                raise NativeMatrixRefused("marked heading does not replay from retained header context")
        native_cells = refs.pages[page["number"]]["tables"]["value"][table]["structured_cells"]
        expected_rows = sorted({cell["row"] for cell in native_cells
                                if body["header_row"] is None or cell["row"] > body["header_row"]})
        if [row["row"] for row in body["rows"]] != expected_rows:
            raise NativeMatrixRefused("semantic row accounting omitted or repeated a detected body row")
        for row in body["rows"]:
            fields = tuple(_bind_field(
                               refs, page["number"], name, value, marks, header_context,
                               table=table, row=row["row"], mapping=mapping,
                               attributes=body["page_attributes"],
                               attribute_ids=page["structure"]["page_attributes"],
                               header_row=body["header_row"],
                           )
                           for name, value in sorted(row["fields"].items()))
            row_id = pdf_cell_id(document, reading.reading_sha256, page["number"], table, row["row"], 0).rsplit(":c", 1)[0]
            rows.append(NativeRowBinding(
                row_id, row["row_id"], page["number"], table, row["row"],
                row["disposition"], row["reason"], fields,
                body["mapping_confidence"], tuple(mapping["unmapped"]),
            ))
    rows = tuple(rows)
    pages_json, config_json = _canonical(pages), _canonical(config_record)
    bound = NativeMatrixMapping(
        document.project_id, document.id, reading, rows, pages_json, config_json,
        _mapping_seal(document.project_id, document.id, reading, rows, pages_json, config_json),
    )
    bound.row_accounting  # duplicate identities or dispositions fail before append
    return bound


def _bind_field(
    refs, page_no, name, value, marks, headers, *, table, row, mapping,
    attributes, attribute_ids, header_row,
) -> NativeFieldBinding:
    identifiers = value["cells"]
    if not identifiers or len(identifiers) != len(set(identifiers)):
        raise NativeMatrixRefused("a semantic field needs distinct source references")
    selected = tuple(refs.resolve(page_no, identifier) for identifier in identifiers)
    if name == "resolution_strategy" and marks:
        chosen_headers = []
        for mark in selected:
            source = mark.expected
            column = source.cell_column
            if (source.kind != "pdf_cell" or source.page_no != page_no
                    or source.table_index != table or source.cell_row != row
                    or column not in marks or not clean_pdf_source_text(source.exact_text)):
                raise NativeMatrixRefused("resolution source is not a selected mark")
            chosen_headers.append(headers[column])
        expected = "; ".join(clean_pdf_source_text(header.expected.exact_text) for header in chosen_headers)
        values = tuple(dict.fromkeys(chosen_headers))
        contexts = selected
    else:
        if len(selected) != 1:
            raise NativeMatrixRefused("ordinary native fields require one exact source")
        source = selected[0].expected
        own = (source.kind == "pdf_cell" and source.page_no == page_no
               and source.table_index == table and source.cell_row == row
               and mapping["fields"].get(str(source.cell_column)) == name)
        inherited = (
            name == "external_org" and attributes.get(name) == value
            and attribute_ids.get(name) == identifiers[0]
            and (source.kind == "pdf_span" or source.table_index != table
                 or (header_row is not None and source.cell_row <= header_row))
        )
        if not own and not inherited:
            raise NativeMatrixRefused("field reference is outside its row or declared page attribute")
        expected = clean_pdf_source_text(source.exact_text)
        values, contexts = selected, ()
    if expected != value["text"]:
        raise NativeMatrixRefused("a proposed literal does not reproduce from its source references")
    return NativeFieldBinding(name, expected, values, contexts)
