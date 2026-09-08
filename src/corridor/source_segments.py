"""Store one exact source value and replay it from original bytes.

Copied quotations made the old evidence path shallow: every consumer owned a
slightly different text copy.  Source Segments give those consumers one durable
address instead. Structured workbook cells and Minutes prose spans are the first
two locator shapes. Both come directly from original bytes: native cell
coordinates for a workbook, and page-local character bounds for a PDF text
layer (ADR-0068). The new pdf_span/pdf_cell schemes delegate to reader_segments
(#736), which records the rendition, exact reader/configuration result and
physical glyph locations. The legacy prose_span kind always retains its old
reader and offsets; selecting a challenger cannot reinterpret them.

The module deliberately owns both directions of the contract.  Segmentation
turns source bytes into ordered append-only rows; dereference follows a row's
typed locator back through the same native reader and checks the registered
Document digest, stored text digest, and recovered value before returning text.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from openpyxl import load_workbook
import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Document, SourceSegment
from corridor.source_append import SegmentValues, append_source_segments
from corridor.prose_spans import (
    NumberedActionSpan,
    numbered_action_spans,
    page_prose_ranges,
)

SPREADSHEET_SUFFIXES = frozenset({".xlsx", ".xlsm"})


from corridor.source_segment_errors import (
    SourceSegmentIntegrityError,
    SourceDocumentDigestMismatch,
    SourceSegmentDigestMismatch,
    SourceSegmentLocatorMismatch,
)


@dataclass(frozen=True)
class SpreadsheetSegment:
    """One populated workbook cell in deterministic workbook order."""

    ordinal: int
    sheet_name: str
    cell_range: str
    exact_text: str
    content_sha256: str


@dataclass(frozen=True)
class ProseSegment:
    """One non-overlapping exact prose span in deterministic page order."""

    ordinal: int
    page_no: int
    start_offset: int
    end_offset: int
    exact_text: str
    content_sha256: str


def spreadsheet_segments(path: Path | str) -> tuple[SpreadsheetSegment, ...]:
    """Read every populated cell once, preserving workbook, row, and column order."""

    segments: list[SpreadsheetSegment] = []
    workbook = load_workbook(Path(path), data_only=True, read_only=True)
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    exact_text = _exact_cell_text(cell.value)
                    if exact_text == "":
                        continue
                    segments.append(
                        SpreadsheetSegment(
                            ordinal=len(segments) + 1,
                            sheet_name=sheet.title,
                            cell_range=cell.coordinate,
                            exact_text=exact_text,
                            content_sha256=_text_digest(exact_text),
                        )
                    )
    finally:
        workbook.close()
    return tuple(segments)


def pdf_prose_segments(path: Path | str) -> tuple[ProseSegment, ...]:
    """Read non-overlapping Minutes spans from each native PDF text layer."""

    segments: list[ProseSegment] = []
    with pymupdf.open(Path(path)) as pdf:
        for page_index, page in enumerate(pdf):
            page_no = page_index + 1
            text = page.get_text()
            for start, end in page_prose_ranges(text):
                exact_text = text[start:end]
                segments.append(
                    ProseSegment(
                        ordinal=len(segments) + 1,
                        page_no=page_no,
                        start_offset=start,
                        end_offset=end,
                        exact_text=exact_text,
                        content_sha256=_text_digest(exact_text),
                    )
                )
    return tuple(segments)


def append_ingested_source_segments(
    session: Session, document: Document, path: Path | str, *, native_reading=None
) -> tuple[SourceSegment, ...]:
    """Append the supported segments for registered source bytes, at most once."""

    original = Path(path)
    # A configured native challenger must never append incumbent prose beside
    # its new page string, including the ordinary ingest deduplication path.
    from corridor.config import settings
    from corridor.reader_segments import append_native_segments, read_native_pdf

    if native_reading is not None or (
        settings.native_reader_token_layer and original.suffix.lower() == ".pdf"
    ):
        _require_registered_bytes(document, original)
        reading = native_reading or read_native_pdf(original, source_sha256=document.sha256)
        return append_native_segments(session, document, reading)
    is_spreadsheet = original.suffix.lower() in SPREADSHEET_SUFFIXES
    is_minutes_pdf = (
        document.doc_type == "minutes" and original.suffix.lower() == ".pdf"
    )
    if not original.exists() or not (is_spreadsheet or is_minutes_pdf):
        return ()
    _require_registered_bytes(document, original)
    existing = tuple(
        session.scalars(
            select(SourceSegment)
            .where(SourceSegment.document_id == document.id)
            .where(SourceSegment.kind.in_(("spreadsheet_cell", "prose_span")))
            .order_by(SourceSegment.ordinal)
        ).all()
    )
    if existing:
        return existing

    if is_spreadsheet:
        values = tuple(
            SegmentValues(
                kind="spreadsheet_cell",
                exact_text=segment.exact_text,
                content_sha256=segment.content_sha256,
                ordinal=segment.ordinal,
                sheet_name=segment.sheet_name,
                cell_range=segment.cell_range,
            )
            for segment in spreadsheet_segments(original)
        )
    else:
        values = tuple(
            SegmentValues(
                kind="prose_span",
                exact_text=segment.exact_text,
                content_sha256=segment.content_sha256,
                ordinal=segment.ordinal,
                page_no=segment.page_no,
                start_offset=segment.start_offset,
                end_offset=segment.end_offset,
            )
            for segment in pdf_prose_segments(original)
        )
    return append_source_segments(
        session,
        project_id=document.project_id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=values,
    )


def append_source_segment(session: Session, segment: SourceSegment) -> SourceSegment:
    """Append one built-but-unsaved segment through the command and return the row.

    The application holds no ``INSERT`` on ``source_segments`` (#492), so a
    segment built in memory (a Recorded Verbal Statement's exact words) reaches
    the table only this way.  A segment that already has an identity is
    returned as it is.
    """

    if segment.id is not None:
        return segment
    (row,) = append_source_segments(
        session,
        project_id=segment.project_id,
        document_id=segment.document_id,
        recorded_verbal_origin_id=segment.recorded_verbal_origin_id,
        segments=(
            SegmentValues(
                kind=segment.kind,
                exact_text=segment.exact_text,
                content_sha256=segment.content_sha256,
                ordinal=segment.ordinal,
                sheet_name=segment.sheet_name,
                cell_range=segment.cell_range,
                page_no=segment.page_no,
                start_offset=segment.start_offset,
                end_offset=segment.end_offset,
            ),
        ),
    )
    return row


def dereference_source_segment(
    document: Document, segment: SourceSegment, path: Path | str
) -> str:
    """Replay one segment from its registered bytes or fail without returning text."""

    if segment.document_id != document.id or segment.project_id != document.project_id:
        raise SourceSegmentLocatorMismatch(
            "source segment does not belong to the supplied Document"
        )
    original = Path(path)
    _require_registered_bytes(document, original)
    if _text_digest(segment.exact_text) != segment.content_sha256:
        raise SourceSegmentDigestMismatch("stored segment digest does not match its text")
    if segment.kind == "spreadsheet_cell":
        if segment.sheet_name is None or segment.cell_range is None:
            raise SourceSegmentLocatorMismatch("spreadsheet segment locator is incomplete")
        recovered = _dereference_spreadsheet_cell(
            original, sheet_name=segment.sheet_name, cell_range=segment.cell_range
        )
    elif segment.kind == "prose_span":
        recovered = _dereference_pdf_prose_span(original, segment)
    elif segment.kind in {"pdf_span", "pdf_cell"}:
        from corridor.reader_segments import replay_native_segment

        recovered = replay_native_segment(document, segment, original)
    else:
        raise SourceSegmentLocatorMismatch(
            f"unsupported source segment kind {segment.kind!r}"
        )
    if recovered != segment.exact_text:
        raise SourceSegmentLocatorMismatch(
            "source segment locator does not recover its stored text"
        )
    if _text_digest(recovered) != segment.content_sha256:
        raise SourceSegmentDigestMismatch(
            "dereferenced segment digest does not match its stored digest"
        )
    return recovered


def recorded_verbal_statement_segment(
    *,
    project_id: int,
    recorded_verbal_origin_id: int,
    exact_text: str,
    ordinal: int = 1,
) -> SourceSegment:
    """Build the one exact-wording segment for a Recorded Verbal Statement.

    A verbal has no source Document (ADR-0033): the named recorder's words are
    the source, so the segment points at the recorder's own attestation — the
    spine-native Recorded Verbal origin — and self-certifies its words with a
    digest instead of pointing at document bytes (ADR-0068).  It pointed at a
    legacy ``dependency_events`` row until #512; ADR-0081 stage 1 demoted that
    key to a compatibility mapping the segment never carries.
    """

    if not exact_text.strip():
        raise SourceSegmentLocatorMismatch(
            "a recorded verbal statement segment needs exact words"
        )
    return SourceSegment(
        project_id=project_id,
        document_id=None,
        recorded_verbal_origin_id=recorded_verbal_origin_id,
        kind="recorded_verbal_statement",
        exact_text=exact_text,
        content_sha256=_text_digest(exact_text),
        ordinal=ordinal,
    )


def replay_recorded_verbal_statement(segment: SourceSegment) -> str:
    """Replay a recorded verbal statement from its own words.

    There is no Document to dereference against; integrity is the stored
    digest's self-consistency with the words it certifies. A tampered digest
    or an incomplete locator fails closed rather than returning text.
    """

    if segment.kind != "recorded_verbal_statement":
        raise SourceSegmentLocatorMismatch(
            "segment is not a recorded verbal statement"
        )
    if segment.recorded_verbal_origin_id is None or segment.document_id is not None:
        raise SourceSegmentLocatorMismatch(
            "recorded verbal statement segment locator is incomplete"
        )
    if _text_digest(segment.exact_text) != segment.content_sha256:
        raise SourceSegmentDigestMismatch(
            "stored segment digest does not match its recorded words"
        )
    return segment.exact_text


def _require_registered_bytes(document: Document, path: Path) -> None:
    actual = sha256(path.read_bytes()).hexdigest()
    if actual != document.sha256:
        raise SourceDocumentDigestMismatch(
            "source bytes do not match the registered Document digest"
        )


def _dereference_spreadsheet_cell(
    path: Path, *, sheet_name: str, cell_range: str
) -> str:
    if re.fullmatch(r"[A-Z]+[1-9][0-9]*", cell_range) is None:
        raise SourceSegmentLocatorMismatch(
            f"invalid spreadsheet cell locator {cell_range!r}"
        )
    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise SourceSegmentLocatorMismatch(
                f"spreadsheet locator does not exist: {sheet_name}!{cell_range}"
            )
        value = workbook[sheet_name][cell_range].value
        if value is None:
            raise SourceSegmentLocatorMismatch(
                f"spreadsheet locator does not exist: {sheet_name}!{cell_range}"
            )
        return _exact_cell_text(value)
    finally:
        workbook.close()


def _dereference_pdf_prose_span(path: Path, segment: SourceSegment) -> str:
    if path.suffix.lower() != ".pdf":
        raise SourceSegmentLocatorMismatch("prose span requires registered PDF bytes")
    if (
        not isinstance(segment.page_no, int)
        or not isinstance(segment.start_offset, int)
        or not isinstance(segment.end_offset, int)
        or segment.page_no < 1
        or segment.start_offset < 0
        or segment.end_offset <= segment.start_offset
    ):
        raise SourceSegmentLocatorMismatch("prose span locator is incomplete")
    with pymupdf.open(path) as pdf:
        if segment.page_no > pdf.page_count:
            raise SourceSegmentLocatorMismatch("prose span page does not exist")
        page_text = pdf[segment.page_no - 1].get_text()
    if segment.end_offset > len(page_text):
        raise SourceSegmentLocatorMismatch("prose span bounds exceed the page text")
    return page_text[segment.start_offset : segment.end_offset]


def _exact_cell_text(value: object) -> str:
    """A native workbook value without citation-renderer whitespace folding."""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _text_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
