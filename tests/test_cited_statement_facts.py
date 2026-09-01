"""A Meeting Notes passage may back the same statement facts (#451 stage 3).

Fact types are source-neutral (ADR-0074): statement_wording, statement_timing,
and applies_to can be supported by a prose_span passage, not only a verbal
segment. The resulting human facts are document-less and reproduce from their
own stored values; the passage is the evidence context.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.facts import (
    append_recorded_applies_to_fact,
    append_recorded_statement_timing_fact,
    append_recorded_statement_wording_fact,
    replay_recorded_applies_to_fact,
    replay_recorded_statement_timing_fact,
    replay_recorded_statement_wording_fact,
)
from corridor.models import Dependency, Document, Project, SourceSegment
from corridor.statement_values import StatementTiming


_PASSAGE = "AT&T committed to relocate the conduit by June 15, 2025."


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
def prose(session):
    project = Project(slug="cited-facts", name="Cited Facts", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="minutes.pdf",
        doc_type="minutes",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code="TEL-1",
        dep_type="utility_relocation",
        title="Telecom conflict",
    )
    session.add(dependency)
    segment = SourceSegment(
        project_id=project.id,
        document_id=document.id,
        kind="prose_span",
        exact_text=_PASSAGE,
        content_sha256=sha256(_PASSAGE.encode()).hexdigest(),
        ordinal=1,
        page_no=1,
        start_offset=0,
        end_offset=len(_PASSAGE),
    )
    session.add(segment)
    session.flush()
    return project, dependency, segment


def test_a_cited_wording_fact_is_document_less_and_replays(session, prose):
    project, _dependency, segment = prose
    fact = append_recorded_statement_wording_fact(
        session,
        segment=segment,
        subject_key="lineage:7",
        description="AT&T committed to relocate the conduit by June 15, 2025.",
        recorded_by="local:coordinator",
    )
    assert fact.document_id is None
    assert fact.extraction_run_id is None
    assert fact.subject_kind == "statement_candidate"
    assert replay_recorded_statement_wording_fact(session, fact) == _PASSAGE


def test_a_cited_timing_fact_replays_from_a_prose_passage(session, prose):
    project, _dependency, segment = prose
    fact = append_recorded_statement_timing_fact(
        session,
        segment=segment,
        subject_key="lineage:7",
        timings=(("new", StatementTiming.day("June 15, 2025", date(2025, 6, 15))),),
        recorded_by="local:coordinator",
    )
    assert fact.document_id is None
    replayed = replay_recorded_statement_timing_fact(session, fact)
    assert replayed.timings[0][1].start_date == date(2025, 6, 15)


def test_a_cited_applies_to_fact_replays_from_a_prose_passage(session, prose):
    project, dependency, segment = prose
    fact = append_recorded_applies_to_fact(
        session,
        segment=segment,
        subject_key="lineage:7",
        dependency_ids=(dependency.id,),
        recorded_by="local:coordinator",
    )
    assert fact.document_id is None
    assert replay_recorded_applies_to_fact(session, fact).dependency_ids == (
        dependency.id,
    )


def test_cited_wording_must_appear_in_its_passage(session, prose):
    project, _dependency, segment = prose
    with pytest.raises(Exception):
        append_recorded_statement_wording_fact(
            session,
            segment=segment,
            subject_key="lineage:7",
            description="A promise that never appears in the passage.",
            recorded_by="local:coordinator",
        )
