"""Public work-list behavior for coordinator-facing External Party work."""

from datetime import date
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from corridor import policy
from corridor.db import Session, engine
from corridor.dependency_events import current_scope_decision_filter
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.event_admission import UNKNOWN_SCOPE_POLICY_VERSION, run_event_admission
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
    DependencyAdmissionOutcome,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DocPage,
    Document,
    EvidenceLink,
    EventAdmissionOutcome,
    ExternalOrg,
    PolicyRun,
    Project,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
)
from corridor.principals import HumanPrincipal
from corridor.statement_coordination import (
    assign_admitted_statement_owner,
    read_admitted_statement_coordination,
    set_admitted_statement_next_action,
)
from corridor.work_list import build_work_list
from corridor.work_decisions import (
    CoordinationSubject,
    assign_internal_owner,
    current_next_action_decision,
    defer_work,
    set_next_action,
)
from corridor.web.app import app, get_human_principal, get_session


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
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
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


def _record_pending_statement_candidates(session, project, specifications):
    """Record one Active Run and the Admission residue its public reader sees."""
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(
            f"bounded-work-list-{project.slug}".encode()
        ).hexdigest(),
        filename="meeting-notes/bounded-work-list.pdf",
        doc_type="minutes",
        doc_date=date(2025, 1, 15),
        parse_status="parsed",
    )
    session.add(document)
    session.flush()

    candidates = []
    for page_no, specification in enumerate(specifications, start=1):
        quote = specification["quote"]
        session.add(DocPage(document_id=document.id, page_no=page_no, text=quote))
        fields = {
            "event_type": specification["event_type"],
            "event_date": specification["event_date"].isoformat(),
            "description": quote,
            "external_org": specification.get("external_org", "Example Party"),
        }
        if "committed_date" in specification:
            fields["committed_date"] = specification["committed_date"]
        if "conflict_ref" in specification:
            fields["conflict_ref"] = specification["conflict_ref"]
        if "stated_party" in specification:
            fields["stated_party"] = specification["stated_party"]
        candidate = Candidate(
            project_id=project.id,
            kind="event",
            payload_json={
                "kind": "event",
                "fields": fields,
                "citations": [
                    {
                        "document_id": document.id,
                        "page": page_no,
                        "quote": quote,
                        "verified": True,
                    }
                ],
            },
            source_document_id=document.id,
            source_pages=[page_no],
            confidence=specification.get("confidence", 0.5),
            prompt_version="bounded-work-list-test",
            model="test-model",
            citations_verified=True,
        )
        candidates.append(candidate)

    run = record_extraction_run(
        session,
        document,
        prompt_version="bounded-work-list-test",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=tuple(candidates),
        model="test-model",
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)
    admission_run = PolicyRun(
        project_id=project.id,
        family="event-admission",
        policy_approval_id=None,
        policy_version="bounded-work-list-test",
        policy_sha256="a" * 64,
        abstention_reason_version="bounded-work-list-test",
        applied_count=0,
        abstained_count=len(candidates),
    )
    session.add(admission_run)
    session.flush()
    session.add_all(
        EventAdmissionOutcome(
            policy_run_id=admission_run.id,
            candidate_id=candidate.id,
            outcome="abstained",
            reason=specification["reason"],
        )
        for candidate, specification in zip(candidates, specifications, strict=True)
    )
    session.flush()
    return tuple(candidates)


def _record_pending_dependency_candidates(session, project, specifications):
    """Record ordinary dependency proposals from one declared Active Run."""
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(
            f"dependency-work-list-{project.slug}-{len(specifications)}".encode()
        ).hexdigest(),
        filename="utility-matrix/current-active-run.pdf",
        doc_type="matrix",
        doc_date=date(2025, 1, 20),
        parse_status="parsed",
    )
    session.add(document)
    session.flush()

    candidates = []
    for page_no, specification in enumerate(specifications, start=1):
        quote = specification["quote"]
        session.add(DocPage(document_id=document.id, page_no=page_no, text=quote))
        candidate = Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": {
                    "conflict_ref": specification["conflict_ref"],
                    "external_org": specification["external_org"],
                    "description": specification["description"],
                },
                "citations": [
                    {
                        "document_id": document.id,
                        "page": page_no,
                        "quote": quote,
                        "verified": True,
                    }
                ],
            },
            source_document_id=document.id,
            source_pages=[page_no],
            confidence=0.9,
            prompt_version="dependency-work-list-test",
            model="test-model",
            citations_verified=True,
        )
        candidates.append(candidate)

    run = record_extraction_run(
        session,
        document,
        prompt_version="dependency-work-list-test",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=tuple(candidates),
        model="test-model",
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)
    admission_run = PolicyRun(
        project_id=project.id,
        family="dependency-admission",
        policy_approval_id=None,
        policy_version="dependency-work-list-test",
        policy_sha256="b" * 64,
        abstention_reason_version="dependency-work-list-test",
        applied_count=0,
        abstained_count=len(candidates),
    )
    session.add(admission_run)
    session.flush()
    outcomes = []
    for candidate, specification in zip(candidates, specifications, strict=True):
        reason = specification.get("reason", "no_row_identity")
        eligibility = {
            "input": {"candidate_id": candidate.id},
            "verdict": reason,
            "reason_version": "dependency-work-list-test",
        }
        outcomes.append(
            DependencyAdmissionOutcome(
                policy_run_id=admission_run.id,
                family="dependency-admission",
                candidate_id=candidate.id,
                outcome="abstained",
                reason=reason,
                eligibility_json=eligibility,
                eligibility_sha256=policy.canonical_sha256(eligibility),
            )
        )
    session.add_all(outcomes)
    session.flush()
    return tuple(candidates)


def _mechanically_admit_unknown_scope(session, project, party):
    quote = f"{party.name} will provide the chain of title in June 2025."
    [candidate] = _record_pending_statement_candidates(
        session,
        project,
        [
            {
                "event_type": "commitment",
                "event_date": date(2025, 1, 16),
                "committed_date": {
                    "text": "June 2025",
                    "precision": "month",
                    "start_date": "2025-06-01",
                    "end_date": "2025-06-30",
                },
                "external_org": party.name,
                "stated_party": party.name,
                "quote": quote,
                "reason": "no_conflict_reference",
            }
        ],
    )
    result = run_event_admission(
        session,
        project.id,
        policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
    )
    assert result.admitted_count == 1
    return candidate


def test_admitted_statement_coordination_returns_one_residual_decision(
    session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    owner = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:residual-owner",
        display_name="Residual Owner",
    )
    session.add(owner)
    session.flush()

    coordination = read_admitted_statement_coordination(
        session, project.id, candidate.id
    )

    assert coordination is not None
    assert coordination.event.commitment_lineage_id == coordination.lineage.id
    assert coordination.scope.scope_mode == "unknown"
    assert coordination.next_decision == "owner"
    assert coordination.authority_gap is None
    assert coordination.roster == (owner,)
    assert coordination.evidence[0].quote == (
        f"{party.name} will provide the chain of title in June 2025."
    )


def test_admitted_statement_coordination_records_only_the_current_residue(
    session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    owner = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:residual-plan-owner",
        display_name="Residual Plan Owner",
    )
    session.add(owner)
    session.flush()

    assign_admitted_statement_owner(
        session,
        project.id,
        candidate.id,
        owner.id,
        principal=RECORDER,
    )
    after_owner = read_admitted_statement_coordination(
        session, project.id, candidate.id
    )
    assert after_owner.next_decision == "next_action"

    set_admitted_statement_next_action(
        session,
        project.id,
        candidate.id,
        "Confirm the External Party and Commitment Scope",
        due_date=None,
        due_date_unknown_reason="date_not_yet_known",
        principal=RECORDER,
    )
    completed = read_admitted_statement_coordination(
        session, project.id, candidate.id
    )

    assert completed.next_decision is None
    assert completed.lineage.internal_owner == "Residual Plan Owner"
    assert completed.lineage.next_action == (
        "Confirm the External Party and Commitment Scope"
    )


def _complete_mechanical_commitment_plan(client, project, candidate, owner):
    owned = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/owner",
        data={"internal_owner_roster_entry_id": str(owner.id)},
        follow_redirects=False,
    )
    assert owned.status_code == 303
    planned = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/next-action",
        data={
            "next_action": "Confirm the External Party and Commitment Scope",
            "action_due_date_unknown_reason": "date_not_yet_known",
        },
        follow_redirects=False,
    )
    assert planned.status_code == 303
    return planned.headers["location"]


def _record_deferred_month_commitments(session, project, party, count):
    statements = []
    for number in range(1, count + 1):
        quote = f"{party.name} to provide accepted package {number:02d} (Due 01/2025)."
        document = Document(
            project_id=project.id,
            sha256=hashlib.sha256(quote.encode()).hexdigest(),
            filename=f"accepted/deferred-{number:02d}.pdf",
            doc_type="minutes",
            doc_date=date(2025, 1, 16),
            parse_status="parsed",
        )
        session.add(document)
        session.flush()
        session.add(DocPage(document_id=document.id, page_no=1, text=quote))
        session.flush()
        statement = record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=date(2025, 1, 16),
            description=quote,
            new_timing=StatementTiming.month("01/2025", 2025, 1),
            scope=StatementScope.unknown(),
            created_by=RECORDER.subject,
            evidence=CitedStatementEvidence(document.id, 1, quote),
        )
        defer_work(
            session,
            CoordinationSubject.statement(statement.commitment_lineage_id),
            reason="waiting_for_external_party",
            return_date=date(2099, 1, 1),
            principal=RECORDER,
        )
        statements.append(statement)
    return tuple(statements)


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
    )
    session.add(critical)
    session.flush()
    [candidate] = _record_pending_statement_candidates(
        session,
        project,
        [
            {
                "event_type": "commitment",
                "event_date": date(2025, 1, 20),
                "committed_date": "2025-03-01",
                "quote": "External Party statement needs coordinator review.",
                "reason": "no_conflict_reference",
            }
        ],
    )

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


def test_work_list_caps_immediate_cards_after_authoritative_work_and_orders_candidate_leads(
    session, project, party
):
    """Accepted facts stay ahead; Candidate metadata only selects what to read."""
    statement = _record_month_commitment(session, project, party)
    critical = Dependency(
        project_id=project.id,
        ref_code="DEP-BOUNDED-WORK-LIST",
        dep_type="utility_relocation",
        title="Critical relocation without a Coordination Plan",
        resolution_strategy="relocate",
    )
    session.add(critical)
    session.flush()

    specifications = [
        {
            "event_type": "slip",
            "event_date": date(2025, 1, 10),
            "quote": "The January schedule may move to March.",
            "reason": "event_type_outside_policy",
            "confidence": 0.99,
        },
        {
            "event_type": "slip",
            "event_date": date(2025, 3, 10),
            "quote": "The March schedule may move to May.",
            "reason": "event_type_outside_policy",
            "confidence": 0.01,
        },
    ]
    specifications.extend(
        {
            "event_type": "commitment",
            "event_date": date(2025, 4, day),
            "committed_date": "2025-05-01",
            "quote": f"Example Party expects to finish work in May, note {day}.",
            "reason": "no_conflict_reference",
            "confidence": (20 - day) / 100,
        }
        for day in range(1, 20)
    )
    candidates = _record_pending_statement_candidates(session, project, specifications)

    work_list = build_work_list(session, project.id, today=date(2025, 6, 1))

    assert len(work_list.immediate) == 20
    assert work_list.immediate[0].statement_event_id == statement.id
    assert work_list.immediate[1].dependency_id == critical.id
    assert [item.candidate_id for item in work_list.immediate[2:5]] == [
        candidates[1].id,
        candidates[0].id,
        candidates[-1].id,
    ]
    assert work_list.immediate[2].candidate_decision == (
        "Review a possible timing change."
    )
    assert work_list.immediate[4].candidate_decision == (
        "Review who spoke, what timing is supported, and where this statement belongs."
    )
    assert all(item.kind != "candidate" for item in work_list.immediate[:2])
    assert work_list.candidate_backlog_total == 21
    assert {item.candidate_id for item in work_list.candidate_backlog} == {
        candidate.id for candidate in candidates
    }


def test_candidate_lead_exposes_source_context_without_promoting_extracted_facts(
    session, project
):
    quote = (
        "Equistar to provide a chain of title on the ROW agreement that is in "
        "DOW's name (Due date of 01/2025)."
    )
    [candidate] = _record_pending_statement_candidates(
        session,
        project,
        [
            {
                "event_type": "commitment",
                "event_date": date(2024, 12, 4),
                "committed_date": "2025-01-01",
                "external_org": "Equistar",
                "quote": quote,
                "reason": "no_conflict_reference",
            }
        ],
    )

    work_list = build_work_list(session, project.id, today=date(2025, 2, 1))

    [item] = work_list.immediate
    assert item.candidate_id == candidate.id
    assert item.statement_event_id is None
    assert item.dependency_id is None
    assert item.timing_text is None
    assert item.candidate_decision == (
        "Review who spoke, what timing is supported, and where this statement belongs."
    )
    assert item.candidate_source is not None
    assert item.candidate_source.context_external_org == "Equistar"
    assert item.candidate_source.quote == quote
    assert item.candidate_source.document_name == "meeting-notes/bounded-work-list.pdf"
    assert item.candidate_source.document_date == date(2025, 1, 15)
    assert item.candidate_source.page == 1


def test_coordinator_home_labels_candidate_source_context_without_inventing_a_day(
    client, session, project
):
    quote = "Equistar to provide chain of title (Due date of 01/2025)."
    [candidate] = _record_pending_statement_candidates(
        session,
        project,
        [
            {
                "event_type": "commitment",
                "event_date": date(2024, 12, 4),
                "committed_date": "2025-01-01",
                "external_org": "Equistar",
                "quote": quote,
                "reason": "no_conflict_reference",
            }
        ],
    )

    response = client.get(f"/work/{project.slug}")

    assert response.status_code == 200
    assert "Extracted for review — not yet in the Ledger." in response.text
    assert "Extracted context / affected party: Equistar" in response.text
    assert quote in response.text
    assert "meeting-notes/bounded-work-list.pdf" in response.text
    assert "2025-01-15" in response.text
    assert "page 1" in response.text
    assert (
        "Review who spoke, what timing is supported, and where this statement belongs."
        in response.text
    )
    assert "Decide this statement&#39;s scope." not in response.text
    assert "2025-01-01" not in response.text
    assert "January 1" not in response.text
    assert f'href="/statements/{project.slug}/{candidate.id}/coordinate"' in response.text


def test_candidate_backlog_is_searchable_and_paginated_on_the_coordinator_home(
    client, session, project
):
    specifications = [
        {
            "event_type": "status_update",
            "event_date": date(2025, 1, day),
            "committed_date": "2025-02-01",
            "external_org": f"Backlog Party {day:02d}",
            "quote": f"General extracted statement {day:02d}.",
            "reason": "event_type_outside_policy",
        }
        for day in range(1, 28)
    ]
    air_products = ExternalOrg(name="Air Products work-list-test")
    session.add(air_products)
    session.flush()
    session.add(
        Dependency(
            project_id=project.id,
            ref_code="DEP-AIR-PRODUCTS-BACKLOG",
            source_ref="PL35",
            dep_type="utility_relocation",
            title="Air Products workshop context",
            external_org_id=air_products.id,
        )
    )
    session.flush()
    specifications.append(
        {
            "event_type": "commitment",
            "event_date": date(2025, 1, 28),
            "committed_date": "2025-05-08",
            "external_org": air_products.name,
            "conflict_ref": "PL35",
            "quote": "Air Products will be invited to a May workshop.",
            "reason": "party_unstated",
        }
    )
    candidates = _record_pending_statement_candidates(session, project, specifications)

    first_page = client.get(f"/work/{project.slug}")

    assert first_page.status_code == 200
    assert "All extracted work" in first_page.text
    assert "28 extracted items" in first_page.text
    assert "Page 1 of 2" in first_page.text
    assert "Air Products will be invited to a May workshop." in first_page.text
    assert "General extracted statement 27." in first_page.text
    assert "General extracted statement 04." in first_page.text
    assert "General extracted statement 03." not in first_page.text
    assert f'href="/statements/{project.slug}/{candidates[-1].id}/coordinate"' in first_page.text
    assert f"statement_page=2" in first_page.text

    second_page = client.get(f"/work/{project.slug}?statement_page=2")

    assert "General extracted statement 03." in second_page.text
    assert "General extracted statement 01." in second_page.text
    assert "Air Products will be invited" not in second_page.text

    searched = client.get(f"/work/{project.slug}?statement_search=Air+Products")

    assert searched.status_code == 200
    assert "1 extracted item" in searched.text
    assert "Air Products will be invited to a May workshop." in searched.text
    assert "General extracted statement 01." not in searched.text


def test_work_list_includes_every_active_run_dependency_proposal_with_ordinary_queue_links(
    client, session, project
):
    [statement] = _record_pending_statement_candidates(
        session,
        project,
        [
            {
                "event_type": "status_update",
                "event_date": date(2025, 1, 21),
                "external_org": "Equistar",
                "quote": "Equistar discussed current utility coordination.",
                "reason": "event_type_outside_policy",
            }
        ],
    )
    dependencies = _record_pending_dependency_candidates(
        session,
        project,
        [
            {
                "conflict_ref": f"PL{number:02d}",
                "external_org": "Equistar",
                "description": f"Equistar utility crossing {number:02d}",
                "quote": f"PL{number:02d} Equistar crossing at station {number:02d}+00.",
            }
            for number in range(1, 28)
        ],
    )

    work_list = build_work_list(session, project.id)

    assert work_list.event_candidate_total == 1
    assert work_list.dependency_candidate_total == 27
    assert work_list.candidate_backlog_total == 28
    assert {item.candidate_id for item in work_list.candidate_backlog}.issubset(
        {statement.id, *(candidate.id for candidate in dependencies)}
    )

    first_page = client.get(f"/work/{project.slug}")

    assert first_page.status_code == 200
    assert "1 extracted External Party statement" in first_page.text
    assert "27 extracted Dependencies" in first_page.text
    assert "28 extracted items" in first_page.text
    assert "Page 1 of 2" in first_page.text
    assert "PL24 Equistar crossing at station 24+00." in first_page.text
    assert "PL27 Equistar crossing at station 27+00." not in first_page.text
    assert "utility-matrix/current-active-run.pdf" in first_page.text

    second_page = client.get(f"/work/{project.slug}?statement_page=2")
    assert "PL27 Equistar crossing at station 27+00." in second_page.text

    exact = dependencies[22]
    searched = client.get(
        f"/work/{project.slug}?statement_search=PL23+Equistar+crossing"
    )
    assert searched.status_code == 200
    assert "1 extracted item" in searched.text
    assert "PL23 Equistar crossing at station 23+00." in searched.text
    assert (
        f'href="/queue/{project.slug}?lane=candidate&amp;mode=review&amp;candidate_id={exact.id}"'
        in searched.text
    )

    queue = client.get(
        f"/queue/{project.slug}?lane=candidate&mode=review&candidate_id={exact.id}"
    )
    assert queue.status_code == 200
    assert "PL23 Equistar crossing at station 23+00." in queue.text
    assert "External Party identity not established" in queue.text
    assert (
        f'action="/candidates/{exact.id}/keep-unresolved"'
        in queue.text
    )

    kept = client.post(
        f"/candidates/{exact.id}/keep-unresolved",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert kept.status_code == 303
    session.refresh(exact)
    assert exact.state == "pending"
    receipt = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "candidate",
            AuditLog.entity_id == exact.id,
            AuditLog.action == "keep_candidate_unresolved",
        )
        .order_by(AuditLog.id.desc())
    )
    assert receipt is not None
    assert receipt.actor == RECORDER.subject
    assert receipt.human_principal == RECORDER.subject
    assert receipt.after_json["abstention_reason"] == "no_row_identity"

    acknowledged = client.get(kept.headers["location"])
    assert acknowledged.status_code == 200
    assert "Kept unresolved in the Work List" in acknowledged.text
    assert "Unresolved acknowledgment history" in acknowledged.text


def test_authoritative_overflow_is_searchable_and_paginated_in_project_language(
    client, session, project
):
    party = ExternalOrg(name="Overflow Utility work-list-test")
    session.add(party)
    session.flush()
    session.add_all(
        Dependency(
            project_id=project.id,
            ref_code=f"DEP-OVERFLOW-{number:02d}",
            dep_type="utility_relocation",
            title=f"North corridor relocation {number:02d}",
            external_org_id=party.id,
            resolution_strategy="relocate",
        )
        for number in range(1, 49)
    )
    session.flush()

    first_page = client.get(f"/work/{project.slug}")

    assert first_page.status_code == 200
    assert "28 more work items" in first_page.text
    assert "Page 1 of 2" in first_page.text
    assert "Overflow Utility work-list-test" in first_page.text
    assert "North corridor relocation 21" in first_page.text
    assert "North corridor relocation 45" in first_page.text
    assert "North corridor relocation 46" not in first_page.text
    assert "DEP-OVERFLOW" not in first_page.text
    assert "work_page=2" in first_page.text
    assert 'name="work_search"' in first_page.text
    assert 'name="statement_search"' in first_page.text

    second_page = client.get(f"/work/{project.slug}?work_page=2")

    assert "North corridor relocation 46" in second_page.text
    assert "North corridor relocation 48" in second_page.text
    assert "North corridor relocation 21" not in second_page.text

    searched = client.get(
        f"/work/{project.slug}?work_search=North+corridor+relocation+48"
    )

    assert searched.status_code == 200
    assert "1 more work item" in searched.text
    assert "North corridor relocation 48" in searched.text
    assert "North corridor relocation 21" not in searched.text


def test_deferred_statement_is_searchable_by_party_and_supported_wording(
    client, session, project, party
):
    statement = _record_month_commitment(session, project, party)
    defer_work(
        session,
        CoordinationSubject.statement(statement.commitment_lineage_id),
        reason="waiting_for_external_party",
        return_date=date(2099, 1, 1),
        principal=RECORDER,
    )

    response = client.get(f"/work/{project.slug}?work_search=chain+of+title")

    assert response.status_code == 200
    assert "1 more work item" in response.text
    assert party.name in response.text
    assert "provide chain of title" in response.text
    assert "commitment_lineage_id" not in response.text


def test_candidate_work_list_build_has_a_bounded_query_count_at_review_scale(
    session, project
):
    party = ExternalOrg(name="Scale Party work-list-test")
    session.add(party)
    session.flush()
    specifications = [
        {
            "event_type": "commitment",
            "event_date": date(2025, 1, 15),
            "committed_date": "2025-05-01",
            "external_org": party.name,
            "stated_party": party.name,
            "quote": f"{party.name} committed to finish in May, statement {number:02d}.",
            "reason": "no_conflict_reference",
        }
        for number in range(1, 51)
    ]
    _record_pending_statement_candidates(session, project, specifications)
    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", count_statement)
    try:
        work_list = build_work_list(session, project.id, today=date(2025, 2, 1))
    finally:
        event.remove(connection, "before_cursor_execute", count_statement)

    assert len(work_list.immediate) == 20
    assert work_list.candidate_backlog_total == 50
    assert statements <= 12


def test_deferred_accepted_statement_backlog_has_a_bounded_query_count_at_scale(
    session, project, party
):
    _record_deferred_month_commitments(session, project, party, 50)
    statements = 0

    def count_statement(*_args):
        nonlocal statements
        statements += 1

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", count_statement)
    try:
        work_list = build_work_list(session, project.id, today=date(2025, 2, 1))
    finally:
        event.remove(connection, "before_cursor_execute", count_statement)

    assert work_list.immediate == ()
    assert work_list.backlog_total == 50
    assert len(work_list.backlog) == 25
    assert all(item.past_due is not None for item in work_list.backlog)
    assert all(
        item.past_due.source_evidence_link_ids
        for item in work_list.backlog
        if item.past_due is not None
    )
    assert statements <= 25


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


def test_mechanically_admitted_commitment_links_to_its_residual_work(
    client, session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)

    work_list = build_work_list(session, project.id, today=date(2025, 5, 1))
    [item] = work_list.immediate
    assert item.kind == "statement"
    assert item.source_candidate_id == candidate.id
    assert item.attention_reason_codes == (
        "unknown_scope",
        "missing_internal_owner",
        "missing_next_action",
    )

    page = client.get(f"/work/{project.slug}")
    assert page.status_code == 200
    assert f'href="/statements/{project.slug}/{candidate.id}/coordinate"' in page.text
    assert "Review extracted statement" not in page.text
    assert "Open statement plan" in page.text


def test_mechanical_commitment_shows_known_unknown_scope_and_owner_first(
    client, session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    session.add(
        ProjectRosterEntry(
            project_id=project.id,
            principal_subject="local:read-only-owner",
            display_name="Read Only Owner",
        )
    )
    session.add(
        Dependency(
            project_id=project.id,
            ref_code="EQ-READ-ONLY",
            source_ref="EQ-READ-ONLY",
            dep_type="utility_relocation",
            title="Equistar read-only screen choice",
            station_from="245+00",
            station_to="445+00",
            external_org_id=party.id,
        )
    )
    session.flush()
    response = client.get(
        f"/statements/{project.slug}/{candidate.id}/coordinate"
    )

    assert response.status_code == 200
    body = response.text
    assert "Accepted External Party Commitment" in body
    assert party.name in body
    assert "June 2025" in body
    assert "month precision" in body
    assert "Commitment Scope not yet known" in body
    assert UNKNOWN_SCOPE_POLICY_VERSION in body
    assert "Verified Evidence" in body
    assert f"{party.name} will provide the chain of title in June 2025." in body
    assert "The accepted Evidence does not identify an affected Dependency." in body
    assert "Corridor keeps this Commitment at party level." in body
    assert "Choose the Commitment Scope" not in body
    assert "Keep Commitment Scope not yet known" not in body
    assert 'name="scope_mode"' not in body
    assert 'name="dependency_id"' not in body
    assert 'name="affected_external_org_id"' not in body
    assert 'name="stated_external_org_id"' not in body
    assert 'name="description"' not in body
    assert 'name="new_timing_precision"' not in body
    assert 'name="internal_owner_roster_entry_id"' in body
    assert 'name="next_action"' not in body
    assert "Evidence Investigator" not in body
    assert "confidence" not in body.lower()
    assert "Change Commitment Scope" not in body


def test_unknown_scope_with_no_dependency_choices_does_not_block_the_plan(
    client, session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    owner = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:gap-owner",
        display_name="Gap Owner",
    )
    session.add(owner)
    session.flush()

    response = client.get(f"/statements/{project.slug}/{candidate.id}/coordinate")

    assert response.status_code == 200
    body = response.text
    assert "No active Dependency choices are registered" not in body
    assert 'name="scope_mode"' not in body
    assert 'name="internal_owner_roster_entry_id"' in body

    owned = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/owner",
        data={"internal_owner_roster_entry_id": str(owner.id)},
        follow_redirects=False,
    )
    assert owned.status_code == 303
    action_page = client.get(owned.headers["location"]).text
    assert "Choose the Next Action" in action_page
    assert 'name="next_action"' in action_page
    assert 'name="scope_mode"' not in action_page

    planned = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/next-action",
        data={
            "next_action": "Confirm the External Party and Commitment Scope",
            "action_due_date_unknown_reason": "date_not_yet_known",
        },
        follow_redirects=False,
    )
    assert planned.status_code == 303
    final_page = client.get(planned.headers["location"]).text
    assert "The Coordination Plan is recorded." in final_page
    assert "Corridor keeps this Commitment at party level." in final_page
    assert "Change Commitment Scope" not in final_page
    assert 'name="scope_mode"' not in final_page


def test_mechanical_commitment_keeps_malformed_owner_identity_on_the_guided_screen(
    client, session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    session.add(
        ProjectRosterEntry(
            project_id=project.id,
            principal_subject="local:malformed-owner-choice",
            display_name="Malformed Owner Choice",
        )
    )
    session.flush()

    response = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/owner",
        data={"internal_owner_roster_entry_id": "not-an-identity"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Choose the Internal Owner" in response.text
    assert "internal_owner_roster_entry_id must be a positive identity" in response.text


def test_mechanical_commitment_asks_owner_then_structured_next_action(
    client, session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    dependency = Dependency(
        project_id=project.id,
        ref_code="EQ-1",
        source_ref="EQ-1",
        dep_type="utility_relocation",
        title="Equistar line",
        station_from="245+00",
        station_to="445+00",
        external_org_id=party.id,
    )
    owner = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:work-list-owner",
        display_name="Work List Owner",
    )
    session.add_all((dependency, owner))
    session.flush()

    owner_page = client.get(
        f"/statements/{project.slug}/{candidate.id}/coordinate"
    ).text
    assert "Choose the Internal Owner" in owner_page
    assert 'name="internal_owner_roster_entry_id"' in owner_page
    assert 'name="scope_mode"' not in owner_page
    assert 'name="next_action"' not in owner_page

    owned = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/owner",
        data={"internal_owner_roster_entry_id": str(owner.id)},
        follow_redirects=False,
    )
    assert owned.status_code == 303
    action_page = client.get(owned.headers["location"]).text
    assert "Choose the Next Action" in action_page
    assert 'name="next_action"' in action_page
    assert 'name="action_due_date"' in action_page
    assert 'name="action_due_date_unknown_reason"' in action_page
    assert 'name="internal_owner_roster_entry_id"' not in action_page
    assert 'name="scope_mode"' not in action_page

    planned = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/next-action",
        data={
            "next_action": "Confirm the External Party and Commitment Scope",
            "action_due_date_unknown_reason": "date_not_yet_known",
        },
        follow_redirects=False,
    )
    assert planned.status_code == 303
    final_page = client.get(planned.headers["location"]).text
    assert "The Coordination Plan is recorded." in final_page
    assert "Current Commitment Scope" in final_page
    assert "Not yet known." in final_page
    assert "Corridor keeps this Commitment at party level." in final_page
    assert "Change Commitment Scope" not in final_page
    assert 'name="scope_mode"' not in final_page
    assert 'name="dependency_id"' not in final_page

    outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == candidate.id,
            EventAdmissionOutcome.outcome == "admitted",
        )
    )
    decisions = tuple(
        session.scalars(
            select(DependencyEventScopeDecision)
            .where(
                DependencyEventScopeDecision.event_id
                == outcome.dependency_event_id
            )
            .order_by(DependencyEventScopeDecision.id)
        ).all()
    )
    assert len(decisions) == 1
    assert decisions[0].decided_by == "corridor:event-admission"


def test_machine_unknown_scope_is_not_saved_again_as_a_human_decision(
    client, session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    owner = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:unknown-scope-owner",
        display_name="Unknown Scope Owner",
    )
    session.add(owner)
    session.flush()
    _complete_mechanical_commitment_plan(client, project, candidate, owner)

    response = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/scope",
        data={"scope_mode": "unknown"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    page = response.text
    assert "Use Correct to change Commitment Scope after admission." in page
    assert "Commitment Scope not yet known" in page
    outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == candidate.id,
            EventAdmissionOutcome.outcome == "admitted",
        )
    )
    decisions = tuple(
        session.scalars(
            select(DependencyEventScopeDecision)
            .where(
                DependencyEventScopeDecision.event_id
                == outcome.dependency_event_id
            )
            .order_by(DependencyEventScopeDecision.id)
        ).all()
    )
    assert len(decisions) == 1
    assert decisions[0].decided_by == "corridor:event-admission"


def test_scope_can_be_identified_after_the_coordination_plan(
    client, session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    dependency = Dependency(
        project_id=project.id,
        ref_code="EQ-SCOPED",
        source_ref="PL19",
        dep_type="utility_relocation",
        title="Equistar pipeline",
        station_from="6350+04",
        station_to="6354+23",
        external_org_id=party.id,
    )
    owner = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:scoped-owner",
        display_name="Scoped Owner",
    )
    session.add_all((dependency, owner))
    session.flush()
    _complete_mechanical_commitment_plan(client, project, candidate, owner)

    outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == candidate.id,
            EventAdmissionOutcome.outcome == "admitted",
        )
    )
    admitted_page = client.get(f"/statements/{project.slug}/{candidate.id}/coordinate")
    assert admitted_page.status_code == 200
    assert "Correct accepted facts or Commitment Scope" in admitted_page.text
    assert (
        f'href="/statements/{project.slug}/{candidate.id}/correct"'
        in admitted_page.text
    )
    refused = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/scope",
        data={"scope_mode": "selected", "dependency_id": str(dependency.id)},
        follow_redirects=False,
    )
    assert refused.status_code == 400
    assert "Use Correct to change Commitment Scope after admission." in refused.text
    current_scope = session.scalar(
        select(DependencyEventScopeDecision)
        .where(
            DependencyEventScopeDecision.event_id == outcome.dependency_event_id,
            current_scope_decision_filter(),
        )
        .order_by(DependencyEventScopeDecision.id.desc())
        .limit(1)
    )
    correct_page = client.get(f"/statements/{project.slug}/{candidate.id}/correct")
    assert correct_page.status_code == 200
    scoped = client.post(
        f"/statements/{project.slug}/{candidate.id}/correct/scope",
        data={
            "expected_statement_event_id": str(outcome.dependency_event_id),
            "expected_scope_decision_id": str(current_scope.id),
            "scope_mode": "selected",
            "dependency_id": str(dependency.id),
        },
        follow_redirects=False,
    )

    assert scoped.status_code == 303
    decisions = tuple(
        session.scalars(
            select(DependencyEventScopeDecision)
            .where(
                DependencyEventScopeDecision.event_id
                == outcome.dependency_event_id
            )
            .order_by(DependencyEventScopeDecision.id)
        ).all()
    )
    assert [decision.scope_mode for decision in decisions] == [
        "unknown",
        "selected",
    ]
    [link] = session.scalars(
        select(DependencyEventScope).where(
            DependencyEventScope.scope_decision_id == decisions[-1].id
        )
    ).all()
    assert link.dependency_id == dependency.id
    page = client.get(scoped.headers["location"]).text
    assert "PL19 — Equistar pipeline" in page
    assert "Change Commitment Scope" not in page
    assert 'name="scope_mode"' not in page


def test_admitted_scope_route_refuses_even_when_selected_dependencies_are_missing(
    client, session, project, party
):
    candidate = _mechanically_admit_unknown_scope(session, project, party)
    owner = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:empty-scope-owner",
        display_name="Empty Scope Owner",
    )
    session.add(owner)
    session.flush()
    _complete_mechanical_commitment_plan(client, project, candidate, owner)
    response = client.post(
        f"/statements/{project.slug}/{candidate.id}/admitted/scope",
        data={"scope_mode": "selected"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    body = response.text
    assert "Use Correct to change Commitment Scope after admission." in body
