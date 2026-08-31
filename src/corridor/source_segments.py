"""Store one exact source value and replay it from original bytes.

Copied quotations made the old evidence path shallow: every consumer owned a
slightly different text copy.  Source Segments give those consumers one durable
address instead.  This first slice handles structured workbook cells, where the
original bytes already expose both the value and its exact locator without model
interpretation (ADR-0068).

The module deliberately owns both directions of the contract.  Segmentation
turns workbook bytes into ordered append-only rows; dereference follows a row's
typed locator back through the same native reader and checks the registered
Document digest, stored text digest, and recovered value before returning text.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Document, SourceSegment

SPREADSHEET_SUFFIXES = frozenset({".xlsx", ".xlsm"})


class SourceSegmentIntegrityError(ValueError):
    """A segment cannot be proven against its registered source bytes."""


class SourceDocumentDigestMismatch(SourceSegmentIntegrityError):
    """The supplied bytes are not the segment's registered Document."""


class SourceSegmentDigestMismatch(SourceSegmentIntegrityError):
    """Stored or dereferenced segment text does not match its digest."""


class SourceSegmentLocatorMismatch(SourceSegmentIntegrityError):
    """A typed locator is invalid or no longer recovers the stored text."""


@dataclass(frozen=True)
class SpreadsheetSegment:
    """One populated workbook cell in deterministic workbook order."""

    ordinal: int
    sheet_name: str
    cell_range: str
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


def append_ingested_source_segments(
    session: Session, document: Document, path: Path | str
) -> tuple[SourceSegment, ...]:
    """Append the supported segments for registered source bytes, at most once."""

    original = Path(path)
    if not original.exists() or original.suffix.lower() not in SPREADSHEET_SUFFIXES:
        return ()
    _require_registered_bytes(document, original)
    existing = tuple(
        session.scalars(
            select(SourceSegment)
            .where(SourceSegment.document_id == document.id)
            .order_by(SourceSegment.ordinal)
        ).all()
    )
    if existing:
        return existing

    rows = tuple(
        SourceSegment(
            project_id=document.project_id,
            document_id=document.id,
            kind="spreadsheet_cell",
            exact_text=segment.exact_text,
            content_sha256=segment.content_sha256,
            ordinal=segment.ordinal,
            sheet_name=segment.sheet_name,
            cell_range=segment.cell_range,
        )
        for segment in spreadsheet_segments(original)
    )
    session.add_all(rows)
    return rows


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
    if segment.kind != "spreadsheet_cell":
        raise SourceSegmentLocatorMismatch(
            f"unsupported source segment kind {segment.kind!r}"
        )

    recovered = _dereference_spreadsheet_cell(
        original, sheet_name=segment.sheet_name, cell_range=segment.cell_range
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


def _exact_cell_text(value: object) -> str:
    """A native workbook value without citation-renderer whitespace folding."""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _text_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
