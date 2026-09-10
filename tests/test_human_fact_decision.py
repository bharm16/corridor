"""A Human Record Decision writes the spine, attributably and reversibly (#451)."""

from __future__ import annotations

from hashlib import sha256

import pytest
from sqlalchemy import select

from corridor.current_record import read_current_project_record
from corridor.fact_decisions import (
    FactDecisionRefused,
    HumanDecisionResult,
    StaleHumanDecision,
    record_human_fact_decision,
)
from corridor.models import (
    ActiveExtractionRun,
    Document,
    ExtractionRun,
    Fact,
    FactDecision,
    Project,
    ProjectRecordRevision,
)
from corridor.principals import HumanPrincipal


RECORDER = HumanPrincipal("local:dana-fields")


@pytest.fixture
def project_document_run(session):
    project = Project(slug="human-decision", name="Human Decision", is_synthetic=True)
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
    run = ExtractionRun(
        document_id=document.id,
        prompt_version="human_fixture_v1",
        outcome="completed",
        candidate_count=0,
        page_errors=0,
    )
    session.add(run)
    session.flush()
    session.add(ActiveExtractionRun(document_id=document.id, extraction_run_id=run.id))
    session.flush()
    return project, document, run


def _statement_fact(session, project, document, run, *, text, key):
    fact = Fact(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=run.id,
        fact_type="statement_wording",
        subject_kind="statement_candidate",
        subject_key=key,
        text_value=text,
        transformation="exact_prose_span_v1",
        recorded_by="extractor:human_fixture_v1",
        content_sha256=sha256(text.encode()).hexdigest(),
    )
    session.add(fact)
    session.flush()
    return fact


def _station_fact(session, project, document, run):
    fact = Fact(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=run.id,
        fact_type="station_from",
        subject_kind="source_row",
        subject_key="Sheet1!3",
        text_value="1149+00",
        transformation="trim_cell_text_v1",
        recorded_by="extractor:human_fixture_v1",
        content_sha256=sha256(b"1149+00").hexdigest(),
    )
    session.add(fact)
    session.flush()
    return fact


def test_human_decision_writes_a_human_principal_revision_and_decision(
    session, project_document_run
):
    project, document, run = project_document_run
    fact = _statement_fact(
        session, project, document, run, text="Equistar will submit.", key="candidate:1"
    )

    result = record_human_fact_decision(
        session,
        fact,
        principal=RECORDER,
        command_type="record_verbal_statement",
        idempotency_key="verbal-1",
    )

    assert isinstance(result, HumanDecisionResult)
    assert result.created is True
    assert result.revision.human_principal == "local:dana-fields"
    assert result.revision.released_policy is None  # human XOR policy
    assert result.revision.command_type == "record_verbal_statement"
    assert result.decision.fact_id == fact.id
    assert result.decision.superseded_by is None


def test_human_decision_is_idempotent_on_its_key(session, project_document_run):
    project, document, run = project_document_run
    fact = _statement_fact(
        session, project, document, run, text="Same words.", key="candidate:2"
    )
    first = record_human_fact_decision(
        session, fact, principal=RECORDER,
        command_type="record_verbal_statement", idempotency_key="verbal-2",
    )
    second = record_human_fact_decision(
        session, fact, principal=RECORDER,
        command_type="record_verbal_statement", idempotency_key="verbal-2",
    )
    assert second.created is False
    assert second.decision.id == first.decision.id
    assert second.revision.id == first.revision.id


def test_correction_supersedes_the_named_predecessor(session, project_document_run):
    project, document, run = project_document_run
    original = _statement_fact(
        session, project, document, run, text="March.", key="candidate:3"
    )
    corrected = _statement_fact(
        session, project, document, run, text="April.", key="candidate:3"
    )
    first = record_human_fact_decision(
        session, original, principal=RECORDER,
        command_type="record_verbal_statement", idempotency_key="verbal-3a",
    )
    second = record_human_fact_decision(
        session, corrected, principal=RECORDER,
        command_type="correct_statement_facts", idempotency_key="verbal-3b",
        expected_predecessor=first.decision.id,
    )
    session.expire_all()
    superseded = session.get(FactDecision, first.decision.id)
    assert superseded.superseded_by == second.decision.id
    assert second.decision.superseded_by is None


def test_a_stale_predecessor_is_refused(session, project_document_run):
    project, document, run = project_document_run
    original = _statement_fact(
        session, project, document, run, text="First.", key="candidate:4"
    )
    later = _statement_fact(
        session, project, document, run, text="Second.", key="candidate:4"
    )
    third = _statement_fact(
        session, project, document, run, text="Third.", key="candidate:4"
    )
    first = record_human_fact_decision(
        session, original, principal=RECORDER,
        command_type="record_verbal_statement", idempotency_key="verbal-4a",
    )
    record_human_fact_decision(
        session, later, principal=RECORDER,
        command_type="correct_statement_facts", idempotency_key="verbal-4b",
        expected_predecessor=first.decision.id,
    )
    # first.decision is now superseded; correcting from it again is stale.
    with pytest.raises(StaleHumanDecision):
        record_human_fact_decision(
            session, third, principal=RECORDER,
            command_type="correct_statement_facts", idempotency_key="verbal-4c",
            expected_predecessor=first.decision.id,
        )


def test_a_structured_cell_fact_admits_a_human_discrepancy_decision(
    session, project_document_run
):
    # The cell types are dual-use since #451 stage 3: automatic off their
    # spreadsheet cells, human-settled by a Discrepancy Resolution.
    project, document, run = project_document_run
    fact = _station_fact(session, project, document, run)
    result = record_human_fact_decision(
        session, fact, principal=RECORDER,
        command_type="resolve_discrepancy", idempotency_key="station-x",
    )
    assert result.created is True
    assert result.revision.human_principal == RECORDER.subject


def test_the_current_view_shows_the_effective_human_decision(
    session, project_document_run
):
    project, document, run = project_document_run
    fact = _statement_fact(
        session, project, document, run, text="Visible.", key="candidate:5"
    )
    record_human_fact_decision(
        session, fact, principal=RECORDER,
        command_type="record_verbal_statement", idempotency_key="verbal-5",
    )
    session.expire_all()
    current = read_current_project_record(session, project.id)
    keys = {(value.subject_key, value.fact_type) for value in current}
    assert ("candidate:5", "statement_wording") in keys
