"""Public command tests for the atomic guided statement coordination flow."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.external_statements import CitedStatementEvidence, StatementScope, StatementTiming
from corridor.models import (
    Candidate,
    AuditLog,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DocPage,
    Document,
    ExternalOrg,
    Milestone,
    Project,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
    WorkDecision,
    WorkDecisionMilestoneImpact,
)
from corridor.principals import HumanPrincipal
from corridor.statement_coordination import (
    StaleStatementCoordination,
    StatementCoordinationDraft,
    StatementCoordinationRefusal,
    coordinate_statement,
)
from corridor.web.app import app, get_human_principal, get_session


RECORDER = HumanPrincipal("local:statement-coordinator")


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
    project = Project(
        slug="guided-statement-coordination-test",
        name="Guided statement coordination test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    return project


@pytest.fixture
def party(session):
    party = ExternalOrg(name="Kinder Morgan")
    session.add(party)
    session.flush()
    return party


@pytest.fixture
def roster_entry(session, project):
    entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:dana-fields",
        display_name="Dana Fields",
    )
    session.add(entry)
    session.flush()
    return entry


def _document(session, project, name: str, page_text: str) -> Document:
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(f"{project.id}:{name}".encode()).hexdigest(),
        filename=name,
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=page_text))
    session.flush()
    return document


def _candidate(
    session,
    project,
    document,
    *,
    quote: str,
    fields: dict[str, str],
) -> Candidate:
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={"kind": "event", "fields": fields, "citations": [{
            "document_id": document.id,
            "page": 1,
            "quote": quote,
            "verified": True,
        }]},
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="guided-statement-test",
        model="test-model",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version="guided-statement-test",
        candidate_count=1,
        page_errors=0,
        candidates=[candidate],
        model="test-model",
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)
    return candidate


def _dependency(session, project, party, ref_code: str, title: str) -> Dependency:
    dependency = Dependency(
        project_id=project.id,
        ref_code=ref_code,
        dep_type="utility_relocation",
        title=title,
        station_from="6608+70",
        station_to="6616+50",
        external_org_id=party.id,
        status="identified",
    )
    session.add(dependency)
    session.flush()
    return dependency


def _draft(
    candidate,
    party,
    roster_entry,
    *,
    description: str,
    new_timing: StatementTiming,
    evidence: tuple[CitedStatementEvidence, ...],
    previous_timing: StatementTiming | None = None,
    scope: StatementScope | None = None,
    milestone_impact: str | None = None,
    milestone_ids: tuple[int, ...] = (),
    action_due_date: date | None = date(2026, 2, 1),
    action_due_date_unknown_reason: str | None = None,
) -> StatementCoordinationDraft:
    return StatementCoordinationDraft(
        candidate_id=candidate.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        event_date=date(2025, 1, 16),
        description=description,
        new_timing=new_timing,
        previous_timing=previous_timing,
        evidence=evidence,
        scope=scope or StatementScope.unknown(),
        internal_owner_roster_entry_id=roster_entry.id,
        next_action="Confirm the revised completion plan with Kinder Morgan",
        action_due_date=action_due_date,
        action_due_date_unknown_reason=action_due_date_unknown_reason,
        milestone_impact=milestone_impact,
        milestone_ids=milestone_ids,
    )


def test_command_records_the_7296_shape_with_additional_verified_party_context(
    session, project, party, roster_entry
):
    statement_quote = (
        "The March 2026 completion timeline seems unattainable. Propose extending to May 16th."
    )
    party_quote = "Kinder Morgan Management Meeting Highlights"
    document = _document(
        session,
        project,
        "kinder-morgan-minutes.pdf",
        f"{party_quote}\n{statement_quote}",
    )
    candidate = _candidate(
        session,
        project,
        document,
        quote=statement_quote,
        fields={"event_type": "slip", "description": statement_quote},
    )
    dependency = _dependency(session, project, party, "KM-31", "KM 20-inch line")
    original_payload = deepcopy(candidate.payload_json)
    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=statement_quote,
            new_timing=StatementTiming.day("May 16th", date(2026, 5, 16)),
            previous_timing=StatementTiming.month("March 2026", 2026, 3),
            evidence=(
                CitedStatementEvidence(document.id, 1, statement_quote),
                CitedStatementEvidence(document.id, 1, party_quote),
            ),
            milestone_impact="not_yet_known",
        ),
        principal=RECORDER,
    )

    assert candidate.payload_json == original_payload
    assert candidate.state == "accepted"
    assert result.event.event_type == "committed_date_change"
    assert result.event.timing_direction == "later"
    assert result.event.scope_mode == "unknown"
    assert dependency.committed_date is None
    assert result.receipt.candidate_id == candidate.id
    assert result.receipt.dependency_event_id == result.event.id
    assert result.receipt.commitment_lineage_id == result.event.commitment_lineage_id
    assert result.receipt.scope_decision_id is not None
    assert result.receipt.audit_log_id == session.scalar(
        select(AuditLog.id).where(AuditLog.id == result.receipt.audit_log_id)
    )
    assert result.receipt.internal_owner_decision_id == result.internal_owner_decision.id
    assert result.receipt.next_action_decision_id == result.next_action_decision.id
    assert result.receipt.milestone_impact_decision_id == result.milestone_impact_decision.id
    assert session.scalar(
        select(func.count()).select_from(DependencyEventScope).where(
            DependencyEventScope.event_id == result.event.id
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(DependencyEventEvidence).where(
            DependencyEventEvidence.event_id == result.event.id
        )
    ) == 2
    assert session.scalar(
        select(func.count()).select_from(WorkDecision).where(
            WorkDecision.commitment_lineage_id == result.event.commitment_lineage_id
        )
    ) == 3


def test_command_records_the_7129_month_commitment_without_inventing_a_day(
    session, project, roster_entry
):
    party = ExternalOrg(name="Equistar")
    session.add(party)
    session.flush()
    quote = "Equistar to provide a chain of title (Due date of 01/2025)."
    document = _document(session, project, "equistar-minutes.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )

    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=quote,
            new_timing=StatementTiming.month("01/2025", 2025, 1),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
        ),
        principal=RECORDER,
    )

    assert result.event.event_type == "commitment"
    assert result.event.timing_direction is None
    assert result.event.new_timing.precision == "month"
    assert result.event.new_timing.start_date == date(2025, 1, 1)
    assert result.event.new_timing.end_date == date(2025, 1, 31)
    assert result.milestone_impact_decision is None


@pytest.mark.parametrize(
    ("scope_mode", "expected_count"),
    (("one", 1), ("selected", 2), ("all_active", 2), ("unknown", 0)),
)
def test_command_records_each_explicit_scope_mode(
    session, project, party, roster_entry, scope_mode, expected_count
):
    quote = "Kinder Morgan will provide the relocation schedule by June 1, 2026."
    document = _document(session, project, f"scope-{scope_mode}.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    first = _dependency(session, project, party, f"{scope_mode}-1", "First KM line")
    second = _dependency(session, project, party, f"{scope_mode}-2", "Second KM line")
    scope = {
        "one": StatementScope.selected((first.id,)),
        "selected": StatementScope.selected((first.id, second.id)),
        "all_active": StatementScope.all_active(),
        "unknown": StatementScope.unknown(),
    }[scope_mode]

    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
            scope=scope,
        ),
        principal=RECORDER,
    )

    assert result.event.scope_mode == scope.mode
    assert session.scalar(
        select(func.count()).select_from(DependencyEventScope).where(
            DependencyEventScope.event_id == result.event.id
        )
    ) == expected_count


def test_command_rolls_back_every_result_when_a_late_milestone_refuses(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan moved completion from May 1, 2026 to June 1, 2026."
    document = _document(session, project, "late-refusal.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    other_project = Project(slug="other-guided-project", name="Other", is_synthetic=True)
    session.add(other_project)
    session.flush()
    foreign_milestone = Milestone(
        project_id=other_project.id, code="OTHER", name="Other milestone"
    )
    session.add(foreign_milestone)
    session.flush()

    with pytest.raises(StatementCoordinationRefusal, match="registered project Milestones"):
        coordinate_statement(
            session,
            _draft(
                candidate,
                party,
                roster_entry,
                description=quote,
                new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
                previous_timing=StatementTiming.day("May 1, 2026", date(2026, 5, 1)),
                evidence=(CitedStatementEvidence(document.id, 1, quote),),
                milestone_impact="affects",
                milestone_ids=(foreign_milestone.id,),
            ),
            principal=RECORDER,
        )

    assert candidate.state == "pending"
    assert session.scalar(select(func.count()).select_from(DependencyEvent)) == 0
    assert session.scalar(select(func.count()).select_from(StatementCoordinationReceipt)) == 0
    assert session.scalar(select(func.count()).select_from(WorkDecision)) == 0


def test_command_refuses_stale_resubmission_without_partial_second_save(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, "stale-save.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    draft = _draft(
        candidate,
        party,
        roster_entry,
        description=quote,
        new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
        evidence=(CitedStatementEvidence(document.id, 1, quote),),
    )
    coordinate_statement(session, draft, principal=RECORDER)

    with pytest.raises(StaleStatementCoordination, match="already accepted"):
        coordinate_statement(session, draft, principal=RECORDER)

    assert session.scalar(select(func.count()).select_from(DependencyEvent)) == 1
    assert session.scalar(select(func.count()).select_from(StatementCoordinationReceipt)) == 1


def test_command_refuses_party_or_timing_that_the_verified_evidence_does_not_support(
    session, project, party, roster_entry
):
    quote = "The completion timeline is unattainable. Propose extending to May 16th."
    document = _document(session, project, "missing-party-context.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "slip", "description": quote},
    )

    with pytest.raises(StatementCoordinationRefusal, match="stated External Party"):
        coordinate_statement(
            session,
            _draft(
                candidate,
                party,
                roster_entry,
                description=quote,
                new_timing=StatementTiming.day("May 16th", date(2026, 5, 16)),
                previous_timing=StatementTiming.month("March 2026", 2026, 3),
                evidence=(CitedStatementEvidence(document.id, 1, quote),),
                milestone_impact="does_not_affect",
            ),
            principal=RECORDER,
        )

    assert candidate.state == "pending"
    assert session.scalar(select(func.count()).select_from(DependencyEvent)) == 0


def test_command_requires_a_project_roster_selection_and_preserves_unknown_due_reason(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, "roster-and-reason.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    draft = _draft(
        candidate,
        party,
        roster_entry,
        description=quote,
        new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
        evidence=(CitedStatementEvidence(document.id, 1, quote),),
        action_due_date=None,
        action_due_date_unknown_reason="awaiting_external_information",
    )

    with pytest.raises(StatementCoordinationRefusal, match="active roster"):
        coordinate_statement(
            session,
            replace(draft, internal_owner_roster_entry_id=9999999),
            principal=RECORDER,
        )

    result = coordinate_statement(session, draft, principal=RECORDER)
    assert result.next_action_decision.action_due_date_reason == "awaiting_external_information"


def test_command_derives_unknown_direction_when_supported_timings_do_not_order(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan may complete in early summer instead of late spring."
    document = _document(session, project, "unknown-direction.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "slip", "description": quote},
    )

    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=quote,
            new_timing=StatementTiming.approximate("early summer"),
            previous_timing=StatementTiming.approximate("late spring"),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
            milestone_impact="does_not_affect",
        ),
        principal=RECORDER,
    )

    assert result.event.event_type == "committed_date_change"
    assert result.event.timing_direction == "unknown"


def test_command_records_an_affecting_milestone_with_its_exact_registered_link(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan moved completion from May 1, 2026 to June 1, 2026."
    document = _document(session, project, "affecting-milestone.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "slip", "description": quote},
    )
    milestone = Milestone(
        project_id=project.id, code="RELO-CONSTR", name="Relocation construction"
    )
    session.add(milestone)
    session.flush()

    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            previous_timing=StatementTiming.day("May 1, 2026", date(2026, 5, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
            milestone_impact="affects",
            milestone_ids=(milestone.id,),
        ),
        principal=RECORDER,
    )

    assert result.milestone_impact_decision is not None
    assert session.scalar(
        select(WorkDecisionMilestoneImpact.milestone_id).where(
            WorkDecisionMilestoneImpact.work_decision_id
            == result.milestone_impact_decision.id
        )
    ) == milestone.id


def test_http_flow_renders_verified_context_and_delegates_to_the_atomic_command(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, "http-guided-flow.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    _dependency(session, project, party, "HTTP-1", "Kinder Morgan crossing")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            screen = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
            assert screen.status_code == 200
            assert quote in screen.text
            assert "Scope not yet known" in screen.text
            assert "Dana Fields" in screen.text
            assert 'name="scope_mode" value="unknown" required' in screen.text
            assert 'data-dependency-party="' in screen.text
            assert "Kinder Morgan crossing · Kinder Morgan" in screen.text

            response = client.post(
                f"/statements/{project.slug}/{candidate.id}/coordinate",
                data={
                    "affected_external_org_id": str(party.id),
                    "stated_party": party.name,
                    "stated_external_org_id": str(party.id),
                    "event_date": "2025-01-16",
                    "description": quote,
                    "new_timing_text": "June 1, 2026",
                    "new_timing_precision": "day",
                    "new_timing_start_date": "2026-06-01",
                    "new_timing_end_date": "2026-06-01",
                    "scope_mode": "unknown",
                    "internal_owner_roster_entry_id": str(roster_entry.id),
                    "next_action": "Confirm the June plan",
                    "action_due_date": "2026-02-01",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            saved = client.get(response.headers["location"])
            assert "Coordination Plan saved" in saved.text
            assert "Commitment" in saved.text
            assert "Dana Fields" in saved.text
            assert "Attention Reason" in saved.text
            stale = client.post(
                f"/statements/{project.slug}/{candidate.id}/coordinate",
                data={
                    "affected_external_org_id": str(party.id),
                    "stated_party": party.name,
                    "stated_external_org_id": str(party.id),
                    "description": quote,
                    "new_timing_text": "June 1, 2026",
                    "new_timing_precision": "day",
                    "new_timing_start_date": "2026-06-01",
                    "new_timing_end_date": "2026-06-01",
                    "scope_mode": "unknown",
                    "internal_owner_roster_entry_id": str(roster_entry.id),
                    "next_action": "Confirm the June plan",
                    "action_due_date": "2026-02-01",
                },
            )
            assert stale.status_code == 409
            assert "Coordination Plan saved" in stale.text
    finally:
        app.dependency_overrides.clear()
