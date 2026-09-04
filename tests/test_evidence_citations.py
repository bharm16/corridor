"""An Evidence Link cites the Source Segment that owns its words (#605)."""

from hashlib import sha256

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.evidence_citations import (
    citable_segment_for_quote,
    cite_source_segments,
    evidence_quotation,
)
from corridor.models import (
    Dependency,
    Document,
    EvidenceLink,
    EvidenceLinkSource,
    Project,
    SourceSegment,
)

PASSAGE = "Kinder Morgan confirmed the relocation window opens in March."
OTHER_PASSAGE = "Equistar has not confirmed a relocation window."


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def _new_document(session, project, *, filename, sha):
    document = Document(
        project_id=project.id,
        sha256=sha,
        filename=filename,
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    return document


def _new_segment(session, project, document, *, text_value, ordinal, start=0):
    segment = SourceSegment(
        project_id=project.id,
        document_id=document.id,
        kind="prose_span",
        exact_text=text_value,
        content_sha256=sha256(text_value.encode()).hexdigest(),
        ordinal=ordinal,
        page_no=1,
        start_offset=start,
        end_offset=start + len(text_value),
    )
    session.add(segment)
    session.flush()
    return segment


def _new_link(session, document, dependency, *, quote):
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=quote,
        verified=True,
    )
    session.add(link)
    session.flush()
    return link


@pytest.fixture
def cited(session):
    project = Project(
        slug="evidence-citation-test",
        name="Evidence Citation Test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    document = _new_document(session, project, filename="minutes.pdf", sha="a" * 64)
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-CITE-1",
        dep_type="utility_relocation",
        title="Citation subject",
    )
    session.add(dependency)
    session.flush()
    segment = _new_segment(session, project, document, text_value=PASSAGE, ordinal=1)
    link = _new_link(session, document, dependency, quote=PASSAGE)
    return project, document, dependency, segment, link


def test_a_citation_names_the_segment_and_carries_no_text(cited, session):
    project, _document, _dependency, segment, link = cited

    rows = cite_source_segments(session, link, (segment,))

    assert [row.source_segment_id for row in rows] == [segment.id]
    assert [row.ordinal for row in rows] == [1]
    # The relationship stores identity, not words: nothing on the row could
    # disagree with the segment it names.
    stored = set(EvidenceLinkSource.__table__.columns.keys())
    assert not any("quote" in name or "text" in name for name in stored)
    quotation = evidence_quotation(session, link)
    assert quotation.owner == "source_segments"
    assert quotation.passages == (PASSAGE,)
    assert quotation.source_segment_ids == (segment.id,)


def test_a_link_with_no_citation_still_reads_its_legacy_quote(cited, session):
    _project, _document, _dependency, _segment, link = cited

    quotation = evidence_quotation(session, link)

    assert quotation.owner == "legacy_quote"
    assert quotation.passages == (link.quote,)
    assert quotation.source_segment_ids == ()


def test_citations_keep_appending_in_order(cited, session):
    project, document, _dependency, segment, link = cited
    second = _new_segment(
        session,
        project,
        document,
        text_value=OTHER_PASSAGE,
        ordinal=2,
        start=len(PASSAGE) + 1,
    )

    cite_source_segments(session, link, (segment,))
    cite_source_segments(session, link, (second,))

    quotation = evidence_quotation(session, link)
    assert quotation.passages == (PASSAGE, OTHER_PASSAGE)
    assert quotation.source_segment_ids == (segment.id, second.id)


def test_a_citation_refuses_a_segment_from_another_rendition(cited, session):
    project, _document, _dependency, _segment, link = cited
    other_document = _document_in(session, project)
    foreign = _new_segment(
        session, project, other_document, text_value=PASSAGE, ordinal=1
    )

    with pytest.raises(ValueError, match="document rendition"):
        cite_source_segments(session, link, (foreign,))


def _document_in(session, project):
    return _new_document(session, project, filename="other.pdf", sha="b" * 64)


def test_the_database_refuses_a_citation_across_renditions(cited, session):
    project, _document, _dependency, _segment, link = cited
    other_document = _document_in(session, project)
    foreign = _new_segment(
        session, project, other_document, text_value=PASSAGE, ordinal=1
    )

    # The Python check above is a better error message; this is the rule.
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "insert into evidence_link_sources (project_id, document_id, "
                "evidence_link_id, source_segment_id, ordinal) "
                "values (:project, :document, :link, :segment, 1)"
            ),
            {
                "project": project.id,
                "document": other_document.id,
                "link": link.id,
                "segment": foreign.id,
            },
        )


def test_a_citable_segment_is_one_whole_passage_or_nothing(cited, session):
    project, document, _dependency, segment, _link = cited

    exact = citable_segment_for_quote(
        session, document_id=document.id, page_no=1, quote=PASSAGE
    )
    assert exact is not None and exact.id == segment.id

    # A substring is not what the segment delimits, so there is nothing to
    # cite and the legacy copy stays the owner of those words.
    assert (
        citable_segment_for_quote(
            session,
            document_id=document.id,
            page_no=1,
            quote=PASSAGE.split(" ", 1)[1],
        )
        is None
    )
    # Two occurrences of the same words cannot say which one was read.
    _new_segment(
        session,
        project,
        document,
        text_value=PASSAGE,
        ordinal=2,
        start=len(PASSAGE) + 1,
    )
    assert (
        citable_segment_for_quote(
            session, document_id=document.id, page_no=1, quote=PASSAGE
        )
        is None
    )


def test_a_citation_refuses_an_empty_or_repeated_segment_set(cited, session):
    _project, _document, _dependency, segment, link = cited

    with pytest.raises(ValueError, match="at least one Source Segment"):
        cite_source_segments(session, link, ())
    with pytest.raises(ValueError, match="same segment twice"):
        cite_source_segments(session, link, (segment, segment))
