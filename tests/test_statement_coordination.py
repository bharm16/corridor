"""Public command tests for the atomic guided statement coordination flow."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import base64
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor import audit
from corridor.db import Session, engine
from corridor.dependency_events import (
    current_dependency_statements,
    current_statement_evidence_memberships,
)
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.external_statements import CitedStatementEvidence, StatementScope, StatementTiming
from corridor.models import (
    Candidate,
    CandidateDisposition,
    AuditLog,
    CommitmentLineage,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DocPage,
    Document,
    ExternalOrg,
    Milestone,
    Project,
    ProjectRosterEntry,
    ReportRun,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
    StatementCoordinationReversalEffect,
    WorkDecision,
    WorkDecisionMilestoneImpact,
)
from corridor.principals import HumanPrincipal
from corridor.statement_coordination import (
    StaleStatementCoordination,
    StatementCoordinationDraft,
    StatementCoordinationRefusal,
    StatementCoordinationUndoRefusal,
    coordinate_statement,
    correct_statement_facts,
    correct_statement_scope,
    mark_statement_not_relevant,
    restore_statement_not_relevant,
    StatementFactCorrectionDraft,
    StatementScopeCorrection,
    undo_statement_coordination,
)
from corridor.work_decisions import (
    CoordinationSubject,
    current_internal_owner_decision,
    current_next_action_decision,
    set_next_action,
)
from corridor.web.app import (
    _supporting_statement_evidence,
    app,
    get_human_principal,
    get_session,
)


RECORDER = HumanPrincipal("local:statement-coordinator")
_PAGE_IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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


def _register_page_image(session, document: Document, image_path) -> None:
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == 1,
        )
    )
    image_path.write_bytes(_PAGE_IMAGE_BYTES)
    page.image_path = str(image_path)


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
    assert "party_resolution" not in result.receipt.accepted_facts_json
    coordination_audit = session.get(AuditLog, result.receipt.audit_log_id)
    assert "party_resolution" not in coordination_audit.after_json
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
    party = session.scalar(select(ExternalOrg).where(ExternalOrg.name == "Equistar"))
    if party is None:
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
    scoped_dependency_ids = set(
        session.scalars(
            select(DependencyEventScope.dependency_id).where(
                DependencyEventScope.event_id == result.event.id
            )
        )
    )
    expected_scope_ids = {
        "one": {first.id},
        "selected": {first.id, second.id},
        "all_active": {first.id, second.id},
        "unknown": set(),
    }[scope_mode]
    assert scoped_dependency_ids == expected_scope_ids
    assert session.scalar(
        select(func.count()).select_from(StatementCoordinationReceipt).where(
            StatementCoordinationReceipt.candidate_id == candidate.id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(DependencyEvent).where(
            DependencyEvent.commitment_lineage_id == result.event.commitment_lineage_id
        )
    ) == 1


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


def test_undo_reverses_exactly_one_guided_save_without_deleting_its_history(
    session, project, party, roster_entry
):
    """Undo is a compensating command over the receipt, never a row delete."""
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, "undo-guided-save.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    dependency = _dependency(session, project, party, "UNDO-1", "KM crossing")
    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
            scope=StatementScope.selected((dependency.id,)),
        ),
        principal=RECORDER,
    )

    reversal = undo_statement_coordination(session, result.receipt.id, principal=RECORDER)

    session.refresh(candidate)
    assert candidate.state == "pending"
    assert session.get(DependencyEvent, result.event.id) is not None
    assert session.get(StatementCoordinationReceipt, result.receipt.id) is not None
    assert session.scalar(
        select(CandidateDisposition).where(
            CandidateDisposition.candidate_id == candidate.id,
            CandidateDisposition.disposition == "accepted",
        )
    ) is not None
    assert reversal.receipt_id == result.receipt.id
    assert {
        (effect.effect_kind, effect.target_id)
        for effect in session.scalars(
            select(StatementCoordinationReversalEffect).where(
                StatementCoordinationReversalEffect.reversal_id == reversal.id
            )
        )
    } >= {
        ("statement", result.event.id),
        ("scope_decision", result.receipt.scope_decision_id),
        ("work_decision", result.internal_owner_decision.id),
        ("work_decision", result.next_action_decision.id),
        ("candidate_disposition", result.receipt.candidate_disposition_id),
        ("grouping_receipt", result.receipt.id),
    }
    subject = CoordinationSubject.statement(result.event.commitment_lineage_id)
    assert current_internal_owner_decision(session, subject) is None
    assert current_next_action_decision(session, subject) is None
    assert session.scalar(select(func.count()).select_from(DependencyEventEvidence)) == 1
    assert current_dependency_statements(session, (dependency.id,))[dependency.id].event is None

    resaved = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
        ),
        principal=RECORDER,
    )
    assert resaved.receipt.id != result.receipt.id


def test_undo_refuses_atomically_when_later_work_depends_on_the_save(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, "undo-dependent-work.pdf", quote)
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
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
        ),
        principal=RECORDER,
    )
    set_next_action(
        session,
        CoordinationSubject.statement(result.event.commitment_lineage_id),
        "Ask Kinder Morgan for the June construction sequence",
        due_date=date(2026, 2, 2),
        principal=RECORDER,
    )

    with pytest.raises(StatementCoordinationRefusal, match="Correct"):
        undo_statement_coordination(session, result.receipt.id, principal=RECORDER)

    session.refresh(candidate)
    assert candidate.state == "accepted"
    assert session.scalar(
        select(StatementCoordinationReversal).where(
            StatementCoordinationReversal.receipt_id == result.receipt.id
        )
    ) is None
    assert current_next_action_decision(
        session, CoordinationSubject.statement(result.event.commitment_lineage_id)
    ).after_value is not None


def test_correct_scope_appends_one_scope_decision_without_rewriting_the_statement_or_plan(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, "correct-statement-scope.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    dependency = _dependency(session, project, party, "CORRECT-1", "KM crossing")
    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
        ),
        principal=RECORDER,
    )

    correction = correct_statement_scope(
        session,
        StatementScopeCorrection(
            candidate_id=candidate.id,
            event_id=result.event.id,
            expected_scope_decision_id=result.receipt.scope_decision_id,
            scope=StatementScope.selected((dependency.id,)),
        ),
        principal=RECORDER,
    )

    assert correction.event_id == result.event.id
    assert correction.supersedes_scope_decision_id == result.receipt.scope_decision_id
    assert session.get(DependencyEvent, result.event.id).description == quote
    assert current_internal_owner_decision(
        session, CoordinationSubject.statement(result.event.commitment_lineage_id)
    ).id == result.internal_owner_decision.id
    assert session.scalar(
        select(DependencyEventScope.dependency_id).where(
            DependencyEventScope.scope_decision_id == correction.id
        )
    ) == dependency.id


def test_correcting_statement_facts_appends_a_successor_and_marks_its_plan_for_review(
    session, project, party, roster_entry
):
    original_quote = "Kinder Morgan will complete relocation by June 1, 2026."
    corrected_quote = "Kinder Morgan will complete relocation by July 1, 2026."
    document = _document(
        session,
        project,
        "correct-statement-facts.pdf",
        f"{original_quote}\n{corrected_quote}",
    )
    candidate = _candidate(
        session,
        project,
        document,
        quote=original_quote,
        fields={"event_type": "commitment", "description": original_quote},
    )
    dependency = _dependency(session, project, party, "FACT-CORRECT-1", "KM crossing")
    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=original_quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, original_quote),),
            scope=StatementScope.selected((dependency.id,)),
        ),
        principal=RECORDER,
    )

    successor = correct_statement_facts(
        session,
        StatementFactCorrectionDraft(
            candidate_id=candidate.id,
            expected_statement_event_id=result.event.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            event_date=date(2025, 1, 17),
            description=corrected_quote,
            new_timing=StatementTiming.day("July 1, 2026", date(2026, 7, 1)),
            previous_timing=None,
            evidence=(CitedStatementEvidence(document.id, 1, corrected_quote),),
        ),
        principal=RECORDER,
    )

    lineage = session.get(CommitmentLineage, result.event.commitment_lineage_id)
    assert successor.commitment_lineage_id == result.event.commitment_lineage_id
    assert successor.supersedes_event_id == result.event.id
    assert successor.new_timing.text == "July 1, 2026"
    assert lineage.plan_needs_review is True
    assert current_internal_owner_decision(
        session, CoordinationSubject.statement(lineage.id)
    ).id == result.internal_owner_decision.id
    memberships = current_statement_evidence_memberships(
        session, (dependency.id,)
    ).for_dependency(dependency.id)
    assert [(member.event_id, member.evidence_link.quote) for member in memberships] == [
        (successor.id, corrected_quote)
    ]


def test_fact_correction_can_bind_new_source_words_to_the_same_selected_party(
    session, project, roster_entry
):
    party = ExternalOrg(name="Kinder Morgan Tejas Pipeline")
    session.add(party)
    session.flush()
    original_quote = (
        "Kinder Morgan Tejas Pipeline will complete relocation by June 1, 2026."
    )
    corrected_quote = "Kinder Morgan will complete relocation by July 1, 2026."
    document = _document(
        session,
        project,
        "correct-statement-local-party-resolution.pdf",
        f"{original_quote}\n{corrected_quote}",
    )
    candidate = _candidate(
        session,
        project,
        document,
        quote=original_quote,
        fields={
            "event_type": "commitment",
            "description": original_quote,
            "external_org": "Kinder Morgan",
        },
    )
    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=original_quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, original_quote),),
        ),
        principal=RECORDER,
    )

    successor = correct_statement_facts(
        session,
        StatementFactCorrectionDraft(
            candidate_id=candidate.id,
            expected_statement_event_id=result.event.id,
            affected_external_org_id=party.id,
            stated_party="Kinder Morgan",
            stated_external_org_id=party.id,
            event_date=date(2025, 1, 17),
            description=corrected_quote,
            new_timing=StatementTiming.day("July 1, 2026", date(2026, 7, 1)),
            previous_timing=None,
            evidence=(CitedStatementEvidence(document.id, 1, corrected_quote),),
        ),
        principal=RECORDER,
    )

    assert successor.stated_party == "Kinder Morgan"
    assert successor.stated_external_org_id == party.id
    assert party.aliases == []
    lineage = session.get(CommitmentLineage, result.event.commitment_lineage_id)
    assert lineage.plan_needs_review is True
    correction_audit = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == audit.CORRECT_STATEMENT_FACTS,
            AuditLog.entity_id == result.event.commitment_lineage_id,
        )
        .order_by(AuditLog.id.desc())
    )
    assert correction_audit.after_json["party_resolution"]["mode"] == (
        "guided_evidence_bound"
    )


@pytest.mark.parametrize(
    ("dependent_kind", "message"),
    (
        ("closure", "later closure"),
        ("report", "Report publication"),
        ("audit", "later recorded act"),
        ("dependency_audit", "later recorded act"),
        ("unrelated_audit", None),
    ),
)
def test_undo_refuses_after_a_scoped_closure_or_published_report(
    session, project, party, roster_entry, dependent_kind, message
):
    """Only later acts that use an exact Save result block its Undo."""
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, f"undo-{dependent_kind}.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    dependency = _dependency(session, project, party, f"UNDO-{dependent_kind}", "KM crossing")
    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
            scope=StatementScope.selected((dependency.id,)),
        ),
        principal=RECORDER,
    )

    if dependent_kind == "closure":
        closure = DependencyEvent(
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_external_org_id=party.id,
            scope_mode="selected",
            event_type="closure",
            event_date=date(2026, 2, 1),
            description="Relocation complete.",
            created_by="local:closure-reviewer",
        )
        session.add(closure)
        session.flush()
        closure_scope = session.scalar(
            select(DependencyEventScopeDecision).where(
                DependencyEventScopeDecision.event_id == closure.id
            )
        )
        session.add(
            DependencyEventScope(
                event_id=closure.id,
                scope_decision_id=closure_scope.id,
                dependency_id=dependency.id,
                recorded_by="local:closure-reviewer",
            )
        )
    elif dependent_kind == "report":
        session.add(
            ReportRun(
                project_id=project.id,
                ruleset_version="test-ruleset",
                snapshot_json={"dependencies": {dependency.ref_code: {"id": dependency.id}}},
            )
        )
    elif dependent_kind == "audit":
        audit.record(
            session,
            principal=RECORDER,
            action=audit.CORRECT_STATEMENT_SCOPE,
            entity_type=audit.COMMITMENT_LINEAGE,
            entity_id=result.event.commitment_lineage_id,
            before={"scope_decision_id": result.receipt.scope_decision_id},
            after={"scope_decision_id": result.receipt.scope_decision_id},
        )
    elif dependent_kind == "dependency_audit":
        audit.record(
            session,
            principal=RECORDER,
            action=audit.SET_NEXT_ACTION,
            entity_type=audit.DEPENDENCY,
            entity_id=dependency.id,
            before={"dependency_id": dependency.id},
            after={
                "dependency_id": dependency.id,
                "scope_decision_id": result.receipt.scope_decision_id,
            },
        )
    else:
        audit.record(
            session,
            principal=RECORDER,
            action=audit.EDIT_CANDIDATE,
            entity_type=audit.CANDIDATE,
            entity_id=candidate.id,
            before={"candidate_id": candidate.id},
            after={"candidate_id": candidate.id, "note": "unrelated review"},
        )
    session.flush()

    if message is not None:
        with pytest.raises(StatementCoordinationUndoRefusal, match=message):
            undo_statement_coordination(session, result.receipt.id, principal=RECORDER)
        assert session.scalar(
            select(StatementCoordinationReversal.id).where(
                StatementCoordinationReversal.receipt_id == result.receipt.id
            )
        ) is None
    else:
        assert undo_statement_coordination(
            session, result.receipt.id, principal=RECORDER
        )


def test_factual_correction_carries_an_all_active_snapshot_without_reselecting_scope(
    session, project, party, roster_entry
):
    original_quote = "Kinder Morgan will complete relocation by June 1, 2026."
    corrected_quote = "Kinder Morgan will complete relocation by July 1, 2026."
    document = _document(
        session,
        project,
        "correct-all-active-scope.pdf",
        f"{original_quote}\n{corrected_quote}",
    )
    candidate = _candidate(
        session,
        project,
        document,
        quote=original_quote,
        fields={"event_type": "commitment", "description": original_quote},
    )
    original_dependency = _dependency(session, project, party, "SCOPE-1", "First KM line")
    result = coordinate_statement(
        session,
        _draft(
            candidate,
            party,
            roster_entry,
            description=original_quote,
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, original_quote),),
            scope=StatementScope.all_active(),
        ),
        principal=RECORDER,
    )
    later_dependency = _dependency(session, project, party, "SCOPE-2", "Later KM line")

    successor = correct_statement_facts(
        session,
        StatementFactCorrectionDraft(
            candidate_id=candidate.id,
            expected_statement_event_id=result.event.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            event_date=date(2025, 1, 17),
            description=corrected_quote,
            new_timing=StatementTiming.day("July 1, 2026", date(2026, 7, 1)),
            previous_timing=None,
            evidence=(CitedStatementEvidence(document.id, 1, corrected_quote),),
        ),
        principal=RECORDER,
    )

    carried_scope = session.scalar(
        select(DependencyEventScopeDecision).where(
            DependencyEventScopeDecision.event_id == successor.id
        )
    )
    assert carried_scope.scope_mode == "carried_forward"
    assert session.scalars(
        select(DependencyEventScope.dependency_id)
        .where(DependencyEventScope.scope_decision_id == carried_scope.id)
        .order_by(DependencyEventScope.dependency_id)
    ).all() == [original_dependency.id]
    assert later_dependency.id != original_dependency.id


def test_not_relevant_is_reasoned_reversible_and_never_creates_a_statement_or_plan(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan discussed traffic control in the project meeting."
    document = _document(session, project, "not-relevant.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "mention", "description": quote},
    )

    with pytest.raises(StatementCoordinationRefusal, match="confirmation"):
        mark_statement_not_relevant(
            session,
            candidate.id,
            reason="outside_project_scope",
            confirmed=False,
            principal=RECORDER,
        )
    disposition = mark_statement_not_relevant(
        session,
        candidate.id,
        reason="outside_project_scope",
        confirmed=True,
        principal=RECORDER,
    )

    session.refresh(candidate)
    assert candidate.state == "rejected"
    assert disposition.reason == "outside_project_scope"
    assert session.scalar(select(func.count()).select_from(DependencyEvent)) == 0
    assert session.scalar(select(func.count()).select_from(StatementCoordinationReceipt)) == 0
    reversal = restore_statement_not_relevant(session, disposition.id, principal=RECORDER)

    session.refresh(candidate)
    assert candidate.state == "pending"
    assert reversal.candidate_disposition_id == disposition.id
    assert session.get(CandidateDisposition, disposition.id).reason == "outside_project_scope"


def test_http_undo_correct_and_not_relevant_delegate_to_append_only_commands(
    session, project, party, roster_entry, tmp_path
):
    """The browser exposes lifecycle commands but never rebuilds their logic."""
    undo_quote = "Kinder Morgan will complete relocation by June 1, 2026."
    undo_document = _document(session, project, "http-undo.pdf", undo_quote)
    _register_page_image(session, undo_document, tmp_path / "http-undo.png")
    undo_candidate = _candidate(
        session,
        project,
        undo_document,
        quote=undo_quote,
        fields={"event_type": "commitment", "description": undo_quote},
    )
    correct_quote = "Kinder Morgan will complete relocation by July 1, 2026."
    correct_document = _document(session, project, "http-correct.pdf", correct_quote)
    _register_page_image(session, correct_document, tmp_path / "http-correct.png")
    correct_candidate = _candidate(
        session,
        project,
        correct_document,
        quote=correct_quote,
        fields={"event_type": "commitment", "description": correct_quote},
    )
    wrong_target_quote = "Kinder Morgan will complete relocation by August 1, 2026."
    wrong_target_document = _document(
        session, project, "http-wrong-correction-target.pdf", wrong_target_quote
    )
    _register_page_image(
        session,
        wrong_target_document,
        tmp_path / "http-wrong-correction-target.png",
    )
    wrong_target_candidate = _candidate(
        session,
        project,
        wrong_target_document,
        quote=wrong_target_quote,
        fields={"event_type": "commitment", "description": wrong_target_quote},
    )
    dependency = _dependency(session, project, party, "HTTP-CORRECT", "KM crossing")
    irrelevant_quote = "Kinder Morgan discussed traffic control in the project meeting."
    irrelevant_document = _document(session, project, "http-not-relevant.pdf", irrelevant_quote)
    irrelevant_candidate = _candidate(
        session,
        project,
        irrelevant_document,
        quote=irrelevant_quote,
        fields={"event_type": "mention", "description": irrelevant_quote},
    )

    def save(candidate, quote, timing_date):
        return {
            "affected_external_org_id": str(party.id),
            "stated_party": party.name,
            "stated_external_org_id": str(party.id),
            "event_date": "2025-01-16",
            "description": quote,
            "new_timing_text": quote.split(" by ")[-1].rstrip("."),
            "new_timing_precision": "day",
            "new_timing_start_date": timing_date,
            "new_timing_end_date": timing_date,
            "scope_mode": "unknown",
            "internal_owner_roster_entry_id": str(roster_entry.id),
            "next_action": "Confirm the plan with Kinder Morgan",
            "action_due_date": "2026-02-01",
        }

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            saved_undo = client.post(
                f"/statements/{project.slug}/{undo_candidate.id}/coordinate",
                data=save(undo_candidate, undo_quote, "2026-06-01"),
                follow_redirects=False,
            )
            assert saved_undo.status_code == 303
            undo_receipt = session.scalar(
                select(StatementCoordinationReceipt).where(
                    StatementCoordinationReceipt.candidate_id == undo_candidate.id
                )
            )
            undone = client.post(
                f"/statements/{project.slug}/{undo_candidate.id}/coordination/{undo_receipt.id}/undo",
                follow_redirects=False,
            )
            assert undone.status_code == 303
            assert "Undid guided Save" in client.get(undone.headers["location"]).text

            saved_correct = client.post(
                f"/statements/{project.slug}/{correct_candidate.id}/coordinate",
                data=save(correct_candidate, correct_quote, "2026-07-01"),
                follow_redirects=False,
            )
            assert saved_correct.status_code == 303
            correct_receipt = session.scalar(
                select(StatementCoordinationReceipt).where(
                    StatementCoordinationReceipt.candidate_id == correct_candidate.id
                )
            )
            corrected = client.post(
                f"/statements/{project.slug}/{correct_candidate.id}/correct/scope",
                data={
                    "expected_statement_event_id": str(correct_receipt.dependency_event_id),
                    "expected_scope_decision_id": str(correct_receipt.scope_decision_id),
                    "scope_mode": "selected",
                    "dependency_id": str(dependency.id),
                },
                follow_redirects=False,
            )
            assert corrected.status_code == 303
            assert session.scalar(
                select(DependencyEventScope).where(
                    DependencyEventScope.event_id == correct_receipt.dependency_event_id,
                    DependencyEventScope.dependency_id == dependency.id,
                )
            ) is not None

            saved_wrong_target = client.post(
                f"/statements/{project.slug}/{wrong_target_candidate.id}/coordinate",
                data=save(wrong_target_candidate, wrong_target_quote, "2026-08-01"),
                follow_redirects=False,
            )
            assert saved_wrong_target.status_code == 303
            wrong_target = client.post(
                f"/statements/{project.slug}/{wrong_target_candidate.id}/correct/scope",
                data={
                    "expected_statement_event_id": str(
                        correct_receipt.dependency_event_id
                    ),
                    "expected_scope_decision_id": str(
                        correct_receipt.scope_decision_id
                    ),
                    "scope_mode": "selected",
                    "dependency_id": str(dependency.id),
                },
                follow_redirects=False,
            )
            assert wrong_target.status_code == 409
            assert "does not own this correction target" in wrong_target.text
            assert session.scalar(
                select(func.count())
                .select_from(DependencyEventScopeDecision)
                .where(
                    DependencyEventScopeDecision.event_id
                    == correct_receipt.dependency_event_id
                )
            ) == 2

            marked = client.post(
                f"/statements/{project.slug}/{irrelevant_candidate.id}/not-relevant",
                data={"reason": "outside_project_scope", "confirmed": "yes"},
                follow_redirects=False,
            )
            assert marked.status_code == 303
            disposition = session.scalar(
                select(CandidateDisposition).where(
                    CandidateDisposition.candidate_id == irrelevant_candidate.id
                )
            )
            marked_page = client.get(marked.headers["location"])
            assert "Marked Not Relevant" in marked_page.text
            restored = client.post(
                f"/statements/{project.slug}/{irrelevant_candidate.id}/not-relevant/{disposition.id}/restore",
                follow_redirects=False,
            )
            assert restored.status_code == 303
            session.refresh(irrelevant_candidate)
            assert irrelevant_candidate.state == "pending"
    finally:
        app.dependency_overrides.clear()


def test_http_screen_shows_the_registered_source_page_without_accepting_party_suggestions(
    session, project, party, roster_entry, tmp_path
):
    quote = "The March 2026 completion timeline seems unattainable."
    page_text = (
        "Kinder Morgan Management Meeting Highlights\n"
        "Relocation schedule discussion\n"
        f"{quote}\n"
        "Propose extending completion to May 16th."
    )
    document = _document(session, project, "http-page-context.pdf", page_text)
    page_image = tmp_path / "registered-page.png"
    _register_page_image(session, document, page_image)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={
            "event_type": "slip",
            "description": quote,
            "external_org": party.name,
        },
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            screen = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
            rendered_page = client.get(f"/page-image/{document.id}/1")

        assert screen.status_code == 200
        assert f'src="/page-image/{document.id}/1"' in screen.text
        assert 'alt="Registered source page 1"' in screen.text
        assert rendered_page.status_code == 200
        assert rendered_page.headers["content-type"] == "image/png"
        assert rendered_page.content == page_image.read_bytes()
        assert "Complete registered page context" in screen.text
        assert "Kinder Morgan Management Meeting Highlights" in screen.text
        assert "Relocation schedule discussion" in screen.text
        assert "Propose extending completion to May 16th." in screen.text
        assert "Extracted context suggestion — not yet accepted" in screen.text
        assert f'<option value="{party.id}">{party.name}</option>' in screen.text
        assert f'<option value="{party.id}" selected>' not in screen.text
        assert 'id="stated-party-words" name="stated_party" value=""' in screen.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("image_state", "text_source"),
    (("absent", "text_layer"), ("missing", "ocr")),
)
def test_non_cell_page_without_an_available_image_is_disabled_and_refused(
    session, project, tmp_path, image_state, text_source
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    diagnostic_context = "Extracted diagnostic context is not visual Evidence."
    document = _document(
        session,
        project,
        f"unavailable-{image_state}-page.pdf",
        f"{quote}\n{diagnostic_context}",
    )
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == 1,
        )
    )
    page.text_source = text_source
    if image_state == "missing":
        page.image_path = str(tmp_path / "missing-rendered-page.png")
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            screen = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )

        assert screen.status_code == 200
        assert "Rendered source page unavailable" in screen.text
        assert "retained for diagnosis only" in screen.text
        assert diagnostic_context in screen.text
        assert (
            'name="supporting_page_index" value="0" disabled' in screen.text
        )
        with pytest.raises(
            StatementCoordinationRefusal,
            match="Save unavailable until every Candidate Evidence page",
        ):
            _supporting_statement_evidence(
                session,
                candidate,
                {
                    "supporting_page_index": "0",
                    "supporting_quote": quote,
                },
            )
    finally:
        app.dependency_overrides.clear()


def test_http_save_refuses_original_pdf_evidence_without_a_rendered_image(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(
        session,
        project,
        "original-candidate-page-without-image.pdf",
        quote,
    )
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            screen = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
            response = client.post(
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
                follow_redirects=False,
            )

        assert screen.status_code == 200
        assert "Save unavailable until every Candidate Evidence page" in screen.text
        assert (
            '<button type="submit" disabled>Save statement and Coordination Plan</button>'
            in screen.text
        )
        assert response.status_code == 400
        assert "Save unavailable until every Candidate Evidence page" in response.text
        assert candidate.state == "pending"
        assert session.scalar(
            select(func.count())
            .select_from(DependencyEvent)
            .where(DependencyEvent.project_id == project.id)
        ) == 0
    finally:
        app.dependency_overrides.clear()


def test_http_flow_binds_an_additional_quote_to_the_visible_registered_page(
    session, project, party, roster_entry
):
    party_quote = "Kinder Morgan Management Meeting Highlights"
    statement_quote = "Will complete relocation by June 1, 2026."
    document = _document(
        session,
        project,
        "http-page-bound-support.xlsx",
        f"{party_quote}\n{statement_quote}",
    )
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == 1,
        )
    )
    page.text_source = "cells"
    candidate = _candidate(
        session,
        project,
        document,
        quote=statement_quote,
        fields={
            "event_type": "commitment",
            "description": statement_quote,
            "external_org": party.name,
        },
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            screen = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
            assert screen.status_code == 200
            assert party_quote in screen.text
            assert "read from registered cells" in screen.text
            assert f'src="/page-image/{document.id}/1"' not in screen.text
            assert 'name="supporting_page_index" value="0"' in screen.text
            assert 'name="supporting_document_id"' not in screen.text
            assert 'name="supporting_page_no"' not in screen.text

            response = client.post(
                f"/statements/{project.slug}/{candidate.id}/coordinate",
                data={
                    "affected_external_org_id": str(party.id),
                    "stated_party": party.name,
                    "stated_external_org_id": str(party.id),
                    "description": statement_quote,
                    "new_timing_text": "June 1, 2026",
                    "new_timing_precision": "day",
                    "new_timing_start_date": "2026-06-01",
                    "new_timing_end_date": "2026-06-01",
                    "supporting_page_index": "0",
                    "supporting_quote": party_quote,
                    "scope_mode": "unknown",
                    "internal_owner_roster_entry_id": str(roster_entry.id),
                    "next_action": "Confirm the June plan",
                    "action_due_date": "2026-02-01",
                },
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert candidate.state == "accepted"
        assert session.scalar(
            select(func.count())
            .select_from(DependencyEventEvidence)
            .join(DependencyEvent, DependencyEvent.id == DependencyEventEvidence.event_id)
            .where(DependencyEvent.project_id == project.id)
        ) == 2
    finally:
        app.dependency_overrides.clear()


def test_http_guided_save_binds_source_party_words_to_the_selected_party_without_registering_an_alias(
    session, project, roster_entry, tmp_path
):
    """The coordinator may make one Evidence-bound attribution, not a registry edit."""
    canonical_party = ExternalOrg(name="Kinder Morgan Tejas Pipeline")
    session.add(canonical_party)
    session.flush()
    party_quote = "Kinder Morgan Management Meeting Highlights"
    statement_quote = (
        "The March 2026 completion timeline seems unattainable. "
        "Propose extending to May 16th."
    )
    document = _document(
        session,
        project,
        "http-statement-local-party-resolution.pdf",
        f"{party_quote}\n{statement_quote}",
    )
    _register_page_image(session, document, tmp_path / "statement-local-party.png")
    candidate = _candidate(
        session,
        project,
        document,
        quote=statement_quote,
        fields={
            "event_type": "slip",
            "description": statement_quote,
            "external_org": "Kinder Morgan",
        },
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            screen = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
            response = client.post(
                f"/statements/{project.slug}/{candidate.id}/coordinate",
                data={
                    "affected_external_org_id": str(canonical_party.id),
                    "stated_party": "Kinder Morgan",
                    "stated_external_org_id": str(canonical_party.id),
                    "description": statement_quote,
                    "new_timing_text": "May 16th",
                    "new_timing_precision": "day",
                    "new_timing_start_date": "2026-05-16",
                    "new_timing_end_date": "2026-05-16",
                    "previous_timing_text": "March 2026",
                    "previous_timing_precision": "month",
                    "previous_timing_start_date": "2026-03-01",
                    "previous_timing_end_date": "2026-03-31",
                    "supporting_page_index": "0",
                    "supporting_quote": party_quote,
                    "scope_mode": "unknown",
                    "internal_owner_roster_entry_id": str(roster_entry.id),
                    "next_action": "Confirm the revised completion plan",
                    "action_due_date": "2026-02-01",
                    "milestone_impact": "not_yet_known",
                },
                follow_redirects=False,
            )

        assert screen.status_code == 200
        assert "does not register a name for future Documents" in screen.text
        assert response.status_code == 303
        event = session.scalar(
            select(DependencyEvent).where(DependencyEvent.project_id == project.id)
        )
        assert event is not None
        assert event.stated_party == "Kinder Morgan"
        assert event.stated_external_org_id == canonical_party.id
        assert canonical_party.aliases == []
        receipt = session.scalar(
            select(StatementCoordinationReceipt).where(
                StatementCoordinationReceipt.candidate_id == candidate.id
            )
        )
        assert receipt is not None
        assert receipt.accepted_facts_json["party_resolution"] == {
            "mode": "guided_evidence_bound",
            "candidate_id": candidate.id,
            "stated_party": "Kinder Morgan",
            "stated_external_org_id": canonical_party.id,
            "principal": RECORDER.subject,
            "evidence": [
                {
                    "document_id": document.id,
                    "page_no": 1,
                    "quote": statement_quote,
                },
                {
                    "document_id": document.id,
                    "page_no": 1,
                    "quote": party_quote,
                },
            ],
        }
        coordination_audit = session.get(AuditLog, receipt.audit_log_id)
        assert coordination_audit.after_json["party_resolution"] == (
            receipt.accepted_facts_json["party_resolution"]
        )
        undo_statement_coordination(session, receipt.id, principal=RECORDER)
        assert candidate.state == "pending"
        assert canonical_party.aliases == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("submitted_party", ("Morgan", "kinder morgan", "Kinder-Morgan"))
def test_http_guided_save_refuses_inexact_party_words_as_a_statement_resolution(
    session, project, roster_entry, submitted_party
):
    canonical_party = ExternalOrg(name="Kinder Morgan Tejas Pipeline")
    session.add(canonical_party)
    session.flush()
    party_quote = "Kinder Morgan Management Meeting Highlights"
    statement_quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(
        session,
        project,
        "http-partial-party-resolution.xlsx",
        f"{party_quote}\n{statement_quote}",
    )
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == 1,
        )
    )
    page.text_source = "cells"
    candidate = _candidate(
        session,
        project,
        document,
        quote=statement_quote,
        fields={
            "event_type": "commitment",
            "description": statement_quote,
            "external_org": "Kinder Morgan",
        },
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/statements/{project.slug}/{candidate.id}/coordinate",
                data={
                    "affected_external_org_id": str(canonical_party.id),
                    "stated_party": submitted_party,
                    "stated_external_org_id": str(canonical_party.id),
                    "description": statement_quote,
                    "new_timing_text": "June 1, 2026",
                    "new_timing_precision": "day",
                    "new_timing_start_date": "2026-06-01",
                    "new_timing_end_date": "2026-06-01",
                    "supporting_page_index": "0",
                    "supporting_quote": party_quote,
                    "scope_mode": "unknown",
                    "internal_owner_roster_entry_id": str(roster_entry.id),
                    "next_action": "Confirm the completion plan",
                    "action_due_date": "2026-02-01",
                },
                follow_redirects=False,
            )

        assert response.status_code == 400
        assert candidate.state == "pending"
        assert canonical_party.aliases == []
        assert session.scalars(
            select(DependencyEvent).where(DependencyEvent.project_id == project.id)
        ).all() == []
        assert session.scalars(
            select(StatementCoordinationReceipt).where(
                StatementCoordinationReceipt.candidate_id == candidate.id
            )
        ).all() == []
    finally:
        app.dependency_overrides.clear()


def test_form_adapter_refuses_when_any_original_candidate_evidence_is_filtered(
    session, project, tmp_path
):
    supporting_quote = "Kinder Morgan Management Meeting Highlights"
    visible_document = _document(
        session,
        project,
        "visible-supporting-page.pdf",
        supporting_quote,
    )
    _register_page_image(
        session,
        visible_document,
        tmp_path / "visible-supporting-page.png",
    )
    filtered_document = _document(
        session,
        project,
        "malformed-hidden-citation.pdf",
        "This page must not receive the visible page selection.",
    )
    candidate = _candidate(
        session,
        project,
        visible_document,
        quote=supporting_quote,
        fields={"event_type": "commitment", "description": supporting_quote},
    )
    candidate.payload_json = {
        **candidate.payload_json,
        "citations": [
            {"document_id": filtered_document.id, "page": 1},
            {
                "document_id": visible_document.id,
                "page": 1,
                "quote": supporting_quote,
                "verified": True,
            },
        ],
    }

    with pytest.raises(
        StatementCoordinationRefusal,
        match="Save unavailable until every Candidate Evidence page",
    ):
        _supporting_statement_evidence(
            session,
            candidate,
            {
                "supporting_page_index": "0",
                "supporting_quote": supporting_quote,
            },
        )


def test_http_flow_refuses_a_supporting_quote_not_on_the_selected_registered_page(
    session, project, party, roster_entry, tmp_path
):
    statement_quote = "Will complete relocation by June 1, 2026."
    document = _document(
        session,
        project,
        "http-fail-closed-support.pdf",
        f"Kinder Morgan Management Meeting Highlights\n{statement_quote}",
    )
    page_image = tmp_path / "fail-closed-registered-page.png"
    _register_page_image(session, document, page_image)
    candidate = _candidate(
        session,
        project,
        document,
        quote=statement_quote,
        fields={"event_type": "commitment", "description": statement_quote},
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/statements/{project.slug}/{candidate.id}/coordinate",
                data={
                    "affected_external_org_id": str(party.id),
                    "stated_party": party.name,
                    "stated_external_org_id": str(party.id),
                    "description": statement_quote,
                    "new_timing_text": "June 1, 2026",
                    "new_timing_precision": "day",
                    "new_timing_start_date": "2026-06-01",
                    "new_timing_end_date": "2026-06-01",
                    "supporting_page_index": "0",
                    "supporting_quote": "Kinder Morgan fabricated supporting words",
                    "scope_mode": "unknown",
                    "internal_owner_roster_entry_id": str(roster_entry.id),
                    "next_action": "Confirm the June plan",
                    "action_due_date": "2026-02-01",
                },
            )

        assert response.status_code == 400
        assert "quote was not found on its registered page" in response.text
        assert candidate.state == "pending"
        assert session.scalar(
            select(func.count())
            .select_from(DependencyEvent)
            .where(DependencyEvent.project_id == project.id)
        ) == 0
    finally:
        app.dependency_overrides.clear()


def test_http_flow_renders_verified_context_and_delegates_to_the_atomic_command(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, "http-guided-flow.xlsx", quote)
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == 1,
        )
    )
    page.text_source = "cells"
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
            assert "read from registered cells" in screen.text
            assert (
                '<button type="submit">Save statement and Coordination Plan</button>'
                in screen.text
            )
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
