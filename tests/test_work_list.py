"""Public work-list behavior for coordinator-facing External Party work."""

from datetime import date
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_closure,
    record_statement_scope_decision,
    record_external_party_statement,
)
from corridor.models import (
    AuditLog,
    Candidate,
    CandidateDisposition,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScopeDecision,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
)
from corridor.principals import HumanPrincipal
from corridor.work_list import build_work_list
from corridor.work_decisions import (
    CoordinationSubject,
    assign_internal_owner,
    current_next_action_decision,
    defer_work,
    set_next_action,
)
from corridor.web.app import app, get_session


RECORDER = HumanPrincipal("local:work-list-coordinator")


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
def client(session):
    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    project = Project(
        slug="work-list-test",
        name="Work list test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    return project


@pytest.fixture
def party(session):
    party = ExternalOrg(name="Equistar work-list-test")
    session.add(party)
    session.flush()
    return party


def _record_month_commitment(session, project, party, *, speaker=None):
    stated_party = speaker or party
    quote = f"{stated_party.name} to provide chain of title (Due date of 01/2025)."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(quote.encode()).hexdigest(),
        filename="equistar-minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    session.flush()
    return record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=stated_party.name,
        stated_external_org_id=stated_party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description=quote,
        new_timing=StatementTiming.month("01/2025", 2025, 1),
        scope=StatementScope.unknown(),
        created_by="local:coordinator",
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )


def test_month_precision_unknown_scope_is_one_party_level_past_due_work_item(
    session, project, party
):
    """January is a source claim; January 1 and a Dependency are not."""
    statement = _record_month_commitment(session, project, party)

    work_list = build_work_list(session, project.id, today=date(2025, 2, 1))

    assert len(work_list.immediate) == 1
    item = work_list.immediate[0]
    assert item.commitment_lineage_id == statement.commitment_lineage_id
    assert item.dependency_id is None
    assert item.timing_text == "January 2025"
    assert item.attention_reason_codes == (
        "past_due",
        "unknown_scope",
        "missing_internal_owner",
        "missing_next_action",
    )
    assert item.past_due is not None
    assert item.past_due.evaluated_on == date(2025, 2, 1)
    assert item.past_due.statement_event_id == statement.id
    assert item.past_due.source_kind == "cited"


def test_future_action_temporarily_moves_work_to_backlog_but_an_unknown_date_does_not(
    session, project, party
):
    statement = _record_month_commitment(session, project, party)
    subject = CoordinationSubject.statement(statement.commitment_lineage_id)
    set_next_action(
        session,
        subject,
        "Confirm the chain of title",
        due_date=date(2025, 2, 15),
        principal=RECORDER,
    )

    deferred = build_work_list(session, project.id, today=date(2025, 2, 1))

    assert deferred.immediate == ()
    assert len(deferred.backlog) == 1
    assert deferred.backlog[0].attention_reason_codes == (
        "past_due",
        "unknown_scope",
        "missing_internal_owner",
    )

    set_next_action(
        session,
        subject,
        "Confirm the chain of title",
        due_date_unknown_reason="awaiting_external_information",
        principal=RECORDER,
    )

    unknown_due_date = build_work_list(session, project.id, today=date(2025, 2, 1))

    assert len(unknown_due_date.immediate) == 1
    assert "action_due_date_unknown" in unknown_due_date.immediate[0].attention_reason_codes


def test_explicit_deferral_requires_a_return_date_and_returns_when_due(
    session, project, party
):
    statement = _record_month_commitment(session, project, party)
    subject = CoordinationSubject.statement(statement.commitment_lineage_id)

    decision = defer_work(
        session,
        subject,
        reason="waiting_for_external_party",
        return_date=date(2025, 2, 15),
        principal=RECORDER,
    )

    assert decision.deferral_reason == "waiting_for_external_party"
    assert decision.deferral_return_date == date(2025, 2, 15)
    assert build_work_list(session, project.id, today=date(2025, 2, 1)).immediate == ()
    assert len(build_work_list(session, project.id, today=date(2025, 2, 1)).backlog) == 1
    assert len(build_work_list(session, project.id, today=date(2025, 2, 15)).immediate) == 1


def test_new_scope_decision_returns_a_deferred_party_level_work_item(
    session, project, party
):
    statement = _record_month_commitment(session, project, party)
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-SCOPE-RETURN",
        dep_type="utility_relocation",
        title="Dependency identified after the original statement",
        external_org_id=party.id,
        status="identified",
    )
    session.add(dependency)
    session.flush()
    defer_work(
        session,
        CoordinationSubject.statement(statement.commitment_lineage_id),
        reason="waiting_for_information",
        return_date=date(2025, 2, 15),
        principal=RECORDER,
    )

    record_statement_scope_decision(
        session,
        event_id=statement.id,
        scope=StatementScope.selected((dependency.id,)),
        actor=RECORDER,
    )

    returned = build_work_list(session, project.id, today=date(2025, 2, 1))

    assert len(returned.immediate) == 1
    assert returned.backlog == ()
    assert "unknown_scope" not in returned.immediate[0].attention_reason_codes


def test_current_timing_replaces_stale_past_due_boundary_and_marks_plan_for_review(
    session, project, party
):
    original = _record_month_commitment(session, project, party)
    subject = CoordinationSubject.statement(original.commitment_lineage_id)
    set_next_action(
        session,
        subject,
        "Confirm the revised delivery plan",
        due_date=date(2025, 2, 15),
        principal=RECORDER,
    )
    quote = "Equistar now commits to delivery in February 2025."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(quote.encode()).hexdigest(),
        filename="equistar-revised-minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    session.flush()
    revised = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 2, 1),
        description=quote,
        new_timing=StatementTiming.month("February 2025", 2025, 2),
        scope=StatementScope.unknown(),
        created_by="local:coordinator",
        evidence=CitedStatementEvidence(document.id, 1, quote),
        commitment_lineage_id=original.commitment_lineage_id,
    )

    work_list = build_work_list(session, project.id, today=date(2025, 2, 1))

    assert len(work_list.immediate) == 1
    item = work_list.immediate[0]
    assert item.statement_event_id == revised.id
    assert item.timing_text == "February 2025"
    assert item.past_due is None


def test_completing_internal_work_does_not_close_a_party_level_commitment(
    session, project, party
):
    statement = _record_month_commitment(session, project, party)
    subject = CoordinationSubject.statement(statement.commitment_lineage_id)
    set_next_action(
        session,
        subject,
        "Call Equistar",
        due_date=date(2025, 2, 15),
        principal=RECORDER,
    )
    from corridor.work_decisions import complete_next_action

    complete_next_action(
        session,
        subject,
        no_follow_up_reason="return_condition_recorded",
        principal=RECORDER,
    )

    work_list = build_work_list(session, project.id, today=date(2025, 2, 1))

    assert len(work_list.immediate) == 1
    assert work_list.immediate[0].past_due is not None


def test_work_list_preserves_exact_approximate_and_date_change_attention_reasons(
    session, project, party
):
    def record(quote, timing, *, previous=None):
        document = Document(
            project_id=project.id,
            sha256=hashlib.sha256(quote.encode()).hexdigest(),
            filename=f"{hashlib.sha256(quote.encode()).hexdigest()[:12]}.pdf",
            doc_type="minutes",
            parse_status="parsed",
        )
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text=quote))
        session.flush()
        return record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=date(2025, 2, 1),
            description=quote,
            new_timing=timing,
            previous_timing=previous,
            scope=StatementScope.unknown(),
            created_by="local:coordinator",
            evidence=CitedStatementEvidence(document.id, 1, quote),
        )

    exact = record("Equistar commits to February 1, 2025.", StatementTiming.day("February 1, 2025", date(2025, 2, 1)))
    approximate = record("Equistar expects work in about two weeks.", StatementTiming.approximate("in about two weeks"))
    change = record(
        "Equistar moved delivery from December 2024 to January 2025.",
        StatementTiming.month("January 2025", 2025, 1),
        previous=StatementTiming.month("December 2024", 2024, 12),
    )

    on_day = build_work_list(session, project.id, today=date(2025, 2, 1))
    after_day = build_work_list(session, project.id, today=date(2025, 2, 2))
    exact_on_day = next(item for item in on_day.immediate if item.statement_event_id == exact.id)
    exact_after_day = next(item for item in after_day.immediate if item.statement_event_id == exact.id)
    approximate_item = next(item for item in after_day.immediate if item.statement_event_id == approximate.id)
    changed_item = next(item for item in after_day.immediate if item.statement_event_id == change.id)

    assert exact_on_day.past_due is None
    assert exact_after_day.past_due is not None
    assert approximate_item.past_due is None
    assert changed_item.attention_reason_codes == (
        "past_due",
        "committed_date_change",
        "milestone_impact_unknown",
        "unknown_scope",
        "missing_internal_owner",
        "missing_next_action",
    )


def test_verified_closure_ends_only_the_linked_party_level_past_due_fact(
    session, project, party
):
    statement = _record_month_commitment(session, project, party)
    subject = CoordinationSubject.statement(statement.commitment_lineage_id)
    action = set_next_action(
        session,
        subject,
        "Confirm completion receipt",
        due_date=date(2025, 2, 15),
        principal=RECORDER,
    )
    quote = "Equistar confirms the chain of title has been delivered."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(quote.encode()).hexdigest(),
        filename="equistar-closure.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    closure = DependencyEvent(
        event_type="closure",
        project_id=project.id,
        closes_commitment_lineage_id=statement.commitment_lineage_id,
        affected_external_org_id=party.id,
        stated_external_org_id=party.id,
        attribution_state="resolved",
        scope_mode="unknown",
        source_kind="cited",
        stated_party=party.name,
        event_date=date(2025, 2, 1),
        description=quote,
        created_by=RECORDER.subject,
    )
    session.add(closure)
    session.flush()
    evidence = EvidenceLink(
        document_id=document.id,
        page_no=1,
        quote=quote,
        verified=False,
    )
    session.add(evidence)
    session.flush()
    session.add(
        DependencyEventEvidence(
            event_id=closure.id,
            evidence_link_id=evidence.id,
            recorded_by=RECORDER.subject,
        )
    )
    session.flush()

    without_verified_closure = build_work_list(
        session, project.id, today=date(2025, 2, 1)
    )
    assert without_verified_closure.immediate == ()
    assert len(without_verified_closure.backlog) == 1

    record_external_party_closure(
        session,
        project_id=project.id,
        commitment_lineage_id=statement.commitment_lineage_id,
        source_kind="cited",
        event_date=date(2025, 2, 1),
        description=quote,
        created_by=RECORDER.subject,
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )

    work_list = build_work_list(session, project.id, today=date(2025, 2, 1))

    assert work_list.immediate == ()
    assert len(work_list.backlog) == 1
    assert work_list.backlog[0].past_due is None
    assert work_list.backlog[0].attention_reason_codes == (
        "external_closure_follow_up",
        "missing_internal_owner",
    )
    assert current_next_action_decision(session, subject).id == action.id


def test_closure_preserves_distinct_affected_and_speaking_party_identities(
    session, project, party
):
    speaker = ExternalOrg(name="Equistar representative work-list-test")
    session.add(speaker)
    session.flush()
    statement = _record_month_commitment(session, project, party, speaker=speaker)
    quote = "Equistar representative work-list-test confirms delivery."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(quote.encode()).hexdigest(),
        filename="equistar-representative-closure.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    session.flush()

    closure = record_external_party_closure(
        session,
        project_id=project.id,
        commitment_lineage_id=statement.commitment_lineage_id,
        source_kind="cited",
        event_date=date(2025, 2, 1),
        description=quote,
        created_by=RECORDER.subject,
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )

    assert (closure.affected_external_org_id, closure.stated_external_org_id) == (
        party.id,
        speaker.id,
    )
    assert build_work_list(session, project.id, today=date(2025, 2, 1)).immediate == ()


def test_work_list_orders_party_past_due_before_critical_dependency_and_unplaced_statement(
    session, project, party
):
    statement = _record_month_commitment(session, project, party)
    critical = Dependency(
        project_id=project.id,
        ref_code="DEP-CRITICAL-WORK-LIST",
        dep_type="utility_relocation",
        title="Critical relocation without a Coordination Plan",
        resolution_strategy="relocate",
        status="identified",
    )
    source = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"unplaced-work-list").hexdigest(),
        filename="unplaced-work-list.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add_all((critical, source))
    session.flush()
    quote = "External Party statement needs coordinator review."
    session.add(DocPage(document_id=source.id, page_no=1, text=quote))
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={"kind": "event", "fields": {}, "citations": [{
            "document_id": source.id,
            "page": 1,
            "quote": quote,
            "verified": True,
        }]},
        source_document_id=source.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="work-list-test",
        model="test-model",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        source,
        prompt_version="work-list-test",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
    )
    declare_active_run(session, source.id, run.id, principal=RECORDER)

    work_list = build_work_list(session, project.id, today=date(2025, 2, 1))

    assert [item.kind for item in work_list.immediate] == [
        "statement",
        "dependency",
        "candidate",
    ]
    assert work_list.immediate[0].statement_event_id == statement.id
    assert work_list.immediate[1].dependency_id == critical.id
    assert work_list.immediate[1].attention_reason_codes == (
        "critical_missing_internal_owner",
        "critical_missing_next_action",
    )
    assert work_list.immediate[2].candidate_id == candidate.id


def test_coordinator_home_renders_the_public_work_list_and_guided_statement_link(
    client, session, project, party
):
    _record_month_commitment(session, project, party)
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"home-unplaced-statement").hexdigest(),
        filename="home-unplaced-statement.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    quote = "A statement awaiting guided coordination."
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={"kind": "event", "fields": {}, "citations": [{
            "document_id": document.id,
            "page": 1,
            "quote": quote,
            "verified": True,
        }]},
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="work-list-test",
        model="test-model",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version="work-list-test",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)

    response = client.get(f"/work/{project.slug}")

    assert response.status_code == 200
    assert "Work needing attention now" in response.text
    assert "The External Party commitment passed its stated date" in response.text
    assert "past_due" not in response.text
    assert f'href="/statements/{project.slug}/{candidate.id}/coordinate"' in response.text
    assert f'href="/ledger/{project.slug}"' in response.text


def test_coordinator_home_links_an_accepted_commitment_to_its_guided_plan(
    client, session, project, party
):
    statement = _record_month_commitment(session, project, party)
    roster_entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:work-list-owner",
        display_name="Work List Owner",
    )
    session.add(roster_entry)
    session.flush()
    subject = CoordinationSubject.statement(statement.commitment_lineage_id)
    owner = assign_internal_owner(
        session, subject, "Work List Owner", principal=RECORDER
    )
    action = set_next_action(
        session,
        subject,
        "Confirm the current party commitment",
        due_date=date(2025, 2, 1),
        principal=RECORDER,
    )
    candidate_document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"accepted-work-list-source").hexdigest(),
        filename="accepted-work-list-source.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(candidate_document)
    session.flush()
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={},
        source_document_id=candidate_document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="work-list-test",
        model="test-model",
        citations_verified=True,
        state="accepted",
    )
    session.add(candidate)
    session.flush()
    disposition = CandidateDisposition(
        candidate_id=candidate.id,
        disposition="accepted",
        reason=None,
        recorded_by=RECORDER.subject,
    )
    audit = AuditLog(
        actor=RECORDER.subject,
        human_principal=RECORDER.subject,
        action="coordinate_statement",
        entity_type="commitment_lineage",
        entity_id=statement.commitment_lineage_id,
        before_json=None,
        after_json={},
    )
    session.add_all((disposition, audit))
    session.flush()
    scope = session.scalar(
        select(DependencyEventScopeDecision).where(
            DependencyEventScopeDecision.event_id == statement.id
        )
    )
    assert scope is not None
    session.add(
        StatementCoordinationReceipt(
            candidate_id=candidate.id,
            candidate_disposition_id=disposition.id,
            commitment_lineage_id=statement.commitment_lineage_id,
            dependency_event_id=statement.id,
            scope_decision_id=scope.id,
            internal_owner_roster_entry_id=roster_entry.id,
            internal_owner_decision_id=owner.id,
            next_action_decision_id=action.id,
            milestone_impact_decision_id=None,
            audit_log_id=audit.id,
            expected_predecessors_json={},
            accepted_facts_json={},
            candidate_payload_sha256="a" * 64,
            recorded_by=RECORDER.subject,
        )
    )
    session.flush()

    response = client.get(f"/work/{project.slug}")
    guided_url = f"/statements/{project.slug}/{candidate.id}/coordinate"

    assert response.status_code == 200
    assert f'href="{guided_url}"' in response.text
    assert client.get(guided_url).status_code == 200
