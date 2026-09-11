"""One cited passage, read back at its place in the source it came from (#831).

Every surface that prints a citation — a Review row, a Record value, a
follow-up bundle, the source register — prints a filename, a locator and the
quoted words, and there it stops.  A coordinator who wants to know what the
sentence *above* the quoted one said, or what the cell to the left of the
cited cell holds, has had to leave the product, find the file, and open it.
The pilot measures that as manual reconstruction time
(``docs/pilot-success-criteria.md``), so the surrounding context is the
product, not decoration.

This module is the reading behind that view.  It answers three questions about
one ``source_segments`` row and keeps them apart, because collapsing any two of
them tells a customer something no reader ever checked:

1.  **Was the passage there?**  That is the Source Passage Check, and this
    module does not re-derive it.  It calls ``locator_validation`` and prints
    ``presentation.source_passage_check_label`` — *Found at cited location*,
    *Not found at cited location*, *No cited location recorded*, *Cited
    location cannot be re-read* (ADR-0082, ADR-0094).

2.  **Could we even look?**  The check needs the registered bytes.  When the
    content-addressed store cannot hand them over — no object for the
    registered digest, a digest the store itself refuses, an unreachable
    backend, a staged file that vanished — nothing was opened, so there is no
    Source Passage Check to report at all.  ``retrieval_failure`` carries that
    instead, ``passage_check_status`` is ``None``, and the failure is logged as
    a retrieval failure.  ``accepted_statement_reading`` answers the same
    situation with ``not_checked``, whose customer words are *No cited location
    recorded* — which is false here, because the location is recorded and is
    exactly what could not be reached.  That is the mislabel this view exists
    to avoid, so the state lives outside the check's vocabulary rather than
    inside it.

3.  **What is around it?**  Nearby context comes from the neighbouring
    ``source_segments`` of the same rendition, never from ``doc_pages``:
    ADR-0068 made the segment the owner of cited text and the page text a
    rebuildable artifact, and #680 revoked ``doc_pages`` from the web
    capability outright, so a boundary-admitted route may not read it.  For a
    page the neighbours are the passages before and after the cited one on that
    page, in reading order; for a workbook they are the populated cells around
    the cited cell, laid out as the rows and columns of the sheet.

The context is therefore **Corridor's reading of the source, not the source**.
The view says so, and offers the registered bytes themselves beside it, so the
original and what was derived from it are never the same object on the screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.locator_validation import (
    recorded_verbal_statement_locator_validation,
    source_segment_locator_validation,
)
from corridor.models import Document, SourceSegment
from corridor.object_storage import StorageError
from corridor.presentation import source_passage_check_label
from corridor.source_segments import source_segment_locator_words
from corridor.storage import stored_file
from corridor.telemetry import log_event


_LOG = logging.getLogger("corridor.source_passage")

#: The operational event name an operator greps for. One name for every way
#: the registered bytes failed to arrive, with the class of the failure as a
#: field rather than as three event names nobody can group.
RETRIEVAL_FAILURE_EVENT = "source_bytes_retrieval_failed"

#: How many neighbouring passages of a page are shown on each side of the
#: cited one. Enough to read the sentences around it; bounded so a page with
#: hundreds of spans does not become the screen.
PAGE_NEIGHBOURS = 6

#: How far around a cited workbook cell the sheet window reaches.
SHEET_ROWS = 2
SHEET_COLUMNS = 3

#: Workbook segmentation walks each sheet row by row, so ordinals next to the
#: cited one are cells next to it. This bounds the read; the row and column
#: windows above then decide what is actually shown.
_SHEET_ORDINALS = 80

_CELL = re.compile(r"^(?P<column>[A-Z]+)(?P<row>[1-9][0-9]*)$")


class SourcePassageNotFound(LookupError):
    """No such cited passage in this project."""


@dataclass(frozen=True)
class RetrievalFailure:
    """The registered bytes did not arrive, so no locator was followed.

    ``failure`` is the class of the refusal, which is what an operator groups
    by; ``detail`` is the store's own sentence about this one attempt.
    """

    failure: str
    detail: str


@dataclass(frozen=True)
class RevisionIdentity:
    """Which issued version of which Document the passage belongs to."""

    document_id: int
    filename: str
    registry_id: str | None
    doc_type: str
    doc_date: date | None
    pages: int | None
    superseded_by_document_id: int | None
    superseded_by_filename: str | None
    superseded_on: date | None

    @property
    def is_superseded(self) -> bool:
        return self.superseded_by_document_id is not None


@dataclass(frozen=True)
class RenditionIdentity:
    """Which file representation the passage was read out of.

    ``cited_sha256`` is the rendition digest recorded on the segment itself.
    A segment written before that column was filled carries ``None``, which is
    not a disagreement; a segment that carries a digest other than the one the
    Document is registered under today is, and the view says so rather than
    presenting one file as the other.
    """

    registered_sha256: str
    cited_sha256: str | None
    file_format: str

    @property
    def cites_the_registered_file(self) -> bool:
        return self.cited_sha256 in (None, self.registered_sha256)


@dataclass(frozen=True)
class NeighbouringPassage:
    """One passage beside the cited one, in the source's own reading order."""

    source_segment_id: int
    locator: str
    exact_text: str
    is_cited: bool


@dataclass(frozen=True)
class SheetCellView:
    """One populated workbook cell in the window around the cited one."""

    address: str
    exact_text: str
    is_cited: bool


@dataclass(frozen=True)
class SheetRowView:
    """One sheet row of the window, aligned with ``SourcePassageView.columns``."""

    row_number: int
    cells: tuple[SheetCellView | None, ...]


@dataclass(frozen=True)
class SourcePassageView:
    """One citation opened at its cited place, with what surrounds it.

    Exactly one of ``passage_check_status`` and ``retrieval_failure`` is set.
    A caller that reads the status without asking whether the bytes arrived
    would report "No cited location recorded" for a storage outage, which is
    the confusion this separation exists to prevent.
    """

    source_segment_id: int
    kind: str
    locator: str
    exact_text: str
    content_sha256: str
    revision: RevisionIdentity | None
    rendition: RenditionIdentity | None
    passage_check_status: str | None
    passage_check_reason: str | None
    retrieval_failure: RetrievalFailure | None
    page_no: int | None
    sheet_name: str | None
    cited_cell: str | None
    passages: tuple[NeighbouringPassage, ...]
    columns: tuple[str, ...]
    rows: tuple[SheetRowView, ...]

    @property
    def passage_check_state(self) -> str | None:
        """The Source Passage Check in its adopted customer words, or none."""

        if self.passage_check_status is None:
            return None
        return source_passage_check_label(self.passage_check_status)

    @property
    def source_bytes_unavailable(self) -> bool:
        """Whether the registered bytes could not be retrieved at all."""

        return self.retrieval_failure is not None


def read_source_passage(
    session: Session, *, project_id: int, segment_id: int
) -> SourcePassageView:
    """Open one project's cited passage at its place in the source.

    A segment of another project is refused exactly as a missing one is, so a
    guessed identifier cannot confirm that another customer's citation exists.
    """

    segment = session.get(SourceSegment, segment_id)
    if segment is None or segment.project_id != project_id:
        raise SourcePassageNotFound(f"no source passage {segment_id} in this project")
    document = (
        session.get(Document, segment.document_id)
        if segment.document_id is not None
        else None
    )
    if document is not None and document.project_id != project_id:
        raise SourcePassageNotFound(f"no source passage {segment_id} in this project")

    check, retrieval_failure = _passage_check(segment, document, project_id=project_id)
    columns, rows = _sheet_window(session, segment)
    return SourcePassageView(
        source_segment_id=int(segment.id),
        kind=segment.kind,
        locator=source_segment_locator_words(segment),
        exact_text=segment.exact_text,
        content_sha256=segment.content_sha256,
        revision=_revision(session, document),
        rendition=_rendition(segment, document),
        passage_check_status=check[0] if check else None,
        passage_check_reason=check[1] if check else None,
        retrieval_failure=retrieval_failure,
        page_no=segment.page_no,
        sheet_name=segment.sheet_name,
        cited_cell=segment.cell_range,
        passages=_neighbouring_passages(session, segment),
        columns=columns,
        rows=rows,
    )


def _passage_check(
    segment: SourceSegment, document: Document | None, *, project_id: int
) -> tuple[tuple[str, str | None] | None, RetrievalFailure | None]:
    """The Source Passage Check, or the reason nothing could be looked at.

    The order matters. Every refusal that means *the bytes did not arrive*
    becomes a retrieval failure and stops; only a check that actually ran
    produces a status. Nothing here invents a status for an absent file.
    """

    if document is None:
        # A Recorded Verbal Statement is a recorder's attestation, not a
        # document proposal (ADR-0082): there are no bytes, and its words are
        # replayed against their own digest.
        check = recorded_verbal_statement_locator_validation(segment)
        return (check.status, check.reason), None
    try:
        path = stored_file(document)
    except (StorageError, OSError) as error:
        return None, _retrieval_failure(segment, document, error, project_id=project_id)
    if path is None:
        return None, _retrieval_failure(
            segment,
            document,
            None,
            project_id=project_id,
            failure="ObjectMissing",
            detail=(
                "the content-addressed store holds no object for this "
                "document's registered digest"
            ),
        )
    try:
        check = source_segment_locator_validation(document, segment, path)
    except (StorageError, OSError) as error:
        return None, _retrieval_failure(segment, document, error, project_id=project_id)
    return (check.status, check.reason), None


def log_source_bytes_retrieval_failure(
    *,
    project_id: int,
    document_id: int,
    registered_sha256: str,
    failure: str,
    detail: str,
    source_segment_id: int | None = None,
) -> None:
    """Log one failure to retrieve registered bytes, under one event name.

    The download route reaches the same storage without a segment, so the
    event lives here rather than at each call site: an operator greps one name
    and groups by the class of the refusal. The line carries identifiers and
    that class, never the customer's words — what it has to lead to is the
    object, not the passage.
    """

    log_event(
        _LOG,
        RETRIEVAL_FAILURE_EVENT,
        level=logging.ERROR,
        project_id=project_id,
        document_id=document_id,
        source_segment_id=source_segment_id,
        registered_sha256=registered_sha256,
        failure=failure,
        detail=detail,
    )


def _retrieval_failure(
    segment: SourceSegment,
    document: Document,
    error: BaseException | None,
    *,
    project_id: int,
    failure: str | None = None,
    detail: str | None = None,
) -> RetrievalFailure:
    """Record one failure to retrieve this passage's registered bytes."""

    named = failure or type(error).__name__
    said = detail if detail is not None else str(error)
    log_source_bytes_retrieval_failure(
        project_id=project_id,
        document_id=int(document.id),
        registered_sha256=document.sha256,
        failure=named,
        detail=said,
        source_segment_id=int(segment.id),
    )
    return RetrievalFailure(failure=named, detail=said)


def _revision(session: Session, document: Document | None) -> RevisionIdentity | None:
    """The Document Revision the passage belongs to, and what replaced it."""

    if document is None:
        return None
    successor = (
        session.get(Document, document.superseded_by)
        if document.superseded_by is not None
        else None
    )
    return RevisionIdentity(
        document_id=int(document.id),
        filename=document.filename,
        registry_id=document.registry_id,
        doc_type=document.doc_type,
        doc_date=document.doc_date,
        pages=document.pages,
        superseded_by_document_id=document.superseded_by,
        superseded_by_filename=successor.filename if successor is not None else None,
        superseded_on=document.superseded_on,
    )


def _rendition(
    segment: SourceSegment, document: Document | None
) -> RenditionIdentity | None:
    """Which file this passage was read from, and its format."""

    if document is None:
        return None
    _, _, suffix = document.filename.rpartition(".")
    return RenditionIdentity(
        registered_sha256=document.sha256,
        cited_sha256=segment.rendition_sha256,
        file_format=suffix.upper() if suffix and "." in document.filename else "",
    )


def _neighbouring_passages(
    session: Session, segment: SourceSegment
) -> tuple[NeighbouringPassage, ...]:
    """The passages around this one, in the order the source presents them.

    Same Document, same segment kind, same reading, and the same page where the
    kind has one — a window of a different page or a different reading would be
    context from somewhere else.
    """

    if segment.kind == "spreadsheet_cell" or segment.document_id is None:
        return ()
    query = select(SourceSegment).where(
        SourceSegment.project_id == segment.project_id,
        SourceSegment.document_id == segment.document_id,
        SourceSegment.kind == segment.kind,
        SourceSegment.reading_sha256.is_not_distinct_from(segment.reading_sha256),
    )
    if segment.page_no is not None:
        query = query.where(SourceSegment.page_no == segment.page_no)
    found = list(session.scalars(query.order_by(SourceSegment.ordinal)))
    positions = [index for index, row in enumerate(found) if row.id == segment.id]
    if not positions:
        return ()
    cited = positions[0]
    window = found[max(0, cited - PAGE_NEIGHBOURS) : cited + PAGE_NEIGHBOURS + 1]
    return tuple(
        NeighbouringPassage(
            source_segment_id=int(row.id),
            locator=source_segment_locator_words(row),
            exact_text=row.exact_text,
            is_cited=row.id == segment.id,
        )
        for row in window
    )


def _sheet_window(
    session: Session, segment: SourceSegment
) -> tuple[tuple[str, ...], tuple[SheetRowView, ...]]:
    """The populated cells around a cited workbook cell, as sheet rows."""

    if segment.kind != "spreadsheet_cell" or segment.cell_range is None:
        return (), ()
    cited = _CELL.match(segment.cell_range)
    if cited is None:
        return (), ()
    cited_row = int(cited.group("row"))
    cited_column = _column_index(cited.group("column"))
    found = session.scalars(
        select(SourceSegment)
        .where(
            SourceSegment.project_id == segment.project_id,
            SourceSegment.document_id == segment.document_id,
            SourceSegment.kind == "spreadsheet_cell",
            SourceSegment.sheet_name == segment.sheet_name,
            SourceSegment.ordinal >= segment.ordinal - _SHEET_ORDINALS,
            SourceSegment.ordinal <= segment.ordinal + _SHEET_ORDINALS,
        )
        .order_by(SourceSegment.ordinal)
    )
    cells: dict[tuple[int, str], SheetCellView] = {}
    columns: set[str] = set()
    for row in found:
        address = _CELL.match(row.cell_range or "")
        if address is None:
            continue
        number = int(address.group("row"))
        letters = address.group("column")
        if abs(number - cited_row) > SHEET_ROWS:
            continue
        if abs(_column_index(letters) - cited_column) > SHEET_COLUMNS:
            continue
        columns.add(letters)
        cells[number, letters] = SheetCellView(
            address=row.cell_range,
            exact_text=row.exact_text,
            is_cited=row.id == segment.id,
        )
    if not cells:
        return (), ()
    ordered = tuple(sorted(columns, key=_column_index))
    rows = tuple(
        SheetRowView(
            row_number=number,
            cells=tuple(cells.get((number, letters)) for letters in ordered),
        )
        for number in sorted({number for number, _ in cells})
    )
    return ordered, rows


def _column_index(letters: str) -> int:
    """A workbook column's position, so ``Z`` sorts before ``AA``."""

    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index
