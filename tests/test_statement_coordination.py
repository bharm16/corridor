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
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
)
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
    EventAdmissionOutcome,
    ExternalOrg,
    ExtractionMeasurementCaseState,
    PolicyRun,
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
from corridor.milestones import import_csv
from corridor.models import DependencyEventTiming
from corridor.principals import HumanPrincipal
from access_support import seed_membership
from corridor.schedule_linking import flow_through_revisions, resolve_link
from corridor.statement_lifecycle import current_lineage_statement
from corridor.statement_suggestions import (
    declare_statement_suggestion_eligibility,
    declare_statement_suggestion_protection,
)
from corridor.work_list import build_work_list
from corridor.event_admission import UNKNOWN_SCOPE_POLICY_VERSION, run_event_admission
from corridor.statement_coordination import (
    CLOSURE_TARGET_RELATIONSHIP_GAP,
    StaleStatementCoordination,
    StatementCoordinationDraft,
    StatementCoordinationRefusal,
    StatementCoordinationUndoRefusal,
    assign_admitted_statement_owner,
    cancel_admitted_statement_next_action,
    complete_admitted_statement_next_action,
    coordinate_statement,
    correct_statement_facts,
    correct_statement_scope,
    defer_admitted_statement,
    mark_statement_not_relevant,
    keep_statement_unresolved,
    pending_statement_authority_gap,
    read_admitted_statement_coordination,
    restore_statement_not_relevant,
    set_admitted_statement_next_action,
    StatementFactCorrectionDraft,
    StatementScopeCorrection,
    undo_statement_coordination,
)
from corridor.work_decisions import (
    CoordinationSubject,
    current_deferral_decision,
    current_internal_owner_decision,
    current_next_action_decision,
    set_next_action,
)
from corridor.web.app import app, get_human_principal, get_session
from corridor.web.statement_forms import supporting_statement_evidence


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
    seed_membership(session, project, RECORDER)
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
        allow_unsealed_legacy=True,
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


def _seed_scope_match_abstention(session, project, candidate, dependency_ids):
    """Record the matcher's own ambiguous abstention receipt (#370, ADR-0054)."""
    run = PolicyRun(
        project_id=project.id,
        family="event-admission",
        policy_approval_id=None,
        policy_version="event-admission-identifying-language-test",
        policy_sha256="0" * 64,
        abstention_reason_version="event-admission-abstentions-test",
        applied_count=0,
        abstained_count=1,
    )
    session.add(run)
    session.flush([run])
    outcome = EventAdmissionOutcome(
        policy_run_id=run.id,
        candidate_id=candidate.id,
        outcome="abstained",
        reason="scope_matches_several_constraints",
        eligibility_json={
            "input": {"candidate_id": candidate.id},
            "verdict": "scope_matches_several_constraints",
            "card": {
                "candidate_dependency_ids": list(dependency_ids),
                "matched_details": ["12-inch gas main", "Station 6608+70"],
                "evidence_applied": {
                    "matched_terms": ["12-inch gas main", "Station 6608+70"],
                    "station_dependency_ids": list(dependency_ids),
                },
                "choice_modes": ["each", "both_all_listed"],
            },
        },
        eligibility_sha256="0" * 64,
    )
    session.add(outcome)
    session.flush([outcome])
    return outcome


def _narrowed_scope_candidate(session, project, party, tmp_path=None):
    quote = (
        "Kinder Morgan will relocate the 12-inch gas main at Station 6608+70."
    )
    document = _document(session, project, "narrowed-scope.pdf", quote)
    if tmp_path is not None:
        _register_page_image(session, document, tmp_path / "narrowed-scope.png")
    return _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={
            "event_type": "commitment",
            "external_org": party.name,
            "stated_party": party.name,
            "description": quote,
            "committed_date": {
                "text": "June 1, 2026",
                "precision": "day",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
            },
        },
    )


def test_coordinate_screen_renders_the_matchers_recorded_narrowed_set(
    session, project, party, roster_entry, tmp_path
):
    candidate = _narrowed_scope_candidate(session, project, party, tmp_path)
    first = _dependency(session, project, party, "PL41", "12-inch gas main")
    first.location_desc = "Station 6608+70"
    second = _dependency(session, project, party, "PL52", "12-inch gas main")
    second.location_desc = "Station 6608+70"
    _dependency(session, project, party, "PL99", "Water main")
    session.flush()
    _seed_scope_match_abstention(session, project, candidate, (first.id, second.id))

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            page = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Several constraints match this statement" in page.text
    card = page.text.split("Several constraints match this statement", 1)[1].split(
        "</section>", 1
    )[0]
    assert "PL41" in card
    assert "PL52" in card
    assert "PL99" not in card
    # The matcher's applied details and the source page image render as facts.
    assert "12-inch gas main" in card
    assert "/page-image/" in card
    assert f"applies to all 2 listed constraints" in card
    # The card never selects: no scope radio or constraint checkbox is checked
    # on load.  The card's buttons only fill a choice on an explicit human
    # click, so the served HTML carries no `checked` on any of these inputs.
    assert "no choice is preselected" in card.lower()
    input_tags = [
        fragment.split(">", 1)[0] for fragment in page.text.split("<input")[1:]
    ]
    scope_inputs = [
        tag
        for tag in input_tags
        if 'name="scope_mode"' in tag or 'name="dependency_id"' in tag
    ]
    assert scope_inputs
    assert all("checked" not in tag for tag in scope_inputs)


def test_coordinate_screen_shows_no_card_without_a_recorded_abstention_card(
    session, project, party, roster_entry
):
    candidate = _narrowed_scope_candidate(session, project, party)
    first = _dependency(session, project, party, "PL41", "12-inch gas main")
    second = _dependency(session, project, party, "PL52", "12-inch gas main")
    session.flush()

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            no_abstention = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
            # A malformed or single-survivor card is not a narrowed set; the
            # screen re-derives nothing from it.
            _seed_scope_match_abstention(session, project, candidate, (first.id,))
            malformed = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
            # A card naming a row that is no longer an active constraint of
            # this organization is stale and renders nothing.  A recorded
            # abstention is immutable, so this is a newer receipt the screen
            # reads as the current one, not an edit of the earlier outcome.
            _seed_scope_match_abstention(
                session, project, candidate, (first.id, second.id + 1000)
            )
            stale = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
    finally:
        app.dependency_overrides.clear()

    for page in (no_abstention, malformed, stale):
        assert page.status_code == 200
        assert "Several constraints match this statement" not in page.text


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
    assert session.scalar(
        select(func.count())
        .select_from(DependencyEvent)
        .where(DependencyEvent.project_id == project.id)
    ) == 0
    assert session.scalar(
        select(func.count())
        .select_from(StatementCoordinationReceipt)
        .where(StatementCoordinationReceipt.candidate_id == candidate.id)
    ) == 0
    assert session.scalar(
        select(func.count())
        .select_from(CandidateDisposition)
        .where(CandidateDisposition.candidate_id == candidate.id)
    ) == 0


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
    result = coordinate_statement(session, draft, principal=RECORDER)

    with pytest.raises(StaleStatementCoordination, match="already accepted"):
        coordinate_statement(session, draft, principal=RECORDER)

    assert session.scalar(
        select(func.count())
        .select_from(DependencyEvent)
        .where(DependencyEvent.commitment_lineage_id == result.event.commitment_lineage_id)
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(StatementCoordinationReceipt)
        .where(StatementCoordinationReceipt.candidate_id == candidate.id)
    ) == 1


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
    assert session.scalar(
        select(func.count())
        .select_from(DependencyEvent)
        .where(DependencyEvent.project_id == project.id)
    ) == 0


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
    assert session.scalar(
        select(func.count())
        .select_from(DependencyEventEvidence)
        .where(DependencyEventEvidence.event_id == result.event.id)
    ) == 1
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
    case = session.scalars(
        select(ExtractionMeasurementCaseState).where(
            ExtractionMeasurementCaseState.ruling_type
            == "statement_scope_decision",
            ExtractionMeasurementCaseState.ruling_id == correction.id,
        )
    ).one()
    assert case.kind == "statement_scope_correction"
    assert case.expected_json == {
        "scoring_rule": "statement_scope",
        "candidate_kind": "event",
        "mode": "selected",
        "dependency_ids": [dependency.id],
        "dependency_refs": [dependency.ref_code],
        "source_conflict_refs": [],
    }


def test_scope_correction_refuses_an_attributable_noop(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(session, project, "noop-statement-scope.pdf", quote)
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

    with pytest.raises(
        StatementCoordinationRefusal,
        match="already has this exact value",
    ):
        correct_statement_scope(
            session,
            StatementScopeCorrection(
                candidate_id=candidate.id,
                event_id=result.event.id,
                expected_scope_decision_id=result.receipt.scope_decision_id,
                scope=StatementScope.unknown(),
            ),
            principal=RECORDER,
        )

    decisions = tuple(
        session.scalars(
            select(DependencyEventScopeDecision).where(
                DependencyEventScopeDecision.event_id == result.event.id
            )
        ).all()
    )
    assert [decision.id for decision in decisions] == [result.receipt.scope_decision_id]


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
    case = session.scalars(
        select(ExtractionMeasurementCaseState).where(
            ExtractionMeasurementCaseState.ruling_type == "statement_event",
            ExtractionMeasurementCaseState.ruling_id == successor.id,
        )
    ).one()
    assert case.kind == "statement_fact_correction"
    assert case.case_key == (
        f"statement-lineage:{successor.commitment_lineage_id}:facts"
    )
    assert case.source_identity_json["documents"] == [
        {
            "document_id": document.id,
            "sha256": document.sha256,
            "locations": [
                {
                    "kind": "page_passage",
                    "page": 1,
                    "quote": corrected_quote,
                }
            ],
        }
    ]
    assert case.expected_json == {
        "scoring_rule": "candidate_fields_include",
        "candidate_kind": "event",
        "fields": {
            "event_type": "commitment",
            "description": corrected_quote,
            "event_date": "2025-01-17",
            "external_org": party.name,
            "stated_party": party.name,
            "committed_date": {
                "text": "July 1, 2026",
                "precision": "day",
                "start_date": "2026-07-01",
                "end_date": "2026-07-01",
            },
        },
    }


def test_http_fact_correction_receipt_binds_both_statement_versions(
    session, project, party, roster_entry
):
    original_quote = "Kinder Morgan will complete relocation by June 1, 2026."
    corrected_quote = "Kinder Morgan will complete relocation by July 1, 2026."
    document = _document(
        session,
        project,
        "http-fact-correction-receipt.pdf",
        f"{original_quote}\n{corrected_quote}",
    )
    candidate = _candidate(
        session,
        project,
        document,
        quote=original_quote,
        fields={"event_type": "commitment", "description": original_quote},
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

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/statements/{project.slug}/{candidate.id}/correct/facts",
                data={
                    "expected_statement_event_id": str(result.event.id),
                    "affected_external_org_id": str(party.id),
                    "stated_party": party.name,
                    "stated_external_org_id": str(party.id),
                    "event_date": "2025-01-17",
                    "description": corrected_quote,
                    "new_timing_text": "July 1, 2026",
                    "new_timing_precision": "day",
                    "new_timing_start_date": "2026-07-01",
                    "new_timing_end_date": "2026-07-01",
                    "evidence_document_id": str(document.id),
                    "evidence_page_no": "1",
                    "evidence_quote": corrected_quote,
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
    finally:
        app.dependency_overrides.clear()

    successor = current_lineage_statement(
        session, result.event.commitment_lineage_id
    )
    assert successor is not None and successor.id != result.event.id
    receipt = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == "product_proving_frontend_request",
            AuditLog.after_json["route_name"].astext
            == "correct_statement_facts_from_screen",
        )
        .order_by(AuditLog.id.desc())
    )
    assert receipt is not None
    subject = receipt.after_json["subject"]
    assert subject == {
        "project_id": project.id,
        "candidate_id": candidate.id,
        "commitment_lineage_id": result.event.commitment_lineage_id,
        "predecessor_statement_event_id": result.event.id,
        "successor_statement_event_id": successor.id,
    }


def test_fact_correction_can_bind_new_source_words_to_the_same_selected_party(
    session, project, roster_entry
):
    party = ExternalOrg(name="Kinder Morgan Tejas Pipeline statement-coordination-test")
    session.add(party)
    session.flush()
    original_quote = (
        "Kinder Morgan Tejas Pipeline statement-coordination-test will complete relocation by June 1, 2026."
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
    assert session.scalar(
        select(func.count())
        .select_from(DependencyEvent)
        .where(DependencyEvent.project_id == project.id)
    ) == 0
    assert session.scalar(
        select(func.count())
        .select_from(StatementCoordinationReceipt)
        .where(StatementCoordinationReceipt.candidate_id == candidate.id)
    ) == 0
    active_case = session.scalars(
        select(ExtractionMeasurementCaseState).where(
            ExtractionMeasurementCaseState.ruling_type == "candidate_disposition",
            ExtractionMeasurementCaseState.ruling_id == disposition.id,
        )
    ).one()
    assert active_case.kind == "do_not_add"
    assert active_case.state == "active"
    assert active_case.expected_json == {
        "scoring_rule": "candidate_disposition",
        "candidate_kind": "event",
        "expected_disposition": "do_not_add",
        "reason": "outside_project_scope",
    }
    reversal = restore_statement_not_relevant(session, disposition.id, principal=RECORDER)

    session.refresh(candidate)
    assert candidate.state == "pending"
    assert reversal.candidate_disposition_id == disposition.id
    assert session.get(CandidateDisposition, disposition.id).reason == "outside_project_scope"
    states = session.scalars(
        select(ExtractionMeasurementCaseState)
        .where(ExtractionMeasurementCaseState.case_key == active_case.case_key)
        .order_by(ExtractionMeasurementCaseState.id)
    ).all()
    assert [state.state for state in states] == ["active", "reversed"]
    assert states[1].predecessor_state_id == states[0].id
    assert states[1].ruling_type == "statement_coordination_reversal"
    assert states[1].ruling_id == reversal.id


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
    dependency.source_ref = "PL19"
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
            correct_screen = client.get(
                f"/statements/{project.slug}/{correct_candidate.id}/correct"
            )
            assert correct_screen.status_code == 200
            assert "Current accepted statement — read only" in correct_screen.text
            assert correct_quote in correct_screen.text
            assert "Supporting documents" in correct_screen.text
            assert "http-correct.pdf" in correct_screen.text
            assert 'name="scope_mode" value="selected"' in correct_screen.text
            assert 'name="scope_mode" value="all_active"' in correct_screen.text
            assert 'name="scope_mode" value="unknown"' not in correct_screen.text
            assert 'type="radio" name="scope_mode"' not in correct_screen.text
            assert "PL19 — KM crossing" in correct_screen.text
            assert "6608+70" in correct_screen.text
            assert "6616+50" in correct_screen.text
            scope_count_before_refusal = session.scalar(
                select(func.count()).select_from(DependencyEventScopeDecision)
            )
            refused = client.post(
                f"/statements/{project.slug}/{correct_candidate.id}/correct/scope",
                data={
                    "expected_statement_event_id": str(
                        correct_receipt.dependency_event_id
                    ),
                    "expected_scope_decision_id": str(
                        correct_receipt.scope_decision_id
                    ),
                    "scope_mode": "selected",
                },
                follow_redirects=False,
            )
            assert refused.status_code == 400
            assert "selected scope must name" in refused.text
            assert session.scalar(
                select(func.count()).select_from(DependencyEventScopeDecision)
            ) == scope_count_before_refusal
            refusal_receipt = session.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "product_proving_frontend_request",
                    AuditLog.entity_type == "project",
                    AuditLog.entity_id == project.id,
                    AuditLog.after_json["route_name"].astext
                    == "correct_statement_scope_from_screen",
                    AuditLog.after_json["status"].astext == "400",
                )
                .order_by(AuditLog.id.desc())
            )
            assert refusal_receipt is not None
            assert refusal_receipt.human_principal == RECORDER.subject
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
            assert "Not added to project record" in marked_page.text
            restored = client.post(
                f"/statements/{project.slug}/{irrelevant_candidate.id}/not-relevant/{disposition.id}/restore",
                follow_redirects=False,
            )
            assert restored.status_code == 303
            session.refresh(irrelevant_candidate)
            assert irrelevant_candidate.state == "pending"
    finally:
        app.dependency_overrides.clear()


def test_same_document_replay_authority_gap_uses_project_language():
    from corridor.statement_coordination import _DEPENDENCY_AUTHORITY_GAP_COPY

    title, detail = _DEPENDENCY_AUTHORITY_GAP_COPY[
        "same_document_replay_unproven"
    ]
    assert title == "Same-source Dependency replay not established"
    assert "current Dependency association" in detail
    assert "pending for Evidence review" in detail


def test_closure_without_an_exact_target_can_only_be_kept_as_attributable_unresolved_work(
    session, project, party
):
    quote = "As-built package for the Kinder Morgan crossing is Complete."
    document = _document(session, project, "meeting-notes/kinder-morgan.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={
            "event_type": "closure",
            "event_date": "2025-01-16",
            "description": quote,
            "external_org": party.name,
            "stated_party": party.name,
        },
    )

    receipt = keep_statement_unresolved(
        session,
        project.id,
        candidate.id,
        principal=RECORDER,
    )
    repeated = keep_statement_unresolved(
        session,
        project.id,
        candidate.id,
        principal=RECORDER,
    )

    session.refresh(candidate)
    assert candidate.state == "pending"
    assert receipt.action == audit.KEEP_STATEMENT_UNRESOLVED
    assert receipt.entity_type == audit.CANDIDATE
    assert receipt.entity_id == candidate.id
    assert receipt.actor == RECORDER.subject
    assert receipt.human_principal == RECORDER.subject
    assert receipt.before_json == {"candidate_state": "pending"}
    assert receipt.after_json == {
        "candidate_state": "pending",
        "authority_gap": "closure_target_commitment_not_established",
        "affected_external_org_id": party.id,
        "matching_open_commitment_lineage_ids": [],
    }
    assert repeated.id == receipt.id
    assert session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.entity_id == candidate.id,
            AuditLog.action == audit.KEEP_CANDIDATE_UNRESOLVED,
        )
    ) == 1


def test_pending_closure_screen_names_the_authority_gap_without_timing_or_scope_quizzes(
    session, project, party
):
    quote = "As-built package for the Kinder Morgan crossing is Complete."
    document = _document(session, project, "meeting-notes/kinder-morgan.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={
            "event_type": "closure",
            "event_date": "2025-01-16",
            "description": quote,
            "external_org": party.name,
            "stated_party": party.name,
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
            assert quote in screen.text
            assert "Documents reporting completion" in screen.text
            assert "Exact target Commitment not established" in screen.text
            assert (
                "The Evidence establishes a closure statement, but it does not "
                "identify an open Commitment in the Project Record that it closes."
                in screen.text
            )
            assert "No structured timing is available" not in screen.text
            assert "Commitment Scope" not in screen.text
            assert 'name="scope_mode"' not in screen.text
            assert "Do not add" not in screen.text
            assert (
                f'action="/statements/{project.slug}/{candidate.id}/keep-unresolved"'
                in screen.text
            )

            saved = client.post(
                f"/statements/{project.slug}/{candidate.id}/keep-unresolved",
                follow_redirects=False,
            )
            assert saved.status_code == 303
            session.refresh(candidate)
            assert candidate.state == "pending"

            acknowledged = client.get(saved.headers["location"])
            assert acknowledged.status_code == 200
            assert "Kept unresolved in the Work List" in acknowledged.text
            assert "Recorded unresolved authority gap" in acknowledged.text
            assert "Commitment covered by the completion report is not established" in acknowledged.text
    finally:
        app.dependency_overrides.clear()


def test_closure_gap_distinguishes_one_possible_commitment_from_an_established_target(
    session, project, party, roster_entry
):
    commitment_quote = f"{party.name} will complete the crossing in June 2026."
    commitment_document = _document(
        session,
        project,
        "meeting-notes/commitment.pdf",
        commitment_quote,
    )
    commitment_candidate = _candidate(
        session,
        project,
        commitment_document,
        quote=commitment_quote,
        fields={
            "event_type": "commitment",
            "event_date": "2025-01-16",
            "description": commitment_quote,
            "external_org": party.name,
            "stated_party": party.name,
        },
    )
    commitment = coordinate_statement(
        session,
        _draft(
            commitment_candidate,
            party,
            roster_entry,
            description=commitment_quote,
            new_timing=StatementTiming.month("June 2026", 2026, 6),
            evidence=(
                CitedStatementEvidence(commitment_document.id, 1, commitment_quote),
            ),
        ),
        principal=RECORDER,
    )
    closure_quote = f"The {party.name} crossing is Complete."
    closure_document = _document(
        session,
        project,
        "meeting-notes/closure.pdf",
        closure_quote,
    )
    closure_candidate = _candidate(
        session,
        project,
        closure_document,
        quote=closure_quote,
        fields={
            "event_type": "closure",
            "event_date": "2025-02-16",
            "description": closure_quote,
            "external_org": party.name,
            "stated_party": party.name,
        },
    )

    gap = pending_statement_authority_gap(
        session,
        project.id,
        closure_candidate.id,
    )

    assert gap is not None
    assert gap.code == CLOSURE_TARGET_RELATIONSHIP_GAP
    assert gap.title == "Closure-to-Commitment relationship not established"
    assert gap.matching_commitment_lineage_ids == (
        commitment.event.commitment_lineage_id,
    )


def test_http_screen_uses_supported_affected_party_without_inventing_the_speaker(
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
        assert "supported by the visible source passages" in screen.text
        assert 'id="affected-party"' not in screen.text
        assert (
            f'name="affected_external_org_id" value="{party.id}"'
            in screen.text
        )
        stated_options = screen.text.split('id="stated-party"', 1)[1].split(
            "</select>", 1
        )[0]
        assert f'<option value="{party.id}" selected>' not in stated_options
        assert 'id="stated-party-words"' not in screen.text
        assert 'type="hidden" name="stated_party"' in screen.text
        assert '<textarea id="description"' not in screen.text
        assert 'type="hidden" name="description"' in screen.text
        assert '<input id="new-timing-text"' not in screen.text
        assert 'type="hidden" name="new_timing_text"' in screen.text
        assert '<textarea id="support-quote"' not in screen.text
        assert '<textarea id="next-action"' not in screen.text
        assert '<select id="next-action" name="next_action" required>' in screen.text
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
        supporting_control = screen.text.split(
            'name="supporting_page_index" value="0"', 1
        )[1].split(">", 1)[0]
        assert "disabled" in supporting_control
        with pytest.raises(
            StatementCoordinationRefusal,
            match="Save unavailable until every registered source page for this extracted statement",
        ):
            supporting_statement_evidence(
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
        assert "Save unavailable until every registered source page for this extracted statement" in screen.text
        assert (
            '<button type="submit" disabled>Save statement and Follow-up plan</button>'
            in screen.text
        )
        assert response.status_code == 400
        assert "Save unavailable until every registered source page for this extracted statement" in response.text
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
    canonical_party = ExternalOrg(
        name="Kinder Morgan Tejas Pipeline statement-coordination-test"
    )
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
            "event_type": "committed_date_change",
            "description": statement_quote,
            "external_org": "DOW",
            "stated_party": "Kinder Morgan",
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
        assert "does not register a name for future documents" in screen.text
        assert response.status_code == 303, response.text
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
    canonical_party = ExternalOrg(
        name="Kinder Morgan Tejas Pipeline statement-coordination-test"
    )
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


@pytest.mark.parametrize("submitted_party", ("kinder morgan", "Kinder-Morgan"))
def test_http_guided_save_requires_exact_evidence_words_for_a_registered_party(
    session, project, roster_entry, submitted_party
):
    canonical_party = ExternalOrg(name="Kinder Morgan")
    session.add(canonical_party)
    session.flush()
    statement_quote = "Kinder Morgan will complete relocation by June 1, 2026."
    document = _document(
        session,
        project,
        "http-registered-party-exact-words.xlsx",
        statement_quote,
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
            "external_org": submitted_party,
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
                    "scope_mode": "unknown",
                    "internal_owner_roster_entry_id": str(roster_entry.id),
                    "next_action": "Confirm the completion plan",
                    "action_due_date": "2026-02-01",
                },
                follow_redirects=False,
            )

        assert response.status_code == 400
        assert candidate.state == "pending"
        assert session.scalars(
            select(DependencyEvent).where(DependencyEvent.project_id == project.id)
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
            match="Save unavailable until every registered source page for this extracted statement",
    ):
        supporting_statement_evidence(
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
    next_action = "Confirm the organization and which constraints the statement applies to"
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
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": party.name,
            "stated_party": party.name,
            "committed_date": {
                "text": "June 1, 2026",
                "precision": "day",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
            },
        },
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
            assert "Applies to: not yet known" in screen.text
            assert "Dana Fields" in screen.text
            assert "read from registered cells" in screen.text
            assert f'<option value="{next_action}">{next_action}</option>' in screen.text
            assert (
                '<button type="submit">Save statement and Follow-up plan</button>'
                in screen.text
            )
            assert 'name="scope_mode" value="unknown" required' in screen.text
            assert 'data-dependency-party="' in screen.text
            assert "Kinder Morgan crossing · Kinder Morgan" in screen.text
            assert (
                f'name="affected_external_org_id" value="{party.id}"'
                in screen.text
            )
            assert (
                f'name="stated_external_org_id" value="{party.id}"'
                in screen.text
            )
            assert 'id="affected-party"' not in screen.text
            assert 'id="stated-party"' not in screen.text
            assert 'name="new_timing_precision" value="day"' in screen.text
            assert "What precision does the Evidence support?" not in screen.text
            assert "June 1, 2026" in screen.text

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
                    "next_action": next_action,
                    "action_due_date": "2026-02-01",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            saved = client.get(response.headers["location"])
            assert "Follow-up plan saved" in saved.text
            assert "Commitment" in saved.text
            assert "Dana Fields" in saved.text
            assert next_action in saved.text
            assert "Why this needs attention" in saved.text
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
                    "next_action": next_action,
                    "action_due_date": "2026-02-01",
                },
            )
            assert stale.status_code == 409
            assert "Follow-up plan saved" in stale.text
    finally:
        app.dependency_overrides.clear()


def test_http_suggestions_only_order_explicit_scope_choices(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will relocate the gas main near Station 6609+00."
    document = _document(session, project, "suggestions.xlsx", quote)
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id, DocPage.page_no == 1
        )
    )
    page.text_source = "cells"
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": party.name,
            "stated_party": party.name,
            "conflict_ref": "SUGGEST-17",
            "station_from": "6609+00",
            "station_to": "6609+00",
            "committed_date": {
                "text": "June 1, 2026",
                "precision": "day",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
            },
        },
    )
    dependency = _dependency(
        session, project, party, "SUGGEST-17", "Kinder Morgan gas main"
    )
    dependency.source_ref = "SUGGEST-17"
    unrelated = _dependency(session, project, party, "AAA-01", "Unrelated fence")
    declare_statement_suggestion_eligibility(session, project.id, candidate.id)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            screen = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )

        assert screen.status_code == 200
        suggestions = screen.text.split('id="statement-suggestions"', 1)[1].split(
            "</section>", 1
        )[0]
        assert "Possible matching constraints" in suggestions
        assert "SUGGEST-17 — Kinder Morgan gas main" in suggestions
        assert "input" not in suggestions
        assert 'name="scope_mode" value="unknown" required' in screen.text
        assert f'value="{dependency.id}" checked' not in screen.text
        assert "checked" not in screen.text.split(
            'id="selected-scope-dependencies"', 1
        )[1].split("</div>", 1)[0]
        choices = screen.text.split('id="selected-scope-dependencies"', 1)[1].split(
            "</div>", 1
        )[0]
        assert choices.index("Kinder Morgan gas main") < choices.index(
            "Unrelated fence"
        )
        assert f'value="{unrelated.id}" checked' not in screen.text
    finally:
        app.dependency_overrides.clear()


def test_http_protected_statement_screen_withholds_every_suggestion(
    session, project, party, roster_entry
):
    quote = "Kinder Morgan will relocate the gas main near Station 6609+00."
    document = _document(session, project, "protected-suggestions.xlsx", quote)
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id, DocPage.page_no == 1
        )
    )
    page.text_source = "cells"
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": party.name,
            "stated_party": party.name,
            "conflict_ref": "SUGGEST-17",
            "station_from": "6609+00",
            "station_to": "6609+00",
            "committed_date": {
                "text": "June 1, 2026",
                "precision": "day",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
            },
        },
    )
    dependency = _dependency(
        session, project, party, "SUGGEST-17", "Kinder Morgan gas main"
    )
    dependency.source_ref = "SUGGEST-17"
    _dependency(session, project, party, "AAA-01", "Unrelated fence")
    declare_statement_suggestion_eligibility(session, project.id, candidate.id)
    declare_statement_suggestion_protection(
        session,
        project.id,
        candidate.id,
        kind="no_agent_baseline",
        observation_contract="correction-observation-window-v1",
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            screen = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )

        assert screen.status_code == 200
        assert 'id="statement-suggestions"' not in screen.text
        assert "Possible matching constraints" not in screen.text
        assert 'name="scope_mode" value="unknown" required' in screen.text
        choices = screen.text.split('id="selected-scope-dependencies"', 1)[1].split(
            "</div>", 1
        )[0]
        assert choices.index("Unrelated fence") < choices.index(
            "Kinder Morgan gas main"
        )
        assert "checked" not in choices
    finally:
        app.dependency_overrides.clear()


# --- ADR-0057 boundary: a schedule import never touches Promised For ---------


def _import_governing(session, project, tmp_path, need_date, name="schedule.csv"):
    path = tmp_path / name
    path.write_text(
        "code,name,need_date\n"
        f"UTIL-RELO-A,Utility relocations 6600+00 to 6620+00,{need_date}\n"
    )
    import_csv(session, project_id=project.id, path=path)
    return session.scalars(
        select(Milestone).where(
            Milestone.project_id == project.id, Milestone.code == "UTIL-RELO-A"
        )
    ).one()


def _statement_facts_snapshot(session, project):
    events = {
        event.id: (
            event.event_type,
            event.timing_direction,
            event.description,
            event.event_date,
            event.stated_party,
            event.scope_mode,
        )
        for event in session.scalars(
            select(DependencyEvent).where(DependencyEvent.project_id == project.id)
        )
    }
    timings = {
        timing.id: (
            timing.event_id,
            timing.kind,
            timing.text,
            timing.precision,
            timing.start_date,
            timing.end_date,
        )
        for timing in session.scalars(select(DependencyEventTiming))
        if timing.event_id in events
    }
    committed = {
        dependency.id: dependency.committed_date
        for dependency in session.scalars(
            select(Dependency).where(Dependency.project_id == project.id)
        )
    }
    return events, timings, committed


def test_a_schedule_import_never_alters_promised_for_or_any_statement_fact(
    session, project, party, roster_entry, tmp_path
):
    """ADR-0057: the schedule updates Required By automatically and never the
    Promised For an organization stated, nor any statement fact."""
    quote = "Kinder Morgan will provide the relocation schedule by June 1, 2026."
    document = _document(session, project, "boundary.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={"event_type": "commitment", "description": quote},
    )
    dependency = _dependency(session, project, party, "KM-1", "KM 20-inch line")
    coordinate_statement(
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
    session.refresh(dependency)
    # The organization's Promised For is on record for this exact row.
    assert dependency.committed_date == date(2026, 6, 1)

    milestone = _import_governing(session, project, tmp_path, "2026-05-01")
    resolve_link(session, dependency, milestone, principal=RECORDER)
    session.refresh(dependency)
    assert dependency.need_date == date(2026, 5, 1)

    before = _statement_facts_snapshot(session, project)

    # A schedule revision moves the Required By basis, and it flows through.
    _import_governing(session, project, tmp_path, "2026-08-15", name="rev.csv")
    flow_through_revisions(session, project.id)
    session.refresh(dependency)

    after = _statement_facts_snapshot(session, project)
    # Required By moved (the schedule's legitimate effect)...
    assert dependency.need_date == date(2026, 8, 15)
    # ...while Promised For and every statement fact are byte-for-byte untouched.
    assert after == before
    assert after[2][dependency.id] == date(2026, 6, 1)


def test_a_moved_key_date_surfaces_on_a_decision_that_referenced_it(
    session, project, party, roster_entry, tmp_path
):
    """ADR-0057: a recorded Effect-on-Key-Dates decision referencing a moved
    date appears as attention with the old and new values."""
    milestone = _import_governing(session, project, tmp_path, "2026-05-01")
    linked = _dependency(session, project, party, "KM-LINK", "KM line under RELO-A")
    resolve_link(session, linked, milestone, principal=RECORDER)

    quote = "Kinder Morgan moved completion from May 1, 2026 to June 1, 2026."
    document = _document(session, project, "affects.pdf", quote)
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
            new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
            previous_timing=StatementTiming.day("May 1, 2026", date(2026, 5, 1)),
            evidence=(CitedStatementEvidence(document.id, 1, quote),),
            scope=StatementScope.selected((linked.id,)),
            milestone_impact="affects",
            milestone_ids=(milestone.id,),
        ),
        principal=RECORDER,
    )
    lineage_id = result.event.commitment_lineage_id

    # A schedule revision moves the very key date that decision referenced.
    _import_governing(session, project, tmp_path, "2026-09-15", name="rev.csv")
    flow_through_revisions(session, project.id)

    work = build_work_list(session, project.id, today=date(2026, 3, 1))
    items = work.immediate + work.backlog
    statement_items = [
        item for item in items if item.commitment_lineage_id == lineage_id
    ]
    assert statement_items
    item = statement_items[0]
    assert "key_date_decision_affected" in item.attention_reason_codes
    [move] = item.key_date_moves
    assert move.milestone_id == milestone.id
    assert move.prior_need_date == date(2026, 5, 1)
    assert move.new_need_date == date(2026, 9, 15)


# --- Complete, cancel, and defer an accepted commitment's plan (#334) --------
#
# These exercise the mechanically admitted Commitment surface: its residual
# owner and Next Action, then the internal complete, cancel, and deferral
# decisions.  Internal work never closes the External Organization fact
# (ADR-0035, ADR-0038).


def _admitted_commitment(session, project, party):
    """Mechanically admit one unknown-scope Commitment and return its Candidate."""
    quote = f"{party.name} will provide the chain of title in June 2025."
    document = _document(session, project, "admitted-commitment.pdf", quote)
    _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={
            "event_type": "commitment",
            "event_date": date(2025, 1, 16).isoformat(),
            "description": quote,
            "external_org": party.name,
            "stated_party": party.name,
            "committed_date": {
                "text": "June 2025",
                "precision": "month",
                "start_date": "2025-06-01",
                "end_date": "2025-06-30",
            },
        },
    )
    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert result.admitted_count == 1
    return session.scalar(
        select(Candidate).where(
            Candidate.project_id == project.id, Candidate.kind == "event"
        )
    )


def _plan_the_admitted_commitment(session, project, candidate, roster_entry, action):
    """Drive the residual owner and Next Action, returning the coordination."""
    assign_admitted_statement_owner(
        session, project.id, candidate.id, roster_entry.id, principal=RECORDER
    )
    set_admitted_statement_next_action(
        session,
        project.id,
        candidate.id,
        action,
        due_date=None,
        due_date_unknown_reason="date_not_yet_known",
        principal=RECORDER,
    )
    return read_admitted_statement_coordination(session, project.id, candidate.id)


def test_admitted_commitment_completes_with_an_optional_note(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    assert coordination.can_close_next_action

    completed = complete_admitted_statement_next_action(
        session,
        project.id,
        candidate.id,
        expected_next_action_decision_id=coordination.next_action_decision.id,
        no_follow_up_reason="return_condition_recorded",
        note="Confirmed on the weekly call.",
        principal=RECORDER,
    )

    assert completed.decision_type == "complete_next_action"
    assert completed.note == "Confirmed on the weekly call."
    subject = CoordinationSubject.statement(coordination.lineage.id)
    assert current_next_action_decision(session, subject).after_value is None


def test_admitted_commitment_cancel_requires_a_structured_reason(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    expected = coordination.next_action_decision.id

    # No structured reason: refused, nothing written.
    with pytest.raises(StatementCoordinationRefusal):
        cancel_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=expected,
            no_follow_up_reason="no_immediate_follow_up",
            principal=RECORDER,
        )
    subject = CoordinationSubject.statement(coordination.lineage.id)
    assert current_next_action_decision(session, subject).id == expected

    cancelled = cancel_admitted_statement_next_action(
        session,
        project.id,
        candidate.id,
        expected_next_action_decision_id=expected,
        cancellation_reason="superseded",
        no_follow_up_reason="no_immediate_follow_up",
        note="Replaced by a direct call.",
        principal=RECORDER,
    )
    assert cancelled.cancellation_reason == "superseded"
    assert cancelled.note == "Replaced by a direct call."


def test_admitted_completion_requires_successor_or_no_follow_up(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    with pytest.raises(StatementCoordinationRefusal):
        complete_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=coordination.next_action_decision.id,
            principal=RECORDER,
        )


def test_admitted_completion_accepts_a_permitted_structured_successor(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    # Free text is refused even as a successor; a bounded choice is accepted.
    with pytest.raises(StatementCoordinationRefusal):
        complete_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=coordination.next_action_decision.id,
            successor_action="just call them",
            successor_due_date=date(2025, 7, 1),
            principal=RECORDER,
        )
    subject = CoordinationSubject.statement(coordination.lineage.id)
    completed = complete_admitted_statement_next_action(
        session,
        project.id,
        candidate.id,
        expected_next_action_decision_id=coordination.next_action_decision.id,
        successor_action="Coordinate the selected constraints",
        successor_due_date=date(2025, 7, 1),
        principal=RECORDER,
    )
    assert completed.decision_type == "complete_next_action"
    current = current_next_action_decision(session, subject)
    assert current.after_value is not None
    assert "Coordinate the selected constraints" in current.after_value


def test_completing_admitted_internal_work_leaves_the_external_fact_unchanged(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    statement_id = coordination.event.id
    scope_id = coordination.scope.id

    complete_admitted_statement_next_action(
        session,
        project.id,
        candidate.id,
        expected_next_action_decision_id=coordination.next_action_decision.id,
        no_follow_up_reason="return_condition_recorded",
        principal=RECORDER,
    )

    # The External Organization statement, Applies To, and Completion Reported
    # are all untouched; the open past-due fact stays in the work list.
    after = read_admitted_statement_coordination(session, project.id, candidate.id)
    assert after.event.id == statement_id
    assert after.scope.id == scope_id
    assert after.scope.scope_mode == "unknown"
    assert not after.can_close_next_action
    work_list = build_work_list(session, project.id, today=date(2025, 7, 1))
    lineage_id = coordination.lineage.id
    item = next(
        entry for entry in work_list.immediate
        if entry.commitment_lineage_id == lineage_id
    )
    assert item.past_due is not None


def test_admitted_close_refuses_stale_and_duplicate_submissions(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    stale_expected = coordination.next_action_decision.id

    # First completion succeeds.
    complete_admitted_statement_next_action(
        session,
        project.id,
        candidate.id,
        expected_next_action_decision_id=stale_expected,
        no_follow_up_reason="no_immediate_follow_up",
        principal=RECORDER,
    )
    # A duplicate submission of the same screen finds no live action to close.
    with pytest.raises(StatementCoordinationRefusal):
        complete_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=stale_expected,
            no_follow_up_reason="no_immediate_follow_up",
            principal=RECORDER,
        )

    # A fresh Next Action becomes current; the stale screen must not close it.
    set_admitted_statement_next_action(
        session,
        project.id,
        candidate.id,
        "Coordinate the selected constraints",
        due_date=None,
        due_date_unknown_reason="date_not_yet_known",
        principal=RECORDER,
    )
    current = read_admitted_statement_coordination(session, project.id, candidate.id)
    with pytest.raises(StaleStatementCoordination):
        complete_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=stale_expected,
            no_follow_up_reason="no_immediate_follow_up",
            principal=RECORDER,
        )
    subject = CoordinationSubject.statement(coordination.lineage.id)
    assert (
        current_next_action_decision(session, subject).id
        == current.next_action_decision.id
    )


def test_admitted_commitment_defers_with_reason_and_return_date(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    subject = CoordinationSubject.statement(coordination.lineage.id)

    deferral = defer_admitted_statement(
        session,
        project.id,
        candidate.id,
        expected_next_action_decision_id=coordination.next_action_decision.id,
        reason="waiting_for_external_party",
        return_date=date(2025, 6, 1),
        principal=RECORDER,
    )
    assert deferral.deferral_return_date == date(2025, 6, 1)
    assert current_deferral_decision(session, subject).id == deferral.id
    # The deferral does not close the action or the external fact.
    assert current_next_action_decision(session, subject).after_value is not None


def test_http_admitted_surface_exposes_complete_cancel_and_defer(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    expected = coordination.next_action_decision.id
    lineage_id = coordination.lineage.id
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            page = client.get(
                f"/statements/{project.slug}/{candidate.id}/coordinate"
            )
            assert page.status_code == 200
            assert (
                f"/statements/{project.slug}/{candidate.id}/admitted/complete"
                in page.text
            )
            assert (
                f"/statements/{project.slug}/{candidate.id}/admitted/defer"
                in page.text
            )

            deferred = client.post(
                f"/statements/{project.slug}/{candidate.id}/admitted/defer",
                data={
                    "expected_next_action_decision_id": str(expected),
                    "deferral_reason": "waiting_for_external_party",
                    "return_date": "2025-06-01",
                },
                follow_redirects=False,
            )
            assert deferred.status_code == 303

            completed = client.post(
                f"/statements/{project.slug}/{candidate.id}/admitted/complete",
                data={
                    "expected_next_action_decision_id": str(expected),
                    "no_follow_up_reason": "return_condition_recorded",
                    "note": "Confirmed by phone.",
                },
                follow_redirects=False,
            )
            assert completed.status_code == 303
    finally:
        app.dependency_overrides.clear()

    subject = CoordinationSubject.statement(lineage_id)
    completion = current_next_action_decision(session, subject)
    assert completion.after_value is None
    assert completion.note == "Confirmed by phone."


def test_http_admitted_close_refuses_a_stale_or_malformed_submission(
    session, project, party, roster_entry
):
    candidate = _admitted_commitment(session, project, party)
    coordination = _plan_the_admitted_commitment(
        session,
        project,
        candidate,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    try:
        with TestClient(app) as client:
            # A missing expected id is a malformed submission: refused, no close.
            malformed = client.post(
                f"/statements/{project.slug}/{candidate.id}/admitted/complete",
                data={"no_follow_up_reason": "no_immediate_follow_up"},
                follow_redirects=False,
            )
            assert malformed.status_code == 400

            stale = client.post(
                f"/statements/{project.slug}/{candidate.id}/admitted/complete",
                data={
                    "expected_next_action_decision_id": str(
                        coordination.next_action_decision.id + 10_000
                    ),
                    "no_follow_up_reason": "no_immediate_follow_up",
                },
                follow_redirects=False,
            )
            assert stale.status_code == 409
    finally:
        app.dependency_overrides.clear()

    subject = CoordinationSubject.statement(coordination.lineage.id)
    assert current_next_action_decision(session, subject).after_value is not None


def test_a_pending_statement_cannot_acquire_a_close_control(
    session, project, party
):
    """A proposal with no accepted plan cannot be completed or deferred (#334)."""
    quote = f"{party.name} might provide the chain of title in June 2025."
    document = _document(session, project, "pending-statement.pdf", quote)
    candidate = _candidate(
        session,
        project,
        document,
        quote=quote,
        fields={
            "event_type": "commitment",
            "event_date": date(2025, 1, 16).isoformat(),
            "description": quote,
            "external_org": party.name,
            "stated_party": party.name,
        },
    )
    # No mechanical admission ran: there is no accepted commitment plan.
    assert read_admitted_statement_coordination(
        session, project.id, candidate.id
    ) is None
    with pytest.raises(StatementCoordinationRefusal):
        complete_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=1,
            no_follow_up_reason="no_immediate_follow_up",
            principal=RECORDER,
        )
    with pytest.raises(StatementCoordinationRefusal):
        defer_admitted_statement(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=1,
            reason="waiting_for_information",
            return_date=date(2025, 6, 1),
            principal=RECORDER,
        )


def test_a_close_bound_to_another_subjects_action_is_refused(
    session, project, party, roster_entry
):
    """Passing a different subject's action id must not close this one (#334)."""
    first = _admitted_commitment(session, project, party)
    first_plan = _plan_the_admitted_commitment(
        session,
        project,
        first,
        roster_entry,
        "Confirm the stated timing with the organization",
    )
    other_party = ExternalOrg(name="Enterprise Products")
    session.add(other_party)
    session.flush()
    quote = f"{other_party.name} will provide the chain of title in June 2025."
    other_doc = _document(session, project, "other-commitment.pdf", quote)
    _candidate(
        session,
        project,
        other_doc,
        quote=quote,
        fields={
            "event_type": "commitment",
            "event_date": date(2025, 1, 16).isoformat(),
            "description": quote,
            "external_org": other_party.name,
            "stated_party": other_party.name,
            "committed_date": {
                "text": "June 2025",
                "precision": "month",
                "start_date": "2025-06-01",
                "end_date": "2025-06-30",
            },
        },
    )
    run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    other = session.scalar(
        select(Candidate).where(
            Candidate.project_id == project.id,
            Candidate.kind == "event",
            Candidate.id != first.id,
        )
    )
    other_plan = _plan_the_admitted_commitment(
        session,
        project,
        other,
        roster_entry,
        "Confirm the stated timing with the organization",
    )

    # Completing the first commitment with the second's action id is refused.
    with pytest.raises(StaleStatementCoordination):
        complete_admitted_statement_next_action(
            session,
            project.id,
            first.id,
            expected_next_action_decision_id=other_plan.next_action_decision.id,
            no_follow_up_reason="no_immediate_follow_up",
            principal=RECORDER,
        )
    first_subject = CoordinationSubject.statement(first_plan.lineage.id)
    other_subject = CoordinationSubject.statement(other_plan.lineage.id)
    assert current_next_action_decision(
        session, first_subject
    ).after_value is not None
    assert current_next_action_decision(
        session, other_subject
    ).after_value is not None
