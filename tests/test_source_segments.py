"""Spreadsheet and Minutes Source Segments through ingest and replay seams.

The workbooks are written at test time so CI exercises real XLSX bytes without
depending on the separately fetched corpus.  The release rehearsal also reads a
fetched development-corpus workbook; these tests pin the same byte-to-segment
contract deterministically.
"""

from hashlib import sha256
import os
from pathlib import Path

from openpyxl import Workbook
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.db import Session, engine
from corridor.config import settings
from corridor.ingest import ingest_document
from corridor.models import Project, SourceSegment
from corridor.source_segments import (
    SourceSegmentDigestMismatch,
    dereference_source_segment,
)

from pdf_fixture_support import PdfFixture

REAL_WORKBOOK_SHA256 = (
    "3cd94fea058a3e61ac95ab1efd566e684e146f64ce6f25048d93cf9db55f83ba"
)
CORPUS_STORE = Path(
    os.environ.get("CORRIDOR_TEST_CORPUS_STORE", settings.corpus_store)
)
REAL_WORKBOOK = (
    CORPUS_STORE
    / REAL_WORKBOOK_SHA256[:2]
    / f"{REAL_WORKBOOK_SHA256}.xlsx"
)
REAL_MINUTES_SHA256 = (
    "ada13da24574950264d974f8a352fd07a3dba042ac2b37e825f8409730182eaf"
)
REAL_MINUTES = (
    CORPUS_STORE / REAL_MINUTES_SHA256[:2] / f"{REAL_MINUTES_SHA256}.pdf"
)
REAL_MINUTES_STATEMENT = (
    "Equistar to provide a chain of title on the ROW agreement that is in DOW’s name "
    "(Due \ndate of 01/2025)."
)
needs_corpus = pytest.mark.skipif(
    not REAL_WORKBOOK.exists(),
    reason="run `make corpus` to fetch the I-35 NEX South workbook",
)
needs_minutes_corpus = pytest.mark.skipif(
    not REAL_MINUTES.exists(),
    reason="run `make corpus` to fetch the SH99 Equistar meeting notes",
)

MINUTES_STATEMENT = "Equistar will submit the signed exhibit by March 2025."


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(
        slug="source-segment-test", name="Source Segment Test", is_synthetic=True
    )
    session.add(project)
    session.flush()
    return project


@pytest.fixture
def workbook(tmp_path):
    path = tmp_path / "utility-conflicts.xlsx"
    book = Workbook()
    first = book.active
    first.title = "Summary"
    first.append(["Project", "IH 45", "  exact spacing  "])
    first.append(["Blank stays absent", None])
    conflicts = book.create_sheet("Utility Conflicts")
    conflicts.append(["Conflict ID", "Owner", "Station"])
    conflicts.append(["UC-1", "CenterPoint", 1149])
    book.save(path)
    return path


def _ingest(session, project, workbook, tmp_path):
    return ingest_document(
        session,
        project_id=project.id,
        path=workbook,
        doc_type="matrix",
        images_dir=tmp_path / "images",
    )


def _minutes_pdf(tmp_path):
    path = tmp_path / "coordination-minutes.pdf"
    fixture = PdfFixture()
    fixture.add_page().text(
        (72, 72),
        "Meeting notes and attendance.\n"
        "Action Items:\n"
        f"1. {MINUTES_STATEMENT}\n"
        "Meeting Notes",
    )
    return fixture.save(path)


def test_workbook_ingest_appends_one_exact_segment_per_populated_cell(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)

    segments = session.scalars(
        select(SourceSegment)
        .where(SourceSegment.document_id == document.id)
        .order_by(SourceSegment.ordinal)
    ).all()

    assert [
        (segment.kind, segment.sheet_name, segment.cell_range, segment.exact_text)
        for segment in segments
    ] == [
        ("spreadsheet_cell", "Summary", "A1", "Project"),
        ("spreadsheet_cell", "Summary", "B1", "IH 45"),
        ("spreadsheet_cell", "Summary", "C1", "  exact spacing  "),
        ("spreadsheet_cell", "Summary", "A2", "Blank stays absent"),
        ("spreadsheet_cell", "Utility Conflicts", "A1", "Conflict ID"),
        ("spreadsheet_cell", "Utility Conflicts", "B1", "Owner"),
        ("spreadsheet_cell", "Utility Conflicts", "C1", "Station"),
        ("spreadsheet_cell", "Utility Conflicts", "A2", "UC-1"),
        ("spreadsheet_cell", "Utility Conflicts", "B2", "CenterPoint"),
        ("spreadsheet_cell", "Utility Conflicts", "C2", "1149"),
    ]
    assert [segment.ordinal for segment in segments] == list(
        range(1, len(segments) + 1)
    )
    assert len({(segment.kind, segment.sheet_name, segment.cell_range) for segment in segments}) == len(segments)
    assert all(
        segment.content_sha256 == sha256(segment.exact_text.encode("utf-8")).hexdigest()
        for segment in segments
    )


@needs_corpus
def test_real_dev_corpus_workbook_registers_and_replays_from_its_addressed_bytes(
    session, project, tmp_path
):
    assert REAL_WORKBOOK.parent.name == REAL_WORKBOOK_SHA256[:2]
    assert REAL_WORKBOOK.stem == REAL_WORKBOOK_SHA256
    document = ingest_document(
        session,
        project_id=project.id,
        path=REAL_WORKBOOK,
        doc_type="matrix",
        images_dir=tmp_path / "images",
        filename="I-35 NEX SOUTH Potential Utility Conflicts.xlsx",
        expected_sha256=REAL_WORKBOOK_SHA256,
    )
    segments = session.scalars(
        select(SourceSegment)
        .where(SourceSegment.document_id == document.id)
        .order_by(SourceSegment.ordinal)
    ).all()

    assert document.parse_status == "parsed"
    assert len(segments) == 1668
    assert len({(row.kind, row.sheet_name, row.cell_range) for row in segments}) == 1668
    assert (
        segments[0].sheet_name,
        segments[0].cell_range,
        dereference_source_segment(document, segments[0], REAL_WORKBOOK),
    ) == (
        "UCM-Conflict List",
        "A1",
        "TxDOT Utility Conflict Management (UCM) - Potential Utility Conflict List",
    )
    assert (
        segments[-1].sheet_name,
        segments[-1].cell_range,
        dereference_source_segment(document, segments[-1], REAL_WORKBOOK),
    ) == ("Drop-Down Lists", "A57", "Rejected")


def test_every_segment_replays_from_the_original_workbook_bytes(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)
    segments = session.scalars(
        select(SourceSegment)
        .where(SourceSegment.document_id == document.id)
        .order_by(SourceSegment.ordinal)
    ).all()

    assert [
        dereference_source_segment(document, segment, workbook) for segment in segments
    ] == [segment.exact_text for segment in segments]


def test_minutes_pdf_ingest_appends_non_overlapping_replayable_prose_spans(
    session, project, tmp_path
):
    path = _minutes_pdf(tmp_path)

    document = ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type="minutes",
        images_dir=tmp_path / "images",
    )
    segments = session.scalars(
        select(SourceSegment)
        .where(SourceSegment.document_id == document.id)
        .order_by(SourceSegment.ordinal)
    ).all()

    assert MINUTES_STATEMENT in [segment.exact_text for segment in segments]
    assert all(segment.kind == "prose_span" for segment in segments)
    assert all(
        segment.sheet_name is None
        and segment.cell_range is None
        and segment.page_no == 1
        and segment.start_offset is not None
        and segment.end_offset is not None
        and segment.start_offset < segment.end_offset
        for segment in segments
    )
    ordered_ranges = [
        (segment.start_offset, segment.end_offset) for segment in segments
    ]
    assert all(
        previous_end <= next_start
        for (_previous_start, previous_end), (next_start, _next_end) in zip(
            ordered_ranges, ordered_ranges[1:]
        )
    )
    assert [
        dereference_source_segment(document, segment, path) for segment in segments
    ] == [segment.exact_text for segment in segments]


@needs_minutes_corpus
def test_real_dev_corpus_minutes_replay_exact_statement_spans(
    session, project, tmp_path
):
    document = ingest_document(
        session,
        project_id=project.id,
        path=REAL_MINUTES,
        filename="Meeting Notes/Equistar/2025.02.12 GPB1 Equistar notes final.pdf",
        doc_type="minutes",
        images_dir=tmp_path / "images",
        expected_sha256=REAL_MINUTES_SHA256,
    )
    segments = session.scalars(
        select(SourceSegment)
        .where(
            SourceSegment.document_id == document.id,
            SourceSegment.kind == "prose_span",
        )
        .order_by(SourceSegment.ordinal)
    ).all()
    statement = next(
        segment for segment in segments if segment.exact_text == REAL_MINUTES_STATEMENT
    )

    assert len(segments) == 135
    assert statement.page_no == 2
    assert dereference_source_segment(document, statement, REAL_MINUTES) == (
        REAL_MINUTES_STATEMENT
    )


def test_replay_fails_closed_when_a_segment_digest_is_tampered(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)
    segment = session.scalars(
        select(SourceSegment).where(SourceSegment.document_id == document.id)
    ).first()
    segment.content_sha256 = "0" * 64

    with pytest.raises(SourceSegmentDigestMismatch, match="stored segment digest"):
        dereference_source_segment(document, segment, workbook)


def test_replay_refuses_bytes_other_than_the_registered_workbook(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)
    segment = session.scalars(
        select(SourceSegment).where(SourceSegment.document_id == document.id)
    ).first()
    other = tmp_path / "other.xlsx"
    other.write_bytes(workbook.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="registered Document digest"):
        dereference_source_segment(document, segment, other)


def test_database_refuses_overlapping_spreadsheet_cell_locators(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)
    first = session.scalars(
        select(SourceSegment).where(SourceSegment.document_id == document.id)
    ).first()
    session.add(
        SourceSegment(
            project_id=project.id,
            document_id=document.id,
            kind=first.kind,
            exact_text=first.exact_text,
            content_sha256=first.content_sha256,
            ordinal=100,
            sheet_name=first.sheet_name,
            cell_range=first.cell_range,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_database_refuses_overlapping_prose_span_locators(
    session, project, tmp_path
):
    path = _minutes_pdf(tmp_path)
    document = ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type="minutes",
        images_dir=tmp_path / "images",
    )
    first = session.scalars(
        select(SourceSegment)
        .where(
            SourceSegment.document_id == document.id,
            SourceSegment.kind == "prose_span",
        )
        .order_by(SourceSegment.ordinal)
    ).first()
    assert first.start_offset is not None
    assert first.end_offset is not None
    overlapping_text = first.exact_text[1:]
    session.add(
        SourceSegment(
            project_id=project.id,
            document_id=document.id,
            kind="prose_span",
            exact_text=overlapping_text,
            content_sha256=sha256(overlapping_text.encode()).hexdigest(),
            ordinal=100,
            sheet_name=None,
            cell_range=None,
            page_no=first.page_no,
            start_offset=first.start_offset + 1,
            end_offset=first.end_offset,
        )
    )

    with pytest.raises(DBAPIError, match="prose source segments cannot overlap"):
        session.flush()


def test_database_refuses_a_segment_scoped_to_another_project(
    session, project, workbook, tmp_path
):
    document = _ingest(session, project, workbook, tmp_path)
    other = Project(slug="other-segment-project", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    session.add(
        SourceSegment(
            project_id=other.id,
            document_id=document.id,
            kind="spreadsheet_cell",
            exact_text="foreign",
            content_sha256=sha256(b"foreign").hexdigest(),
            ordinal=100,
            sheet_name="Summary",
            cell_range="C1",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_source_segments_have_no_update_or_delete_path(
    session, project, workbook, tmp_path, operation
):
    document = _ingest(session, project, workbook, tmp_path)
    segment = session.scalars(
        select(SourceSegment).where(SourceSegment.document_id == document.id)
    ).first()
    if operation == "update":
        segment.exact_text = "rewritten"
    else:
        session.delete(segment)

    with pytest.raises(DBAPIError, match="source segments are append-only"):
        session.flush()
