"""Typed structured-cell Record Inclusion decisions and revision history."""

from hashlib import sha256

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.db import Session, engine
from corridor.fact_decisions import (
    STRUCTURED_CELL_INCLUSION_POLICY,
    current_fact_decisions,
    fact_decisions_as_of_revision,
    include_structured_cell_fact_by_policy,
    include_current_structured_cell_facts,
    include_current_stationing_facts,
)
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    Document,
    ExtractionRun,
    Fact,
    FactDecision,
    FactSource,
    ExtractedProposal,
    ExtractedProposalFact,
    Project,
    ProjectRecordRevision,
    SourceSegment,
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
def decision_case(session):
    project = Project(slug="fact-decision", name="Fact Decision", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="matrix.xlsx",
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    run = ExtractionRun(
        document_id=document.id,
        prompt_version="decision_fixture_v1",
        outcome="completed",
        candidate_count=0,
        page_errors=0,
    )
    session.add(run)
    session.flush()
    session.add(
        ActiveExtractionRun(document_id=document.id, extraction_run_id=run.id)
    )
    segments = []
    facts = []
    for ordinal, (cell, value, fact_type) in enumerate(
        (
            ("D3", "1149+00", "station_from"),
            ("E3", "1150+00", "station_from"),
            ("A3", "UC-1", "utility_id"),
        ),
        start=1,
    ):
        segment = SourceSegment(
            project_id=project.id,
            document_id=document.id,
            kind="spreadsheet_cell",
            exact_text=value,
            content_sha256=sha256(value.encode()).hexdigest(),
            ordinal=ordinal,
            sheet_name="Utility Conflicts",
            cell_range=cell,
        )
        session.add(segment)
        session.flush()
        fact = Fact(
            project_id=project.id,
            document_id=document.id,
            extraction_run_id=run.id,
            fact_type=fact_type,
            subject_kind="source_row",
            subject_key="Utility Conflicts!3",
            text_value=value,
            date_value=None,
            date_range_start=None,
            date_range_end=None,
            external_org_value_id=None,
            document_value_id=None,
            transformation="trim_cell_text_v1",
            recorded_by="extractor:decision_fixture_v1",
            content_sha256=chr(ord("a") + ordinal) * 64,
        )
        session.add(fact)
        session.flush()
        session.add(
            FactSource(
                project_id=project.id,
                document_id=document.id,
                fact_id=fact.id,
                source_segment_id=segment.id,
                role="value_source",
                ordinal=1,
            )
        )
        facts.append(fact)
        segments.append(segment)
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="Admitted conflict",
    )
    session.add(dependency)
    session.flush()
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"fields": {}},
        source_document_id=document.id,
        source_pages=[1],
        prompt_version=run.prompt_version,
        citations_verified=True,
        state="accepted",
        merged_into=dependency.id,
    )
    session.add(candidate)
    session.flush()
    proposal = ExtractedProposal(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=run.id,
        candidate_id=candidate.id,
        kind="dependency",
        subject_key=facts[0].subject_key,
        candidate_metadata_json={"state": "pending", "source_pages": [1]},
    )
    session.add(proposal)
    session.flush()
    for ordinal, fact in enumerate(facts, 1):
        session.add(
            ExtractedProposalFact(
                project_id=project.id,
                document_id=document.id,
                extraction_run_id=run.id,
                proposal_id=proposal.id,
                fact_id=fact.id,
                ordinal=ordinal,
            )
        )
    session.flush()
    return project, document, run, tuple(facts)


def test_policy_inclusion_writes_one_typed_decision_and_revision(session, decision_case):
    project, _document, _run, facts = decision_case

    result = include_structured_cell_fact_by_policy(
        session, facts[0], idempotency_key="include:station:1"
    )

    assert result.created is True
    assert result.decision.fact_id == facts[0].id
    assert result.decision.revision_id == result.revision.id
    assert result.revision.released_policy == STRUCTURED_CELL_INCLUSION_POLICY
    assert result.revision.human_principal is None
    assert result.revision.predecessor_revision_id is None
    assert current_fact_decisions(session, project.id) == (result.decision,)
    replay = include_structured_cell_fact_by_policy(
        session, facts[0], idempotency_key="include:station:1"
    )
    assert replay.created is False
    assert replay.decision.id == result.decision.id


def test_released_structured_cell_policy_includes_a_non_stationing_fact(
    session, decision_case
):
    project, _document, _run, facts = decision_case

    result = include_structured_cell_fact_by_policy(
        session, facts[2], idempotency_key="include:utility-id:1"
    )

    assert result.decision.fact_type == "utility_id"
    assert result.revision.released_policy == STRUCTURED_CELL_INCLUSION_POLICY
    assert current_fact_decisions(session, project.id) == (result.decision,)


def test_stationing_compatibility_seam_does_not_silently_include_other_types(
    session, decision_case
):
    project, _document, _run, _facts = decision_case

    results = include_current_stationing_facts(session, project.id)

    assert len(results) == 2
    assert {result.decision.fact_type for result in results} == {"station_from"}


def test_correction_supersedes_effective_decision_and_as_of_reads_both_sides(
    session, decision_case
):
    project, _document, _run, facts = decision_case
    first = include_structured_cell_fact_by_policy(
        session, facts[0], idempotency_key="include:station:first"
    )
    second = include_structured_cell_fact_by_policy(
        session, facts[1], idempotency_key="include:station:second"
    )

    assert first.decision.superseded_by == second.decision.id
    assert current_fact_decisions(session, project.id) == (second.decision,)
    assert fact_decisions_as_of_revision(
        session, project.id, first.revision.id
    ) == (first.decision,)
    assert fact_decisions_as_of_revision(
        session, project.id, second.revision.id
    ) == (second.decision,)


def test_partial_unique_index_rejects_second_effective_value(session, decision_case):
    project, _document, _run, facts = decision_case
    first = include_structured_cell_fact_by_policy(
        session, facts[0], idempotency_key="include:station:index"
    )
    project_id = project.id
    predecessor_revision_id = first.revision.id
    second_fact_id = facts[1].id
    subject_key = facts[1].subject_key
    fact_type = facts[1].fact_type
    session.execute(text("set local role corridor_fact_decision_writer"))
    revision = ProjectRecordRevision(
        project_id=project_id,
        predecessor_revision_id=predecessor_revision_id,
        command_type="include_structured_cell_fact",
        human_principal=None,
        released_policy=STRUCTURED_CELL_INCLUSION_POLICY,
        idempotency_key="include:station:invalid-second",
    )
    session.add(revision)
    session.flush()
    session.add(
        FactDecision(
            project_id=project.id,
            fact_id=second_fact_id,
            subject_key=subject_key,
            fact_type=fact_type,
            revision_id=revision.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_direct_revision_and_decision_writes_are_database_guarded(session, decision_case):
    project, _document, _run, facts = decision_case
    revision = ProjectRecordRevision(
        project_id=project.id,
        predecessor_revision_id=None,
        command_type="bypass",
        human_principal=None,
        released_policy=STRUCTURED_CELL_INCLUSION_POLICY,
        idempotency_key="bypass",
    )
    session.add(revision)
    with pytest.raises(DBAPIError, match="requires the typed decision command"):
        session.flush()


def test_abstained_proposal_cannot_create_structured_cell_decision(session, decision_case):
    project, document, run, facts = decision_case
    candidate = session.scalar(select(Candidate))
    candidate.state = "pending"
    candidate.merged_into = None
    session.flush()

    assert include_current_structured_cell_facts(session, project.id) == ()
    with pytest.raises(DBAPIError, match="not eligible"):
        session.scalar(
            select(
                func.include_structured_cell_fact_decision(
                    project.id,
                    facts[0].id,
                    facts[0].subject_key,
                    facts[0].fact_type,
                    "direct:abstained",
                    STRUCTURED_CELL_INCLUSION_POLICY,
                )
            )
        )
