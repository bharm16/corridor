"""A Recorded Verbal Statement's exact words are a self-certifying segment (#451)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.models import Project, SourceSegment, ExternalPartyStatement
from corridor.source_segments import (
    SourceSegmentDigestMismatch,
    SourceSegmentLocatorMismatch,
    recorded_verbal_statement_segment,
    replay_recorded_verbal_statement,
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
def statement(session):
    project = Project(slug="verbal-segment", name="Verbal Segment", is_synthetic=True)
    session.add(project)
    session.flush()
    event = ExternalPartyStatement(
        event_type="commitment",
        project_id=project.id,
        source_kind="verbal",
        description="Equistar will submit the signed exhibit by March 2025.",
        created_by="local:dana-fields",
    )
    session.add(event)
    session.flush()
    return project, event


def test_verbal_segment_persists_and_replays_from_its_own_words(session, statement):
    project, event = statement
    words = "Equistar will submit the signed exhibit by March 2025."
    segment = recorded_verbal_statement_segment(
        project_id=project.id, statement_id=event.id, exact_text=words, ordinal=1
    )
    session.add(segment)
    session.flush()

    assert segment.kind == "recorded_verbal_statement"
    assert segment.document_id is None
    assert segment.statement_id == event.id
    assert replay_recorded_verbal_statement(segment) == words


def test_a_tampered_verbal_digest_fails_closed(session, statement):
    project, event = statement
    segment = recorded_verbal_statement_segment(
        project_id=project.id, statement_id=event.id, exact_text="Original words."
    )
    segment.exact_text = "Altered words."
    with pytest.raises(SourceSegmentDigestMismatch):
        replay_recorded_verbal_statement(segment)


def test_replay_refuses_a_non_verbal_segment(session, statement):
    project, _ = statement
    other = SourceSegment(
        project_id=project.id,
        document_id=None,
        statement_id=None,
        kind="spreadsheet_cell",
        exact_text="UC-1",
        content_sha256="a" * 64,
        ordinal=1,
        sheet_name="Conflicts",
        cell_range="A2",
    )
    with pytest.raises(SourceSegmentLocatorMismatch):
        replay_recorded_verbal_statement(other)


def test_the_locator_check_rejects_a_verbal_segment_with_a_document(session, statement):
    project, event = statement
    bad = SourceSegment(
        project_id=project.id,
        document_id=999,  # a verbal segment must have no document
        statement_id=event.id,
        kind="recorded_verbal_statement",
        exact_text="Words.",
        content_sha256="b" * 64,
        ordinal=1,
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_wording_segment_per_statement(session, statement):
    project, event = statement
    session.add(
        recorded_verbal_statement_segment(
            project_id=project.id, statement_id=event.id, exact_text="First words."
        )
    )
    session.flush()
    session.add(
        recorded_verbal_statement_segment(
            project_id=project.id, statement_id=event.id, exact_text="Second words.",
            ordinal=2,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
