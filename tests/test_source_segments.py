"""Spreadsheet Source Segments through the public ingest and replay seams.

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
needs_corpus = pytest.mark.skipif(
    not REAL_WORKBOOK.exists(),
    reason="run `make corpus` to fetch the I-35 NEX South workbook",
)


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
