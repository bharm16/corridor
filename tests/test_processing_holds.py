"""A hold says which processing stage it prohibits, and every gate asks (#919).

The predecessor's single gate refused all rich processing for any hold, while
every hold a production path wrote was an extraction restriction on a document
that had already been read. These cases hold the ruling that replaced it: two
boundaries, an intersection over independent reasons, and one stage-aware
answer consulted on every route into a parser or a semantic reader.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from corridor import access, processing_holds, source_register
from corridor.extraction_runs import record_extraction_run
from corridor.extract_project import extract_project
from corridor.ingest import ingest_document, reparse_document
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    DocumentQuarantine,
    ExtractionRun,
)
from corridor.pipeline import EXTRACTED_PROPOSALS, ExtractionRoute
from corridor.principals import HumanPrincipal

from pdf_fixture_support import PdfFixture


AT = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
OPERATOR = HumanPrincipal("local:ops")
OUTSIDER = HumanPrincipal("local:nobody")


@pytest.fixture
def pdf(tmp_path):
    """One synthetic page with enough real text to avoid the scan heuristic."""

    fixture = PdfFixture()
    page = fixture.add_page(height=180)
    page.text((72, 100), "Page 1")
    page.text(
        (72, 130),
        "Utility Owner: AT&T Texas (SWBT) - Telecom - underground fiber optic",
    )
    return fixture.save(tmp_path / "matrix.pdf")


def _document(session, project, *, doc_type="matrix", digest="a" * 64):
    document = Document(
        project_id=project.id,
        sha256=digest,
        filename=f"{doc_type}.pdf",
        doc_type=doc_type,
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    return document


def _reading_hold(session, document_id, *, reason_code=None, reason=None):
    return processing_holds.impose_hold(
        session,
        document_id=document_id,
        prohibited_stage=processing_holds.DOCUMENT_READING,
        reason_code=reason_code or processing_holds.INTAKE_SECURITY_FINDING,
        reason=reason or "an intake check refused these bytes for rich reading",
        authority=processing_holds.INTAKE_SECURITY,
        imposed_by="tests.test_processing_holds",
        evidence=f"documents.id={document_id}",
    )


def _extraction_hold(session, document_id):
    return processing_holds.impose_hold(
        session,
        document_id=document_id,
        prohibited_stage=processing_holds.SEMANTIC_EXTRACTION,
        reason_code=processing_holds.UNMODELED_SEQUENCING_SEMANTICS,
        reason="document-asserted work sequencing is not modeled (#149)",
        authority=processing_holds.PROCESSING_RULE,
        imposed_by="tests.test_processing_holds",
        evidence=f"documents.id={document_id} doc_type='schedule'",
    )


# --- the two boundaries -----------------------------------------------------


def test_a_restriction_on_extraction_leaves_reading_permitted(session, project):
    """The distinction the whole ticket is about, at the gate itself."""

    document = _document(session, project, doc_type="schedule")
    _extraction_hold(session, document.id)

    standing = processing_holds.permission(session, document.id)
    assert (standing.may_read_document, standing.may_extract_semantics) == (
        True,
        False,
    )
    processing_holds.assert_may_read_document(session, document.id)
    with pytest.raises(processing_holds.ProcessingHoldInForce) as refused:
        processing_holds.assert_may_extract_semantics(session, document.id)
    assert refused.value.stage == processing_holds.SEMANTIC_EXTRACTION


def test_a_restriction_on_reading_prohibits_extraction_without_a_second_row(
    session, project
):
    """The intersection, not a lookup: reading refused refuses extraction too."""

    document = _document(session, project)
    _reading_hold(session, document.id)

    standing = processing_holds.permission(session, document.id)
    assert (standing.may_read_document, standing.may_extract_semantics) == (
        False,
        False,
    )
    with pytest.raises(processing_holds.ProcessingHoldInForce) as refused:
        processing_holds.assert_may_extract_semantics(session, document.id)
    # The stage reported is the one that actually stopped it.
    assert refused.value.stage == processing_holds.DOCUMENT_READING


def test_an_unclassified_historical_restriction_prohibits_reading(session, project):
    """What the migration writes where it could not establish a hold's scope.

    The conservative default is restrictive, and it says so in the reason code
    rather than by implying the file is dangerous.
    """

    document = _document(session, project)
    processing_holds.impose_hold(
        session,
        document_id=document.id,
        prohibited_stage=processing_holds.DOCUMENT_READING,
        reason_code=processing_holds.UNCLASSIFIED_HISTORICAL_HOLD,
        reason="no retained evidence establishes what this hold prohibited",
        authority=processing_holds.TECHNICAL_OPERATIONS,
        imposed_by="tests.test_processing_holds",
        evidence="none",
    )

    assert not processing_holds.permission(session, document.id).may_read_document


# --- independent reasons ----------------------------------------------------


def test_releasing_one_restriction_cannot_override_another(
    session, member_project, tmp_path
):
    """The maintainer's worked example, as one case.

    Unsupported semantics beside a security restriction: reading stays
    prohibited. Clear the semantics one and the security one still prohibits
    reading. A mapping repair cannot accidentally clear a safety restriction,
    because the release reaches one row and there is no row to overwrite.
    """

    project = member_project(OPERATOR)
    principal = OPERATOR
    document = _document(session, project, doc_type="schedule")
    _extraction_hold(session, document.id)
    _reading_hold(session, document.id)

    assert not processing_holds.permission(session, document.id).may_read_document

    processing_holds.release_hold(
        session,
        document_id=document.id,
        reason_code=processing_holds.UNMODELED_SEQUENCING_SEMANTICS,
        principal=principal,
        evidence="the scheduling relationship is modeled as of ruleset v9",
        at=AT,
    )

    standing = processing_holds.permission(session, document.id)
    assert (standing.may_read_document, standing.may_extract_semantics) == (
        False,
        False,
    )
    # And the released restriction is still legible: nothing was overwritten.
    released = session.scalars(
        select(DocumentQuarantine).where(
            DocumentQuarantine.document_id == document.id,
            DocumentQuarantine.released_at.is_not(None),
        )
    ).one()
    assert released.reason_code == processing_holds.UNMODELED_SEQUENCING_SEMANTICS
    assert "ruleset v9" in released.release_evidence
    assert "work sequencing" in released.reason


def test_imposing_a_restriction_never_rewrites_the_standing_one(session, project):
    """Two calls, one row, and the first writer's words survive the second."""

    document = _document(session, project, doc_type="schedule")
    first = _extraction_hold(session, document.id)
    again = _extraction_hold(session, document.id)

    assert again.id == first.id
    assert len(processing_holds.open_holds(session, document.id)) == 1


def test_an_authority_may_only_impose_the_stage_the_ruling_gives_it(
    session, project
):
    """The processing pipeline restricts interpretation, never reading."""

    document = _document(session, project)
    with pytest.raises(ValueError, match="may not prohibit document_reading"):
        processing_holds.impose_hold(
            session,
            document_id=document.id,
            prohibited_stage=processing_holds.DOCUMENT_READING,
            reason_code=processing_holds.UNMODELED_SEQUENCING_SEMANTICS,
            reason="a rule the processing pipeline applied",
            authority=processing_holds.PROCESSING_RULE,
            imposed_by="tests.test_processing_holds",
            evidence="none",
        )


def test_releasing_a_restriction_is_a_technical_operations_act(
    session, member_project
):
    """And it cites the evidence that removes the cause, or it is refused."""

    project = member_project(OPERATOR)
    principal = OPERATOR
    document = _document(session, project)
    _reading_hold(session, document.id)

    with pytest.raises(ValueError, match="cites the evidence"):
        processing_holds.release_hold(
            session,
            document_id=document.id,
            reason_code=processing_holds.INTAKE_SECURITY_FINDING,
            principal=principal,
            evidence="   ",
            at=AT,
        )
    with pytest.raises(processing_holds.ProcessingHoldRefused):
        processing_holds.release_hold(
            session,
            document_id=document.id,
            reason_code=processing_holds.INTAKE_SECURITY_FINDING,
            principal=OUTSIDER,
            evidence="a re-scan found nothing",
            at=AT,
        )
    assert not processing_holds.permission(session, document.id).may_read_document


def test_classifying_an_unclassified_restriction_records_both_acts(
    session, member_project
):
    """The third migration outcome ends with an attributable classification.

    The unclassified restriction is released citing the evidence, and the
    restriction the operator established is imposed as its own row. Nothing is
    edited, so the record still says what it said before.
    """

    project = member_project(OPERATOR)
    principal = OPERATOR
    document = _document(session, project, doc_type="schedule")
    processing_holds.impose_hold(
        session,
        document_id=document.id,
        prohibited_stage=processing_holds.DOCUMENT_READING,
        reason_code=processing_holds.UNCLASSIFIED_HISTORICAL_HOLD,
        reason="no retained evidence establishes what this hold prohibited",
        authority=processing_holds.TECHNICAL_OPERATIONS,
        imposed_by="migration:b2d5f8a1c4e7:unclassified",
        evidence="none",
    )

    processing_holds.classify_hold(
        session,
        document_id=document.id,
        prohibited_stage=processing_holds.SEMANTIC_EXTRACTION,
        reason_code=processing_holds.UNMODELED_SEQUENCING_SEMANTICS,
        reason="document-asserted work sequencing is not modeled (#149)",
        principal=principal,
        evidence="the registration receipt records the schedule import rule",
        at=AT,
    )

    standing = processing_holds.permission(session, document.id)
    assert (standing.may_read_document, standing.may_extract_semantics) == (
        True,
        False,
    )
    rows = session.scalars(
        select(DocumentQuarantine)
        .where(DocumentQuarantine.document_id == document.id)
        .order_by(DocumentQuarantine.id)
    ).all()
    assert [row.reason_code for row in rows] == [
        processing_holds.UNCLASSIFIED_HISTORICAL_HOLD,
        processing_holds.UNMODELED_SEQUENCING_SEMANTICS,
    ]
    assert rows[0].released_by == principal.subject


# --- the entry points -------------------------------------------------------


def test_direct_parsing_ingest_never_hands_prohibited_bytes_to_the_parser(
    session, project, pdf, tmp_path
):
    """Registration reads the file; a restriction on reading stops it there.

    Registered first with the read left to the standing pass, then restricted,
    then re-registered with parsing on: the same bytes, the same call, and the
    reader is never reached.
    """

    document = ingest_document(
        session,
        project_id=project.id,
        path=pdf,
        doc_type="matrix",
        images_dir=tmp_path / "images",
        parse=False,
    )
    _reading_hold(session, document.id)
    session.flush()

    with pytest.raises(processing_holds.ProcessingHoldInForce):
        ingest_document(
            session,
            project_id=project.id,
            path=pdf,
            doc_type="matrix",
            images_dir=tmp_path / "images",
        )
    assert session.scalars(
        select(DocPage).where(DocPage.document_id == document.id)
    ).all() == []


def test_the_explicit_reparse_never_hands_prohibited_bytes_to_the_parser(
    session, project, pdf, tmp_path
):
    """The bounded recovery an operator runs asks the same answer."""

    document = ingest_document(
        session,
        project_id=project.id,
        path=pdf,
        doc_type="matrix",
        images_dir=tmp_path / "images",
        parse=False,
    )
    _reading_hold(session, document.id)
    session.flush()

    with pytest.raises(processing_holds.ProcessingHoldInForce):
        reparse_document(
            session,
            document=document,
            path=pdf,
            images_dir=tmp_path / "images",
        )
    assert document.parse_status == "pending"


def test_a_document_held_only_against_extraction_is_read_by_direct_ingest(
    session, project, pdf, tmp_path
):
    """The permitted read the ruling turns on.

    A `schedule` registration writes its own extraction restriction in the same
    call, and the reader still runs: the workbook's values reach pages and
    segments with their exact locators, and nothing takes meaning out of the
    sequencing it asserts.
    """

    document = ingest_document(
        session,
        project_id=project.id,
        path=pdf,
        doc_type="schedule",
        images_dir=tmp_path / "images",
    )

    assert document.parse_status == "parsed"
    assert session.scalars(
        select(DocPage).where(DocPage.document_id == document.id)
    ).all() != []
    [hold] = processing_holds.open_holds(session, document.id)
    assert hold.prohibited_stage == processing_holds.SEMANTIC_EXTRACTION
    assert hold.imposed_by_authority == processing_holds.PROCESSING_RULE


def test_an_extraction_restriction_stops_the_semantic_reader(session, project):
    """`extract_project` asks before any route runs, not only in its selector.

    A `matrix` rather than a schedule, because a schedule is not a kind any
    extractor takes and would never reach this loop at all. The restriction
    here is the one a delivered revision carries when nobody has said which
    revision it is (#825) -- an extraction restriction on an extractable
    document, which is exactly the case a selector alone would not bind.
    """

    document = _document(session, project)
    processing_holds.impose_hold(
        session,
        document_id=document.id,
        prohibited_stage=processing_holds.SEMANTIC_EXTRACTION,
        reason_code=processing_holds.UNDECLARED_SOURCE_REVISION,
        reason="nobody has declared which revision this delivery is",
        authority=processing_holds.PROCESSING_RULE,
        imposed_by="tests.test_processing_holds",
        evidence=f"documents.id={document.id}",
    )
    session.flush()

    def never_called(session, doc):  # pragma: no cover - the point of the case
        raise AssertionError("the semantic reader must not be reached")

    route = ExtractionRoute(
        effective_prompt_version="test-v1",
        schema_version="test-v1",
        extract=never_called,
        output=EXTRACTED_PROPOSALS,
        allow_unsealed_legacy=True,
    )
    outcomes = extract_project(
        session, project, select_route=lambda _document: route, commit=False
    )

    assert [outcome.status for outcome in outcomes] == ["quarantined"]
    assert "semantic extraction is not permitted" in outcomes[0].detail
    assert session.scalars(
        select(Candidate).where(Candidate.source_document_id == document.id)
    ).all() == []


def test_the_register_reports_reading_and_extraction_together(session, project):
    """The combined reading, over the three conditions a restriction produces."""

    read_and_held = _document(session, project, doc_type="schedule", digest="b" * 64)
    _extraction_hold(session, read_and_held.id)
    queued_and_held = _document(
        session, project, doc_type="schedule", digest="c" * 64
    )
    queued_and_held.parse_status = "pending"
    _extraction_hold(session, queued_and_held.id)
    unreadable = _document(session, project, digest="d" * 64)
    unreadable.parse_status = "failed"
    _reading_hold(session, unreadable.id)
    session.flush()

    register = source_register.read_source_register(session, project_id=project.id)
    rows = {row.document_id: row for row in register.rows}

    assert rows[read_and_held.id].state == source_register.READ_EXTRACTION_HELD
    assert rows[read_and_held.id].state_words == (
        "Document read. Extraction is on hold."
    )
    assert rows[queued_and_held.id].state == (
        source_register.PENDING_EXTRACTION_HELD
    )
    # A restriction on reading outranks the failed attempt, because the
    # failure's own sentence promises a re-read the restriction refuses.
    assert rows[unreadable.id].state == source_register.READING_HELD
    # And none of the three sentences asserts why the source is held.
    for row in rows.values():
        assert "does not model" not in row.next_action


def test_a_failed_parse_keeps_its_own_words_under_an_extraction_restriction(
    session, project
):
    """An extraction restriction does not mean the reading failed."""

    document = _document(session, project, doc_type="schedule")
    document.parse_status = "failed"
    _extraction_hold(session, document.id)
    session.flush()

    register = source_register.read_source_register(session, project_id=project.id)
    [row] = [row for row in register.rows if row.document_id == document.id]
    assert row.state == "parse_failed"


def test_a_quarantined_run_does_not_read_as_a_processing_failure(session, project):
    """The receipt a restriction produced is not a failure a pass will retry."""

    document = _document(session, project, doc_type="schedule")
    _extraction_hold(session, document.id)
    record_extraction_run(
        session,
        document,
        prompt_version="test-v1",
        candidate_count=0,
        page_errors=1,
        outcome="quarantined",
        model="none",
        schema_version="test-v1",
        allow_unsealed_legacy=True,
    )
    session.flush()

    register = source_register.read_source_register(session, project_id=project.id)
    [row] = [row for row in register.rows if row.document_id == document.id]
    assert row.state == source_register.READ_EXTRACTION_HELD
    assert session.scalars(
        select(ExtractionRun).where(ExtractionRun.document_id == document.id)
    ).all() != []


def test_the_technical_operations_designation_is_read_live(session, member_project):
    """The same roster reader every project surface uses, not a second opinion."""

    project = member_project(OPERATOR)
    principal = OPERATOR
    membership = access.resolve_membership(session, principal.subject, project.id)
    assert membership is not None and membership.has(access.TECHNICAL_OPERATIONS)
