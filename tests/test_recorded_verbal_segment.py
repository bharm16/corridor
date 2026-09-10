"""A Recorded Verbal Statement's exact words hang from its own origin (#512).

#451 gave the segment its self-certifying digest but pointed it at a legacy
``dependency_events`` row, so the evidence spine depended on a Project Record
aggregate and the Fact identity digest carried a legacy key.  ADR-0081 stage 1
replaces that with ``recorded_verbal_origins`` — the recorder's own attestation
— and leaves the legacy identity in one temporary compatibility mapping.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.models import (
    ExternalPartyStatement,
    Project,
    RecordedVerbalOrigin,
    RecordedVerbalOriginStatement,
    SourceSegment,
)
from corridor.source_append import append_recorded_verbal_origin
from corridor.source_segments import (
    SourceSegmentDigestMismatch,
    SourceSegmentLocatorMismatch,
    recorded_verbal_statement_segment,
    replay_recorded_verbal_statement,
)

WORDS = "Equistar will submit the signed exhibit by March 2025."
# Supplied logical times. Nothing in this module reads a wall clock, so a
# recorded time can be asserted exactly rather than approximately.
RECORDED_AT = datetime(2025, 3, 3, 14, 30, tzinfo=timezone.utc)
CONVERSATION_DATE = date(2025, 3, 3)


@pytest.fixture
def statement(session):
    project = Project(slug="verbal-segment", name="Verbal Segment", is_synthetic=True)
    session.add(project)
    session.flush()
    event = ExternalPartyStatement(
        event_type="commitment",
        project_id=project.id,
        source_kind="verbal",
        description=WORDS,
        created_by="local:dana-fields",
    )
    session.add(event)
    session.flush()
    return project, event


def _origin(session, project, *, exact_text=WORDS, statement_id=None, corrects=None):
    return append_recorded_verbal_origin(
        session,
        project_id=project.id,
        recorded_by="local:dana-fields",
        recorded_at=RECORDED_AT,
        conversation_date=CONVERSATION_DATE,
        exact_text=exact_text,
        content_sha256=sha256(exact_text.encode("utf-8")).hexdigest(),
        corrects_origin_id=corrects,
        legacy_statement_id=statement_id,
    )


def test_the_origin_carries_the_whole_attestation(session, statement):
    project, event = statement

    origin = _origin(session, project, statement_id=event.id)

    assert origin.project_id == project.id
    assert origin.recorded_by == "local:dana-fields"
    assert origin.recorded_at == RECORDED_AT
    assert origin.conversation_date == CONVERSATION_DATE
    assert origin.exact_text == WORDS
    assert origin.content_sha256 == sha256(WORDS.encode("utf-8")).hexdigest()
    assert origin.corrects_origin_id is None
    assert origin.id > 0


def test_the_legacy_statement_lives_only_in_the_compatibility_mapping(
    session, statement
):
    """No target column names a legacy statement any more (ADR-0081 stage 1)."""

    project, event = statement
    origin = _origin(session, project, statement_id=event.id)
    segment = recorded_verbal_statement_segment(
        project_id=project.id,
        recorded_verbal_origin_id=origin.id,
        exact_text=WORDS,
    )
    session.add(segment)
    session.flush()

    mapping = session.get(RecordedVerbalOriginStatement, origin.id)
    assert (mapping.statement_id, mapping.project_id) == (event.id, project.id)
    assert segment.recorded_verbal_origin_id == origin.id
    assert "statement_id" not in SourceSegment.__table__.c

    # The spine's own tables name no legacy statement; the mapping is the one
    # place the key survives, and it retires with the dual-write.
    legacy_keys = session.execute(
        text(
            "select child.relname as table_name, col.attname as column_name "
            "  from pg_constraint constraint_row "
            "  join pg_class child on child.oid = constraint_row.conrelid "
            "  join pg_class parent on parent.oid = constraint_row.confrelid "
            "  join unnest(constraint_row.conkey) as fk(attnum) on true "
            "  join pg_attribute col on col.attrelid = constraint_row.conrelid "
            "   and col.attnum = fk.attnum "
            " where constraint_row.contype = 'f' "
            "   and parent.relname = 'dependency_events' "
            "   and (child.relname like 'source_%' or child.relname like 'fact%' "
            "        or child.relname like 'recorded_verbal%') "
            " order by 1, 2"
        )
    ).all()
    assert [(row.table_name, row.column_name) for row in legacy_keys] == [
        ("recorded_verbal_origin_statements", "statement_id")
    ]


def test_verbal_segment_persists_and_replays_from_its_own_words(session, statement):
    project, event = statement
    origin = _origin(session, project, statement_id=event.id)
    segment = recorded_verbal_statement_segment(
        project_id=project.id,
        recorded_verbal_origin_id=origin.id,
        exact_text=WORDS,
        ordinal=1,
    )
    session.add(segment)
    session.flush()

    assert segment.kind == "recorded_verbal_statement"
    assert segment.document_id is None
    assert segment.recorded_verbal_origin_id == origin.id
    assert replay_recorded_verbal_statement(segment) == WORDS


def test_a_tampered_verbal_digest_fails_closed(session, statement):
    project, event = statement
    origin = _origin(session, project, exact_text="Original words.", statement_id=event.id)
    segment = recorded_verbal_statement_segment(
        project_id=project.id,
        recorded_verbal_origin_id=origin.id,
        exact_text="Original words.",
    )
    segment.exact_text = "Altered words."
    with pytest.raises(SourceSegmentDigestMismatch):
        replay_recorded_verbal_statement(segment)


def test_replay_refuses_a_non_verbal_segment(session, statement):
    project, _ = statement
    other = SourceSegment(
        project_id=project.id,
        document_id=None,
        recorded_verbal_origin_id=None,
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
    origin = _origin(session, project, statement_id=event.id)
    bad = SourceSegment(
        project_id=project.id,
        document_id=999,  # a verbal segment must have no document
        recorded_verbal_origin_id=origin.id,
        kind="recorded_verbal_statement",
        exact_text="Words.",
        content_sha256="b" * 64,
        ordinal=1,
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_wording_segment_per_origin(session, statement):
    project, event = statement
    origin = _origin(session, project, statement_id=event.id)
    session.add(
        recorded_verbal_statement_segment(
            project_id=project.id,
            recorded_verbal_origin_id=origin.id,
            exact_text="First words.",
        )
    )
    session.flush()
    session.add(
        recorded_verbal_statement_segment(
            project_id=project.id,
            recorded_verbal_origin_id=origin.id,
            exact_text="Second words.",
            ordinal=2,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_replayed_recording_converges_on_the_one_origin(session, statement):
    """The dual-write's legacy statement is the replay key, and it is one-to-one."""

    project, event = statement

    first = _origin(session, project, statement_id=event.id)
    again = _origin(session, project, statement_id=event.id)

    assert again.id == first.id
    mappings = session.scalars(
        select(RecordedVerbalOriginStatement.origin_id).where(
            RecordedVerbalOriginStatement.statement_id == event.id
        )
    ).all()
    assert list(mappings) == [first.id]


def test_a_replay_with_different_words_is_refused(session, statement):
    project, event = statement
    _origin(session, project, statement_id=event.id)

    with pytest.raises(DBAPIError, match="already bound to different content"):
        _origin(session, project, exact_text="Other words.", statement_id=event.id)


def test_an_origin_digest_must_certify_its_own_words(session, statement):
    project, _ = statement

    with pytest.raises(DBAPIError, match="digest does not match its exact words"):
        append_recorded_verbal_origin(
            session,
            project_id=project.id,
            recorded_by="local:dana-fields",
            recorded_at=RECORDED_AT,
            conversation_date=CONVERSATION_DATE,
            exact_text=WORDS,
            content_sha256=sha256(b"different words").hexdigest(),
        )


def test_a_statement_from_another_project_is_refused(session, statement):
    project, _ = statement
    other = Project(slug="verbal-other", name="Verbal Other", is_synthetic=True)
    session.add(other)
    session.flush()
    elsewhere = ExternalPartyStatement(
        event_type="commitment",
        project_id=other.id,
        source_kind="verbal",
        description=WORDS,
        created_by="local:dana-fields",
    )
    session.add(elsewhere)
    session.flush()

    with pytest.raises(DBAPIError, match="statement is outside its project"):
        _origin(session, project, statement_id=elsewhere.id)


def test_a_correction_chains_to_exactly_one_predecessor(session, statement):
    """A re-attestation corrects one origin, and only one successor may claim it."""

    project, event = statement
    original = _origin(session, project, statement_id=event.id)

    corrected = _origin(
        session, project, exact_text="Equistar said March 2025.", corrects=original.id
    )
    assert corrected.corrects_origin_id == original.id

    session.add(
        RecordedVerbalOrigin(
            project_id=project.id,
            recorded_by="local:dana-fields",
            recorded_at=RECORDED_AT,
            conversation_date=CONVERSATION_DATE,
            exact_text="A second correction of the same words.",
            content_sha256=sha256(
                "A second correction of the same words.".encode("utf-8")
            ).hexdigest(),
            corrects_origin_id=original.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_correction_of_another_projects_origin_is_refused(session, statement):
    project, _ = statement
    other = Project(slug="verbal-third", name="Verbal Third", is_synthetic=True)
    session.add(other)
    session.flush()
    elsewhere = _origin(session, other)

    with pytest.raises(DBAPIError, match="outside its project"):
        _origin(session, project, corrects=elsewhere.id)
