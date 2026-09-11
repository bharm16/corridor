"""The source-append commands refuse what the table constraints cannot (#492).

Every appender in ``facts.py`` and ``source_segments.py`` reaches the spine
through these commands, so the contract they enforce — project scope on typed
references, the digest of every exact text, locator identity, and idempotent
replay — is proved here once rather than once per appender.
"""

from hashlib import sha256

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from corridor.materializer import (
    materialize_document_reference,
    materialize_quoted_statement_wording,
)
from corridor.models import Document, Fact, Project, SourceSegment
from corridor.source_append import (
    SegmentValues,
    append_fact,
    append_source_segments,
)


@pytest.fixture
def other_project(session):
    project = Project(slug="source-append-other", name="Other", is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _minutes(session, project, name="minutes.pdf"):
    document = Document(
        project_id=project.id,
        sha256=sha256(name.encode()).hexdigest(),
        filename=name,
        doc_type="minutes",
    )
    session.add(document)
    session.flush()
    return document


def _span(text, ordinal=1, page_no=1, start=0):
    return SegmentValues(
        kind="prose_span",
        exact_text=text,
        content_sha256=sha256(text.encode()).hexdigest(),
        ordinal=ordinal,
        page_no=page_no,
        start_offset=start,
        end_offset=start + len(text),
    )


def _wording_fact(session, project, segment, **overrides):
    values = dict(
        project_id=project.id,
        document_id=None,
        extraction_run_id=None,
        subject_kind="statement_candidate",
        subject_key="candidate:1",
        recorded_by="local:test",
        content_sha256=sha256(f"wording:{segment.id}".encode()).hexdigest(),
        value=materialize_quoted_statement_wording(segment, segment.exact_text),
    )
    values.update(overrides)
    return append_fact(session, **values)


# --- Segments --------------------------------------------------------------


def test_segments_are_refused_for_a_document_outside_the_project(
    session, project, other_project
):
    document = _minutes(session, other_project)

    with pytest.raises(DBAPIError, match="document is outside its project"):
        append_source_segments(
            session,
            project_id=project.id,
            document_id=document.id,
            recorded_verbal_origin_id=None,
            segments=(_span("Equistar will submit the exhibit."),),
        )


def test_segments_are_refused_when_the_digest_does_not_match_the_words(
    session, project
):
    document = _minutes(session, project)
    forged = SegmentValues(
        kind="prose_span",
        exact_text="Equistar will submit the exhibit.",
        content_sha256=sha256(b"different words").hexdigest(),
        ordinal=1,
        page_no=1,
        start_offset=0,
        end_offset=33,
    )

    with pytest.raises(DBAPIError, match="digest does not match its exact text"):
        append_source_segments(
            session,
            project_id=project.id,
            document_id=document.id,
            recorded_verbal_origin_id=None,
            segments=(forged,),
        )


def test_segments_need_exactly_one_of_a_document_or_an_origin(session, project):
    with pytest.raises(
        DBAPIError, match="one document or one recorded verbal origin"
    ):
        append_source_segments(
            session,
            project_id=project.id,
            document_id=None,
            recorded_verbal_origin_id=None,
            segments=(_span("Orphaned words."),),
        )


def test_replaying_a_locator_returns_the_same_row_and_refuses_other_words(
    session, project
):
    document = _minutes(session, project)
    first = append_source_segments(
        session,
        project_id=project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=(_span("Equistar will submit the exhibit."),),
    )
    replayed = append_source_segments(
        session,
        project_id=project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=(_span("Equistar will submit the exhibit."),),
    )

    assert [row.id for row in replayed] == [row.id for row in first]
    assert session.scalar(
        select(func.count()).select_from(SourceSegment).where(
            SourceSegment.document_id == document.id
        )
    ) == 1

    rebound = _span("Equistar will submit the exhibit!")
    with pytest.raises(DBAPIError, match="already bound to different content"):
        append_source_segments(
            session,
            project_id=project.id,
            document_id=document.id,
            recorded_verbal_origin_id=None,
            segments=(rebound,),
        )


# --- Facts -----------------------------------------------------------------


def test_a_fact_is_refused_when_its_source_lies_in_another_project(
    session, project, other_project
):
    document = _minutes(session, other_project)
    (segment,) = append_source_segments(
        session,
        project_id=other_project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=(_span("Equistar will submit the exhibit."),),
    )

    with pytest.raises(DBAPIError, match="source segment is outside its project"):
        _wording_fact(session, project, segment)


def test_a_fact_is_refused_when_its_document_value_lies_in_another_project(
    session, project, other_project
):
    foreign = _minutes(session, other_project, "foreign-support.pdf")

    with pytest.raises(DBAPIError, match="document value is outside its project"):
        append_fact(
            session,
            project_id=project.id,
            document_id=None,
            extraction_run_id=None,
            subject_kind="record_subject",
            subject_key="constraint:1",
            recorded_by="local:test",
            content_sha256=sha256(b"support").hexdigest(),
            value=materialize_document_reference(foreign.id),
        )


def test_a_fact_needs_its_content_digest(session, project):
    document = _minutes(session, project)
    (segment,) = append_source_segments(
        session,
        project_id=project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=(_span("Equistar will submit the exhibit."),),
    )

    with pytest.raises(DBAPIError, match="needs its content digest"):
        _wording_fact(session, project, segment, content_sha256="not-a-digest")


def test_replaying_a_fact_digest_returns_the_same_fact(session, project):
    document = _minutes(session, project)
    (segment,) = append_source_segments(
        session,
        project_id=project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=(_span("Equistar will submit the exhibit."),),
    )
    first = _wording_fact(session, project, segment)
    replayed = _wording_fact(session, project, segment)

    assert replayed.id == first.id
    assert session.scalar(
        select(func.count()).select_from(Fact).where(Fact.project_id == project.id)
    ) == 1
