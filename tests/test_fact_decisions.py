"""Typed structured-cell Record Inclusion decisions and revision history."""

from hashlib import sha256

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.fact_decisions import (
    FactDecisionRefused,
    STRUCTURED_CELL_INCLUSION_POLICY,
    current_fact_decisions,
    fact_decisions_as_of_revision,
    include_structured_cell_fact_by_policy,
    include_current_structured_cell_facts,
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
            ("B3", "Unknown Utility", "external_org"),
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


def test_unresolved_external_org_wording_remains_pending_without_a_revision(
    session, decision_case
):
    project, _document, _run, facts = decision_case

    with pytest.raises(FactDecisionRefused, match="exact registered alias"):
        include_structured_cell_fact_by_policy(
            session, facts[3], idempotency_key="include:unknown-org"
        )
    with pytest.raises(DBAPIError, match="not eligible"):
        with session.begin_nested():
            session.scalar(
                select(
                    func.include_structured_cell_fact_decision(
                        project.id,
                        facts[3].id,
                        facts[3].subject_key,
                        facts[3].fact_type,
                        "include:unknown-org:direct",
                        STRUCTURED_CELL_INCLUSION_POLICY,
                    )
                )
            )

    assert session.scalars(
        select(ProjectRecordRevision).where(
            ProjectRecordRevision.project_id == project.id
        )
    ).all() == []


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
    with as_role(session, RECORD_DECISION_ROLE):
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
        with pytest.raises(IntegrityError), session.begin_nested():
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


def test_settling_a_discrepancy_re_decides_the_matching_observed_fact(
    session, decision_case
):
    from corridor.disputes import settle_dispute
    from corridor.models import Assertion, EvidenceLink
    from corridor.principals import HumanPrincipal

    project, document, _run, facts = decision_case
    dependency_id = session.scalar(
        select(Candidate.merged_into).where(Candidate.project_id == project.id)
    )
    for value in ("1149+00", "1150+00"):
        link = EvidenceLink(
            dependency_id=dependency_id,
            document_id=document.id,
            page_no=1,
            quote=value,
            verified=True,
        )
        session.add(link)
        session.flush()
        session.add(
            Assertion(
                dependency_id=dependency_id,
                field_name="station_from",
                asserted_value=value,
                evidence_link_id=link.id,
            )
        )
    session.flush()
    included = include_structured_cell_fact_by_policy(
        session, facts[0], idempotency_key="include:station:discrepancy"
    )

    settlement = settle_dispute(
        session,
        dependency_id,
        "station_from",
        value="1150+00",
        principal=HumanPrincipal("local:dispute-settler"),
    )

    decisions = session.scalars(
        select(FactDecision)
        .where(
            FactDecision.project_id == project.id,
            FactDecision.fact_type == "station_from",
        )
        .order_by(FactDecision.id)
    ).all()
    assert len(decisions) == 2
    superseded, resolution = decisions
    assert superseded.id == included.decision.id
    assert superseded.superseded_by == resolution.id
    # The settlement selected the already-observed E3 reading, so the
    # resolution re-decides that fact rather than synthesizing a value.
    assert resolution.fact_id == facts[1].id
    assert resolution.disposition == "include"
    assert resolution.superseded_by is None
    revision = session.get(ProjectRecordRevision, resolution.revision_id)
    assert revision.command_type == "resolve_discrepancy"
    assert revision.human_principal == "local:dispute-settler"
    assert revision.released_policy is None
    assert (
        revision.idempotency_key == f"resolve-discrepancy:{settlement.id}"
    )


def test_settling_to_a_value_no_fact_carries_stays_legacy_only(
    session, decision_case
):
    from corridor.disputes import settle_dispute
    from corridor.models import Assertion, EvidenceLink
    from corridor.principals import HumanPrincipal

    project, document, _run, facts = decision_case
    dependency_id = session.scalar(
        select(Candidate.merged_into).where(Candidate.project_id == project.id)
    )
    for value in ("1149+00", "1150+00"):
        link = EvidenceLink(
            dependency_id=dependency_id,
            document_id=document.id,
            page_no=1,
            quote=value,
            verified=True,
        )
        session.add(link)
        session.flush()
        session.add(
            Assertion(
                dependency_id=dependency_id,
                field_name="station_from",
                asserted_value=value,
                evidence_link_id=link.id,
            )
        )
    session.flush()
    included = include_structured_cell_fact_by_policy(
        session, facts[0], idempotency_key="include:station:synthesized"
    )

    settle_dispute(
        session,
        dependency_id,
        "station_from",
        value="1151+50",
        principal=HumanPrincipal("local:dispute-settler"),
    )

    decisions = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.fact_type == "station_from",
        )
    ).all()
    assert [decision.id for decision in decisions] == [included.decision.id]
    assert decisions[0].superseded_by is None
