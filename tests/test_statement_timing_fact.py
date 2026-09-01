"""A statement_timing Fact carries a verbal's stated timing set (#451 stage 1c).

The rails: a Recorded Verbal Statement's exact words are a document-less segment
(stage 1b); its stated timing becomes a document-less, human-gated
``statement_timing`` Fact whose members live in a typed satellite; and the Fact
is settled on the spine through ``record_human_fact_decision`` (stage 1a). The
Fact self-certifies — there are no external bytes to replay (ADR-0033/0074).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.db import Session, engine
from corridor.facts import (
    FactReplayMismatch,
    FactValidationError,
    StatementTimingFactValue,
    append_recorded_statement_timing_fact,
    replay_recorded_statement_timing_fact,
)
from corridor.fact_decisions import record_human_fact_decision
from corridor.models import (
    Document,
    ExternalPartyStatement,
    Fact,
    FactDecision,
    FactStatementTiming,
    Project,
    ProjectRecordRevision,
)
from corridor.principals import HumanPrincipal
from corridor.source_segments import recorded_verbal_statement_segment
from corridor.statement_values import StatementTiming


RECORDER = HumanPrincipal("local:dana-fields")


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
def verbal(session):
    project = Project(slug="verbal-timing", name="Verbal Timing", is_synthetic=True)
    session.add(project)
    session.flush()
    statement = ExternalPartyStatement(
        event_type="commitment",
        project_id=project.id,
        source_kind="verbal",
        description="Equistar will submit the signed exhibit by March 2025.",
        created_by="local:dana-fields",
    )
    session.add(statement)
    session.flush()
    segment = recorded_verbal_statement_segment(
        project_id=project.id,
        statement_id=statement.id,
        exact_text="Equistar will submit the signed exhibit by March 2025.",
    )
    session.add(segment)
    session.flush()
    return project, statement, segment


def _subject_key(statement) -> str:
    return f"statement:{statement.id}"


def test_statement_timing_fact_is_document_less_and_replays(session, verbal):
    project, statement, segment = verbal
    fact = append_recorded_statement_timing_fact(
        session,
        segment=segment,
        subject_key=_subject_key(statement),
        timings=(("new", StatementTiming.day("March 3, 2025", date(2025, 3, 3))),),
        recorded_by="local:dana-fields",
    )

    assert fact.fact_type == "statement_timing"
    assert fact.subject_kind == "statement_candidate"
    assert fact.document_id is None
    assert fact.extraction_run_id is None
    assert fact.transformation == "typed_statement_timing_v1"

    rows = session.scalars(
        select(FactStatementTiming).where(FactStatementTiming.fact_id == fact.id)
    ).all()
    assert [(row.timing_role, row.precision) for row in rows] == [("new", "day")]

    replayed = replay_recorded_statement_timing_fact(session, fact)
    assert isinstance(replayed, StatementTimingFactValue)
    assert replayed.timings[0][0] == "new"
    assert replayed.timings[0][1].precision == "day"
    assert replayed.timings[0][1].start_date == date(2025, 3, 3)


def test_a_change_of_promise_carries_previous_and_new(session, verbal):
    project, statement, segment = verbal
    fact = append_recorded_statement_timing_fact(
        session,
        segment=segment,
        subject_key=_subject_key(statement),
        timings=(
            ("previous", StatementTiming.month("March 2025", 2025, 3)),
            ("new", StatementTiming.approximate("sometime this summer")),
        ),
        recorded_by="local:dana-fields",
    )

    replayed = replay_recorded_statement_timing_fact(session, fact)
    by_role = {role: timing for role, timing in replayed.timings}
    assert by_role["previous"].precision == "month"
    assert by_role["previous"].start_date == date(2025, 3, 1)
    assert by_role["previous"].end_date == date(2025, 3, 31)
    assert by_role["new"].precision == "approximate"
    assert by_role["new"].start_date is None
    assert by_role["new"].text == "sometime this summer"


def test_a_tampered_digest_fails_closed(session, verbal):
    project, statement, segment = verbal
    fact = append_recorded_statement_timing_fact(
        session,
        segment=segment,
        subject_key=_subject_key(statement),
        timings=(("new", StatementTiming.day("March 3, 2025", date(2025, 3, 3))),),
        recorded_by="local:dana-fields",
    )
    fact.content_sha256 = "0" * 64
    with session.no_autoflush:
        with pytest.raises(FactReplayMismatch):
            replay_recorded_statement_timing_fact(session, fact)


def test_the_timing_satellite_is_append_only(session, verbal):
    project, statement, segment = verbal
    fact = append_recorded_statement_timing_fact(
        session,
        segment=segment,
        subject_key=_subject_key(statement),
        timings=(("new", StatementTiming.day("March 3, 2025", date(2025, 3, 3))),),
        recorded_by="local:dana-fields",
    )
    row = session.scalars(
        select(FactStatementTiming).where(FactStatementTiming.fact_id == fact.id)
    ).one()
    row.precision = "approximate"
    with pytest.raises(DBAPIError, match="satellites are append-only"):
        session.flush()


def test_a_previous_without_a_new_is_refused(session, verbal):
    project, statement, segment = verbal
    with pytest.raises(FactValidationError):
        append_recorded_statement_timing_fact(
            session,
            segment=segment,
            subject_key=_subject_key(statement),
            timings=(
                ("previous", StatementTiming.month("March 2025", 2025, 3)),
            ),
            recorded_by="local:dana-fields",
        )


def test_statement_timing_must_be_document_less(session, verbal):
    project, statement, segment = verbal
    document = Document(
        project_id=project.id,
        sha256="d" * 64,
        filename="minutes.pdf",
        doc_type="minutes",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    bad = Fact(
        project_id=project.id,
        document_id=document.id,  # statement_timing may not bind a Document
        extraction_run_id=None,
        fact_type="statement_timing",
        subject_kind="statement_candidate",
        subject_key=_subject_key(statement),
        transformation="typed_statement_timing_v1",
        recorded_by="local:dana-fields",
        content_sha256="e" * 64,
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_document_bound_type_may_not_be_document_less(session, verbal):
    project, statement, _segment = verbal
    bad = Fact(
        project_id=project.id,
        document_id=None,  # only verbal Fact types may be document-less
        extraction_run_id=None,
        fact_type="station_from",
        subject_kind="source_row",
        subject_key="Sheet1!3",
        text_value="1149+00",
        transformation="trim_cell_text_v1",
        recorded_by="local:dana-fields",
        content_sha256="f" * 64,
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()


def test_the_rail_settles_a_timing_through_a_human_decision(session, verbal):
    project, statement, segment = verbal
    fact = append_recorded_statement_timing_fact(
        session,
        segment=segment,
        subject_key=_subject_key(statement),
        timings=(("new", StatementTiming.day("March 3, 2025", date(2025, 3, 3))),),
        recorded_by="local:dana-fields",
    )

    result = record_human_fact_decision(
        session,
        fact,
        principal=RECORDER,
        command_type="record_verbal_statement",
        idempotency_key="verbal-timing-1",
    )

    assert result.created is True
    assert result.revision.human_principal == "local:dana-fields"
    assert result.revision.released_policy is None
    assert result.decision.fact_id == fact.id
    assert result.decision.fact_type == "statement_timing"
    assert result.decision.superseded_by is None
    session.expire_all()
    revision = session.get(ProjectRecordRevision, result.revision.id)
    assert revision.command_type == "record_verbal_statement"
    decision = session.get(FactDecision, result.decision.id)
    assert decision.subject_key == _subject_key(statement)
