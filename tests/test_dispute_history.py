"""ADR-0061 chronology decisions stay distinct from human settlements."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.disputes import (
    apply_staleness_resolutions,
    disputes_for,
    history_assessments_for,
    NoSuchDispute,
    record_dispute_clarification,
    settle_dispute,
)
from corridor.models import (
    Assertion,
    Dependency,
    DisputeHistoryResolution,
    DocPage,
    Document,
    EvidenceLink,
    Project,
    ProjectRosterEntry,
    WorkDecision,
)
from corridor.principals import HumanPrincipal
from corridor.work_list import build_work_list


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(slug="dispute-history", name="Dispute history", is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _claim(session, dependency, *, filename, doc_date, value, doc_type="matrix"):
    document = Document(
        project_id=dependency.project_id,
        sha256=hashlib.sha256(filename.encode()).hexdigest(),
        filename=filename,
        doc_type=doc_type,
        doc_date=doc_date,
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    quote = f"Pipe size: {value}"
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=quote,
        verified=True,
    )
    session.add(link)
    session.flush()
    assertion = Assertion(
        dependency_id=dependency.id,
        field_name="station_from",
        asserted_value=value,
        evidence_link_id=link.id,
        doc_date=doc_date,
    )
    session.add(assertion)
    session.flush()
    return assertion, document


def _dependency(session, project, *, owner=None):
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-HISTORY-1",
        dep_type="utility_relocation",
        title="Water main",
        internal_owner=owner,
    )
    session.add(dependency)
    session.flush()
    return dependency


def test_stale_physical_source_is_a_recorded_history_resolution_not_a_settlement(
    session, project
):
    dependency = _dependency(session, project)
    old, _ = _claim(
        session,
        dependency,
        filename="matrix-2025-01.pdf",
        doc_date=date(2025, 1, 10),
        value="12-inch",
    )
    new, _ = _claim(
        session,
        dependency,
        filename="matrix-2025-03.pdf",
        doc_date=date(2025, 3, 4),
        value="16-inch",
    )

    [assessment] = history_assessments_for(session, dependency.id)
    assert assessment.outcome == "physical_superseded"
    assert assessment.older_assertion_id == old.id
    assert assessment.newer_assertion_id == new.id
    assert "2025-01-10" in assessment.why
    assert "2025-03-04" in assessment.why

    [resolution] = apply_staleness_resolutions(session, dependency.id)
    assert resolution.outcome == "physical_superseded"
    assert resolution.rule_version == "adr-0061-staleness-v1"
    assert resolution.covers_assertion_id == new.id
    assert resolution.older_assertion_id == old.id
    assert disputes_for(session, dependency.id) == []
    assert session.scalars(select(DisputeHistoryResolution)).one().id == resolution.id
    session.refresh(dependency)
    assert dependency.station_from == "16-inch"
    work = build_work_list(session, project.id, today=date(2025, 3, 5))
    assert not any(
        item.dependency_id == dependency.id
        for item in (*work.immediate, *work.backlog)
    )


def test_newer_source_and_value_oscillation_remain_contested(session, project):
    dependency = _dependency(session, project)
    _claim(
        session,
        dependency,
        filename="matrix-2025-01.pdf",
        doc_date=date(2025, 1, 10),
        value="12-inch",
    )
    _claim(
        session,
        dependency,
        filename="matrix-2025-03.pdf",
        doc_date=date(2025, 3, 4),
        value="16-inch",
    )
    _claim(
        session,
        dependency,
        filename="matrix-2025-04.pdf",
        doc_date=date(2025, 4, 2),
        value="12-inch",
    )

    [assessment] = history_assessments_for(session, dependency.id)
    assert assessment.outcome == "contested"
    assert "oscillated" in assessment.why
    assert apply_staleness_resolutions(session, dependency.id) == []
    [dispute] = disputes_for(session, dependency.id)
    assert dispute.field_name == "station_from"


def test_stale_agreement_becomes_an_amendment_task_not_a_conclusion(session, project):
    dependency = _dependency(session, project, owner="Alex Coordinator")
    old, _ = _claim(
        session,
        dependency,
        filename="executed-agreement.pdf",
        doc_date=date(2025, 1, 10),
        value="12-inch",
        doc_type="agreement",
    )
    new, _ = _claim(
        session,
        dependency,
        filename="matrix-2025-03.pdf",
        doc_date=date(2025, 3, 4),
        value="16-inch",
    )

    [assessment] = history_assessments_for(session, dependency.id)
    assert assessment.outcome == "contractual_amendment"
    [resolution] = apply_staleness_resolutions(session, dependency.id)
    assert resolution.outcome == "contractual_amendment"
    assert resolution.older_assertion_id == old.id
    assert resolution.newer_assertion_id == new.id
    # An agreement mismatch is coordination work, never a mechanical verdict.
    assert disputes_for(session, dependency.id)
    work = build_work_list(session, project.id, today=date(2025, 3, 5))
    [item] = [
        item
        for item in (*work.immediate, *work.backlog)
        if item.dependency_id == dependency.id
    ]
    assert "contractual_amendment" in item.attention_reason_codes
    with pytest.raises(NoSuchDispute, match="amendment follow-up"):
        settle_dispute(
            session,
            dependency.id,
            "station_from",
            value="16-inch",
            principal=HumanPrincipal("local:maria"),
        )


def test_needs_clarification_is_one_atomic_roster_backed_follow_up(
    session, project
):
    dependency = _dependency(session, project)
    _claim(
        session,
        dependency,
        filename="matrix-2025-01.pdf",
        doc_date=date(2025, 1, 10),
        value="12-inch",
    )
    _claim(
        session,
        dependency,
        filename="matrix-2025-03.pdf",
        doc_date=date(2025, 3, 4),
        value="16-inch",
    )
    # Keep this genuinely contested: a manual result without ordering is the
    # human card path, not a history auto-conclusion.
    third = session.scalars(select(Document).order_by(Document.id.desc())).first()
    assert third is not None
    third.doc_date = None
    roster = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:alex",
        display_name="Alex Coordinator",
        can_coordinate=True,
    )
    session.add(roster)
    session.flush()

    result = record_dispute_clarification(
        session,
        dependency.id,
        "station_from",
        roster_entry_id=roster.id,
        next_action="Ask the utility to confirm the pipe size.",
        due_date=date(2025, 4, 1),
        due_date_unknown_reason=None,
        principal=HumanPrincipal("local:maria"),
    )

    assert result.owner_decision_id > 0
    assert result.next_action_decision_id > 0
    assert dependency.internal_owner == "Alex Coordinator"
    assert dependency.next_action == "Ask the utility to confirm the pipe size."
    assert len(session.scalars(select(WorkDecision)).all()) == 2
    assert disputes_for(session, dependency.id)
