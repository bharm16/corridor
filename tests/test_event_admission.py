"""Event admission: an authorized policy, replayable checks, abstention.

ADR-0026: an event Candidate enters the Ledger through a named, versioned
policy an accountable human authorizes — every check a computation anyone
can re-run — or through Adjudication, and through nothing else. A model
may demote an event into the human pile and may never promote one, so no
model verdict appears in any check here.

The shape mirrors the Carry-Forward family (ADR-0022): authorization
covers the rules rather than rows, a changed policy version needs new
authorization, each run is an immutable receipt of exact outcomes, and an
event the policy cannot prove eligible abstains — left pending for
Adjudication rather than forced onto the record.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from copy import deepcopy
from datetime import date
from pathlib import Path
import subprocess
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import object_session

from corridor.adjudicate import accept_candidate
from corridor.config import settings
from corridor.db import Session, engine
from corridor.event_admission import (
    ABSTENTION_REASON_VERSION,
    EVENT_ADMISSION_POLICY_VERSION,
    UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
    UNKNOWN_SCOPE_POLICY_VERSION,
    EventAdmissionAbstention,
    run_event_admission,
    canonical_event_admission_policy,
    _current_migration_head,
    _current_source_revision,
    read_event_admission_policy_status,
)
from corridor.event_admission_acceptance import (
    RECEIPT_VERSION,
    SELECTION_RULE,
    _promotion_gates,
    _receipt_promotion_gates,
    activate_passing_acceptance,
    lift_unknown_scope_admission,
    record_acceptance_receipt,
    suspend_unknown_scope_admission,
)
from corridor.event_admission_acceptance_cli import (
    main as event_admission_acceptance_cli_main,
)
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_closure,
    record_external_party_statement,
)
from corridor import policy
from corridor.extraction_runs import (
    declare_active_run,
    declare_single_run_documents,
    record_extraction_run,
)
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DocPage,
    Document,
    EvidenceLink,
    EventAdmissionAcceptanceReceipt,
    EventAdmissionOutcome,
    EventAdmissionActivation,
    ExternalOrg,
    CandidateDisposition,
    CommitmentLineage,
    DependencyEventScopeDecision,
    PolicyRun,
    Project,
)
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal

OPERATOR = HumanPrincipal("local:event-admission-operator")
PIPELINE = "Tejas Pipeline Co"
PROJECT_SIDE = "LJA"


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture(scope="module")
def event_admission_isolated_database():
    """One migrated disposable database for cross-session and replay proofs."""

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=Path(__file__).resolve().parents[1],
        error_cls=RuntimeError,
        database_prefix="corridor_event_admission_race_",
    ) as database:
        yield database


@pytest.fixture
def project(session):
    p = Project(
        slug="event-admission-test",
        name="Event Admission Test",
        is_synthetic=True,
        project_side_parties=[PROJECT_SIDE],
    )
    session.add(p)
    session.flush()
    return p


def _document(session, project, *, filename, doc_type):
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(filename.encode()).hexdigest(),
        filename=filename,
        doc_type=doc_type,
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text="rows"))
    session.flush()
    return document


def _candidate(document, *, kind, fields, verified=True):
    quote = " | ".join(str(v) for v in fields.values() if v is not None)
    return Candidate(
        project_id=document.project_id,
        kind=kind,
        payload_json={
            "kind": kind,
            "fields": fields,
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": verified,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": quote,
            "text_source": "text_layer",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="minutes_v1",
        model="gpt-test",
        citations_verified=verified,
    )


def _run(session, document, candidates):
    for candidate in candidates:
        session.add(candidate)
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes_v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="gpt-test",
        schema_version="matrix_candidate_shape_v1",
        allow_unsealed_legacy=True,
    )
    session.flush()
    return run


def _event(
    *,
    event_type="commitment",
    ref="PL1",
    org=PIPELINE,
    event_date="2025-01-16",
    committed_date="2025-06-01",
    description=None,
    stated_party=None,
):
    return {
        "event_type": event_type,
        "description": description or f"{org} spoke about {ref}",
        "external_org": org,
        "stated_party": stated_party if stated_party is not None else org,
        "event_date": event_date,
        "committed_date": committed_date,
        "conflict_ref": ref,
    }


@pytest.fixture
def admitted(session, project):
    """One admitted Dependency (PL1, Tejas) — events attach to records."""
    matrix = _document(session, project, filename="ucm.pdf", doc_type="matrix")
    candidate = _candidate(
        matrix,
        kind="dependency",
        fields={
            "utility_id": "PL1",
            "external_org": PIPELINE,
            "utility_type": "Petroleum and Gaseous Materials",
            "baseline": "SR-BL",
            "station_from": "1102+20",
            "station_to": "1102+80",
        },
    )
    _run(session, matrix, [candidate])
    declare_single_run_documents(session, project.id, principal=OPERATOR)
    dependency = accept_candidate(session, candidate, principal=OPERATOR)
    session.flush()
    return dependency


def _minutes_with(
    session, project, event_fields_list, *, verified=True, filename="minutes.pdf"
):
    minutes = _document(
        session, project, filename=filename, doc_type="minutes"
    )
    candidates = [
        _candidate(minutes, kind="event", fields=f, verified=verified)
        for f in event_fields_list
    ]
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == minutes.id,
            DocPage.page_no == 1,
        )
    )
    assert page is not None
    page.text = "\n".join(
        candidate.payload_json["citations"][0]["quote"]
        for candidate in candidates
    )
    run = _run(session, minutes, candidates)
    declare_active_run(session, minutes.id, run.id, principal=OPERATOR)
    return candidates


def _events_on(session, dependency_id):
    return session.scalars(
        select(DependencyEvent)
        .join(DependencyEventScope, DependencyEventScope.event_id == DependencyEvent.id)
        .where(DependencyEventScope.dependency_id == dependency_id)
    ).all()


# ── The checks ───────────────────────────────────────────────────────────


def test_a_clean_event_is_admitted_onto_its_dependency(
    session, project, admitted
):
    [candidate] = _minutes_with(session, project, [_event()])

    result = run_event_admission(session, project.id)
    assert result.admitted_count == 1
    assert result.abstained_count == 0

    [event] = _events_on(session, admitted.id)
    assert event.event_type == "commitment"
    assert event.event_date == date(2025, 1, 16)
    session.refresh(candidate)
    assert candidate.state == "accepted"


def test_explicit_event_write_scope_mutates_only_the_named_actionable_candidate(
    session, project, admitted
):
    scoped, unscoped = _minutes_with(
        session,
        project,
        [
            _event(description="Tejas Pipeline committed PL1 for June."),
            _event(
                committed_date="2025-07-01",
                description="Tejas Pipeline committed PL1 for July.",
            ),
        ],
    )

    result = run_event_admission(
        session,
        project.id,
        policy_version=EVENT_ADMISSION_POLICY_VERSION,
        write_candidate_ids=(scoped.id,),
    )

    assert result.admitted_count == 1
    assert result.abstained_count == 0
    outcomes = session.scalars(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == result.run_id
        )
    ).all()
    assert [outcome.candidate_id for outcome in outcomes] == [scoped.id]
    session.refresh(scoped)
    session.refresh(unscoped)
    assert scoped.state == "accepted"
    assert unscoped.state == "pending"
    assert len(_events_on(session, admitted.id)) == 1

    run = session.get(PolicyRun, result.run_id)
    exact_policy = canonical_event_admission_policy(
        project,
        EVENT_ADMISSION_POLICY_VERSION,
        write_candidate_ids=(scoped.id,),
    )
    assert exact_policy["write_candidate_ids"] == [scoped.id]
    assert run.policy_sha256 == policy.canonical_sha256(exact_policy)


def test_explicit_event_write_scope_rejects_non_actionable_ids(
    session, project
):
    minutes = _document(
        session, project, filename="invalid-scope-minutes.pdf", doc_type="minutes"
    )
    candidate = _candidate(
        minutes,
        kind="event",
        fields=_event(),
    )
    _run(session, minutes, [candidate])
    # No Active Run declaration: the row is not currently actionable.

    with pytest.raises(ValueError, match="current actionable pending event"):
        run_event_admission(
            session,
            project.id,
            policy_version=EVENT_ADMISSION_POLICY_VERSION,
            write_candidate_ids=(candidate.id,),
        )


def test_unknown_scope_policy_admits_one_exact_party_level_commitment(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=["Tejas Pipeline"]))
    session.flush()
    fields = _event(
        ref=None,
        committed_date={
            "text": "June 2025",
            "precision": "month",
            "start_date": "2025-06-01",
            "end_date": "2025-06-30",
        },
        description="Tejas Pipeline will deliver the Barlow calculation in June 2025.",
        stated_party="Tejas Pipeline",
    )
    [candidate] = _minutes_with(session, project, [fields])

    predecessor = run_event_admission(session, project.id)
    assert predecessor.admitted_count == 0
    assert predecessor.abstentions[0].reason == "no_conflict_reference"

    result = run_event_admission(
        session,
        project.id,
        policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
    )

    assert result.admitted_count == 1
    event = session.scalar(
        select(DependencyEvent)
        .where(
            DependencyEvent.project_id == project.id,
            DependencyEvent.created_by == "corridor:event-admission",
        )
        .order_by(DependencyEvent.id.desc())
        .limit(1)
    )
    assert event is not None
    assert event.event_type == "commitment"
    assert event.scope_mode == "unknown"
    assert event.affected_external_org_id == event.stated_external_org_id
    assert session.scalars(
        select(DependencyEventScope).where(DependencyEventScope.event_id == event.id)
    ).all() == []
    scope = session.scalar(
        select(DependencyEventScopeDecision).where(
            DependencyEventScopeDecision.event_id == event.id
        )
    )
    assert scope is not None
    assert scope.scope_mode == "unknown"
    disposition = session.scalar(
        select(CandidateDisposition).where(
            CandidateDisposition.candidate_id == candidate.id
        )
    )
    assert disposition is not None
    assert disposition.disposition == "accepted"
    assert disposition.recorded_by == "corridor:event-admission"
    outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == result.run_id,
            EventAdmissionOutcome.candidate_id == candidate.id,
        )
    )
    assert outcome is not None
    assert outcome.commitment_lineage_id == event.commitment_lineage_id
    assert outcome.scope_decision_id == scope.id
    assert outcome.candidate_disposition_id == disposition.id
    assert outcome.audit_log_id is not None
    assert outcome.eligibility_sha256 is not None
    assert outcome.eligibility_json["candidate_id"] == candidate.id
    assert fields["description"] in outcome.eligibility_json["evidence"]["quote"]


def test_unknown_scope_policy_honors_the_exact_event_write_scope(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    scoped, unscoped = _minutes_with(
        session,
        project,
        [
            _event(
                ref=None,
                committed_date="2025-06-01",
                description="Tejas Pipeline will deliver the title package by June.",
            ),
            _event(
                ref=None,
                committed_date="2025-07-01",
                description="Tejas Pipeline will deliver the permit package by July.",
            ),
        ],
    )

    result = run_event_admission(
        session,
        project.id,
        policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
        write_candidate_ids=(scoped.id,),
    )

    assert result.admitted_count == 1
    outcomes = session.scalars(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == result.run_id
        )
    ).all()
    assert [outcome.candidate_id for outcome in outcomes] == [scoped.id]
    session.refresh(scoped)
    session.refresh(unscoped)
    assert scoped.state == "accepted"
    assert unscoped.state == "pending"
    assert session.scalar(
        select(CandidateDisposition).where(
            CandidateDisposition.candidate_id == unscoped.id
        )
    ) is None


def test_unknown_scope_policy_is_opt_in_and_idempotent(session, project):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [candidate] = _minutes_with(
        session,
        project,
        [
            _event(
                ref=None,
                committed_date="2025-06-01",
                description="Tejas Pipeline will deliver by 2025-06-01.",
            )
        ],
    )

    default = run_event_admission(session, project.id)
    assert default.admitted_count == 0
    assert session.get(Candidate, candidate.id).state == "pending"

    first = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    second = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert first.admitted_count == 1
    assert second.admitted_count == 0
    assert session.scalar(
        select(func.count()).select_from(DependencyEvent).where(
            DependencyEvent.project_id == project.id
        )
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(CandidateDisposition)
        .join(Candidate, Candidate.id == CandidateDisposition.candidate_id)
        .where(Candidate.project_id == project.id)
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(EventAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            EventAdmissionOutcome.outcome == "admitted",
        )
    ) == 1


def _unknown_scope_commitment(
    *, event_date="2025-01-16", description=None, timing=None
):
    return _event(
        ref=None,
        event_date=event_date,
        committed_date=timing
        or {
            "text": "June 2025",
            "precision": "month",
            "start_date": "2025-06-01",
            "end_date": "2025-06-30",
        },
        description=description
        or "Tejas Pipeline will deliver the Barlow calculation in June 2025.",
    )


def _reextract_candidate(session, project, original, fields):
    document = session.get(Document, original.source_document_id)
    assert document is not None
    candidate = _candidate(document, kind="event", fields=fields)
    run = _run(session, document, [candidate])
    declare_active_run(session, document.id, run.id, principal=OPERATOR)
    return candidate


def test_unknown_scope_policy_abstains_from_same_document_duplicate_commitment(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    first_fields = _unknown_scope_commitment()
    duplicate_fields = _unknown_scope_commitment(
        event_date="2025-02-20",
        description=(
            "  TEJAS PIPELINE will deliver the Barlow calculation in June 2025.  "
        ),
    )
    first, duplicate = _minutes_with(
        session,
        project,
        [first_fields, duplicate_fields],
        filename="same-document-duplicate.pdf",
    )

    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert result.admitted_count == 1
    assert result.abstentions == [
        EventAdmissionAbstention(
            candidate_id=duplicate.id,
            reason="matching_open_commitment_exists",
            reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        )
    ]
    assert session.get(Candidate, first.id).state == "accepted"
    assert session.get(Candidate, duplicate.id).state == "pending"
    assert session.scalar(
        select(func.count()).select_from(CommitmentLineage).where(
            CommitmentLineage.project_id == project.id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(DependencyEvent).where(
            DependencyEvent.project_id == project.id
        )
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(CandidateDisposition)
        .join(Candidate, Candidate.id == CandidateDisposition.candidate_id)
        .where(Candidate.project_id == project.id)
    ) == 1


def test_unknown_scope_policy_abstains_from_other_document_duplicate_commitment(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [first] = _minutes_with(
        session,
        project,
        [_unknown_scope_commitment()],
        filename="first-commitment.pdf",
    )
    admitted = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert admitted.admitted_count == 1
    [duplicate] = _minutes_with(
        session,
        project,
        [_unknown_scope_commitment(event_date="2025-02-20")],
        filename="repeated-commitment.pdf",
    )

    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert result.admitted_count == 0
    assert result.abstentions == [
        EventAdmissionAbstention(
            candidate_id=duplicate.id,
            reason="matching_open_commitment_exists",
            reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        )
    ]
    assert session.get(Candidate, first.id).state == "accepted"
    assert session.get(Candidate, duplicate.id).state == "pending"
    [statement] = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.project_id == project.id,
            DependencyEvent.event_type == "commitment",
        )
    ).all()
    outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == result.run_id,
            EventAdmissionOutcome.candidate_id == duplicate.id,
        )
    )
    assert outcome is not None
    assert "commitment_registry_sha256" not in outcome.eligibility_json["input"]
    assert outcome.eligibility_json["input"]["matching_commitments"] == [
        {
            "basis": "open_facts",
            "commitment_lineage_id": statement.commitment_lineage_id,
            "statement_event_id": statement.id,
            "closure_event_id": None,
            "closure_event_date": None,
        }
    ]
    assert session.scalar(
        select(func.count()).select_from(CommitmentLineage).where(
            CommitmentLineage.project_id == project.id
        )
    ) == 1


def test_unknown_scope_source_replay_after_correction_matches_lineage_history(
    session, project
):
    party = ExternalOrg(name=PIPELINE, aliases=[])
    session.add(party)
    session.flush()
    original_fields = _unknown_scope_commitment()
    [original] = _minutes_with(
        session,
        project,
        [original_fields],
        filename="commitment-before-correction.pdf",
    )
    first = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert first.admitted_count == 1
    [root] = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.project_id == project.id,
            DependencyEvent.event_type == "commitment",
        )
    ).all()
    correction_quote = (
        "Tejas Pipeline will deliver the Barlow calculation in July 2025."
    )
    page = session.scalar(
        select(DocPage).where(DocPage.document_id == original.source_document_id)
    )
    assert page is not None
    page.text = f"{page.text}\n{correction_quote}"
    correction = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=PIPELINE,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 2, 20),
        description=correction_quote,
        new_timing=StatementTiming.month("July 2025", 2025, 7),
        previous_timing=StatementTiming.month("June 2025", 2025, 6),
        scope=StatementScope.unknown(),
        created_by=OPERATOR.subject,
        evidence=CitedStatementEvidence(
            original.source_document_id, 1, correction_quote
        ),
        commitment_lineage_id=root.commitment_lineage_id,
    )
    replay = _reextract_candidate(session, project, original, original_fields)

    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert result.admitted_count == 0
    assert result.abstentions[0].reason == "commitment_evidence_already_recorded"
    assert session.get(Candidate, replay.id).state == "pending"
    outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == result.run_id,
            EventAdmissionOutcome.candidate_id == replay.id,
        )
    )
    assert outcome is not None
    assert outcome.eligibility_json["input"]["matching_commitments"] == [
        {
            "basis": "historical_evidence",
            "commitment_lineage_id": root.commitment_lineage_id,
            "statement_event_id": root.id,
            "closure_event_id": None,
            "closure_event_date": None,
        }
    ]
    [distinct_source_replay] = _minutes_with(
        session,
        project,
        [_unknown_scope_commitment(event_date="2025-03-03")],
        filename="distinct-source-after-correction.pdf",
    )
    later = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert later.abstentions == [
        EventAdmissionAbstention(
            candidate_id=distinct_source_replay.id,
            reason="matching_open_commitment_exists",
            reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        )
    ]
    later_outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == later.run_id,
            EventAdmissionOutcome.candidate_id == distinct_source_replay.id,
        )
    )
    assert later_outcome is not None
    assert later_outcome.eligibility_json["input"]["matching_commitments"] == [
        {
            "basis": "open_lineage_history",
            "commitment_lineage_id": root.commitment_lineage_id,
            "statement_event_id": root.id,
            "closure_event_id": None,
            "closure_event_date": None,
        }
    ]
    assert correction.supersedes_event_id == root.id
    assert session.scalar(
        select(func.count()).select_from(CommitmentLineage).where(
            CommitmentLineage.project_id == project.id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(DependencyEvent).where(
            DependencyEvent.project_id == project.id
        )
    ) == 2
    assert session.scalar(
        select(func.count())
        .select_from(CandidateDisposition)
        .join(Candidate, Candidate.id == CandidateDisposition.candidate_id)
        .where(Candidate.project_id == project.id)
    ) == 1


def test_unknown_scope_duplicate_abstention_ignores_unrelated_registry_churn(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    _minutes_with(
        session,
        project,
        [_unknown_scope_commitment()],
        filename="registry-churn-original.pdf",
    )
    assert run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    ).admitted_count == 1
    [duplicate] = _minutes_with(
        session,
        project,
        [_unknown_scope_commitment(event_date="2025-02-20")],
        filename="registry-churn-duplicate.pdf",
    )
    first_duplicate = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert first_duplicate.abstained_count == 1
    _minutes_with(
        session,
        project,
        [
            _unknown_scope_commitment(
                description=(
                    "Tejas Pipeline will deliver the separate survey in July 2025."
                ),
                timing={
                    "text": "July 2025",
                    "precision": "month",
                    "start_date": "2025-07-01",
                    "end_date": "2025-07-31",
                },
            )
        ],
        filename="registry-churn-unrelated.pdf",
    )
    unrelated = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    replay = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert unrelated.admitted_count == 1
    assert unrelated.abstained_count == 0
    assert replay.admitted_count == 0
    assert replay.abstained_count == 0
    assert session.scalar(
        select(func.count()).select_from(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == duplicate.id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(DependencyEvent).where(
            DependencyEvent.project_id == project.id
        )
    ) == 2


def test_unknown_scope_ordinary_abstention_ignores_unrelated_success(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [response] = _minutes_with(
        session,
        project,
        [_event(ref=None, event_type="response")],
        filename="ordinary-abstention.pdf",
    )
    first = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert first.abstentions[0].reason == "event_type_outside_policy"
    first_outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == first.run_id,
            EventAdmissionOutcome.candidate_id == response.id,
        )
    )
    assert first_outcome is not None
    assert "commitment_registry_sha256" not in first_outcome.eligibility_json["input"]
    _minutes_with(
        session,
        project,
        [
            _unknown_scope_commitment(
                description="Tejas Pipeline will deliver the survey in August 2025.",
                timing={
                    "text": "August 2025",
                    "precision": "month",
                    "start_date": "2025-08-01",
                    "end_date": "2025-08-31",
                },
            )
        ],
        filename="unrelated-success.pdf",
    )
    unrelated = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    replay = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert unrelated.admitted_count == 1
    assert unrelated.abstained_count == 0
    assert replay.admitted_count == 0
    assert replay.abstained_count == 0
    assert session.scalar(
        select(func.count()).select_from(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == response.id
        )
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(CandidateDisposition)
        .join(Candidate, Candidate.id == CandidateDisposition.candidate_id)
        .where(Candidate.project_id == project.id)
    ) == 1


def test_unknown_scope_duplicate_guard_keeps_changed_wording_and_timing_eligible(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    _minutes_with(
        session,
        project,
        [_unknown_scope_commitment()],
        filename="original-commitment.pdf",
    )
    first = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert first.admitted_count == 1
    changed_wording = _unknown_scope_commitment(
        description="Tejas Pipeline will deliver the final calculation in June 2025."
    )
    changed_timing = _unknown_scope_commitment(
        timing={
            "text": "July 2025",
            "precision": "month",
            "start_date": "2025-07-01",
            "end_date": "2025-07-31",
        }
    )
    wording_candidate, timing_candidate = _minutes_with(
        session,
        project,
        [changed_wording, changed_timing],
        filename="materially-distinct-commitments.pdf",
    )

    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert result.admitted_count == 2
    assert result.abstained_count == 0
    assert session.get(Candidate, wording_candidate.id).state == "accepted"
    assert session.get(Candidate, timing_candidate.id).state == "accepted"
    assert session.scalar(
        select(func.count()).select_from(CommitmentLineage).where(
            CommitmentLineage.project_id == project.id
        )
    ) == 3


def test_unknown_scope_duplicate_falls_through_after_first_write_failure(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    first, fallback = _minutes_with(
        session,
        project,
        [
            _unknown_scope_commitment(event_date="2025-01-16"),
            _unknown_scope_commitment(event_date="2025-02-20"),
        ],
        filename="duplicate-write-fallback.pdf",
    )
    session.execute(
        text(
            """
            create function refuse_first_duplicate_commitment()
            returns trigger
            language plpgsql
            as $$
            begin
                if new.event_type = 'commitment'
                   and new.event_date = date '2025-01-16' then
                    raise exception 'first duplicate refused' using errcode = '23514';
                end if;
                return new;
            end;
            $$;
            """
        )
    )
    session.execute(
        text(
            """
            create trigger refuse_first_duplicate_commitment
            before insert on dependency_events
            for each row execute function refuse_first_duplicate_commitment();
            """
        )
    )

    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert result.admitted_count == 1
    assert result.abstentions == [
        EventAdmissionAbstention(
            candidate_id=first.id,
            reason="write_integrity_failure",
            reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        )
    ]
    assert session.get(Candidate, first.id).state == "pending"
    assert session.get(Candidate, fallback.id).state == "accepted"
    assert session.scalar(
        select(func.count()).select_from(DependencyEvent).where(
            DependencyEvent.project_id == project.id
        )
    ) == 1


def test_unknown_scope_duplicate_guard_does_not_match_a_closed_lineage(
    session, project
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    original_fields = _unknown_scope_commitment()
    [original] = _minutes_with(
        session,
        project,
        [original_fields],
        filename="commitment-before-closure.pdf",
    )
    session.get(Document, original.source_document_id).doc_date = date(2025, 1, 16)
    first = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert first.admitted_count == 1
    [statement] = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.project_id == project.id,
            DependencyEvent.event_type == "commitment",
        )
    ).all()
    repeat = _reextract_candidate(session, project, original, original_fields)
    blocked = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert blocked.admitted_count == 0
    assert blocked.abstentions[0].reason == "commitment_evidence_already_recorded"
    record_external_party_closure(
        session,
        project_id=project.id,
        commitment_lineage_id=statement.commitment_lineage_id,
        source_kind="verbal",
        event_date=date(2025, 2, 20),
        description="Tejas Pipeline confirmed delivery was complete.",
        created_by=OPERATOR.subject,
    )

    replay_after_closure = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    assert replay_after_closure.admitted_count == 0
    assert replay_after_closure.abstained_count == 0
    assert replay_after_closure.abstentions == []
    assert session.get(Candidate, repeat.id).state == "pending"
    [old_distinct_source] = _minutes_with(
        session,
        project,
        [_unknown_scope_commitment(event_date="2025-02-01")],
        filename="distinct-source-before-closure.pdf",
    )
    session.get(Document, old_distinct_source.source_document_id).doc_date = date(
        2025, 2, 1
    )
    [new_post_closure_source] = _minutes_with(
        session,
        project,
        [_unknown_scope_commitment(event_date="2025-03-03")],
        filename="distinct-source-after-closure.pdf",
    )
    session.get(Document, new_post_closure_source.source_document_id).doc_date = date(
        2025, 3, 3
    )

    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert result.admitted_count == 1
    assert result.abstentions == [
        EventAdmissionAbstention(
            candidate_id=old_distinct_source.id,
            reason="matching_closed_commitment_not_post_closure",
            reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        )
    ]
    assert session.get(Candidate, repeat.id).state == "pending"
    assert session.get(Candidate, old_distinct_source.id).state == "pending"
    assert session.get(Candidate, new_post_closure_source.id).state == "accepted"
    assert session.scalar(
        select(func.count()).select_from(CommitmentLineage).where(
            CommitmentLineage.project_id == project.id
        )
    ) == 2


def test_identical_unknown_scope_abstention_does_not_duplicate_policy_outcome(
    session, project
):
    [candidate] = _minutes_with(
        session,
        project,
        [_event(ref=None, event_type="response")],
    )

    first = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    second = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert first.abstained_count == 1
    assert second.admitted_count == 0
    assert second.abstained_count == 0
    assert session.scalar(
        select(func.count()).select_from(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == candidate.id
        )
    ) == 1

    original_payload = deepcopy(candidate.payload_json)
    candidate.payload_json = {
        **candidate.payload_json,
        "fields": {
            **candidate.payload_json["fields"],
            "description": "Changed response wording",
        },
    }
    session.flush()
    changed = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )
    candidate.payload_json = original_payload
    session.flush()
    reverted = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert changed.abstained_count == 1
    assert reverted.abstained_count == 0
    assert session.scalar(
        select(func.count()).select_from(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == candidate.id
        )
    ) == 2


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        (_event(ref="PL1"), "conflict_reference_present"),
        (
            {**_event(ref=None), "previous_timing": {"text": "May", "precision": "approximate"}},
            "previous_timing_present",
        ),
        (_event(ref=None, event_type="committed_date_change"), "event_type_outside_policy"),
        (
            _event(ref=None, org="Other Party", stated_party=PIPELINE),
            "affected_party_disagreement",
        ),
        (
            _event(
                ref=None,
                description=(
                    "Tejas Pipeline will proceed after TxDOT confirms the exhibit."
                ),
                committed_date={
                    "text": "after TxDOT confirms the exhibit",
                    "precision": "approximate",
                    "start_date": None,
                    "end_date": None,
                },
            ),
            "non_commitment_language",
        ),
        (
            _event(
                ref=None,
                description=(
                    "Tejas Pipeline will be invited to the utility workshop on May 8."
                ),
                committed_date={
                    "text": "May 8",
                    "precision": "day",
                    "start_date": "2025-05-08",
                    "end_date": "2025-05-08",
                },
            ),
            "non_commitment_language",
        ),
        (
            _event(
                ref=None,
                description=(
                    "Tejas Pipeline scheduled a meeting with TxDOT for May 8."
                ),
                committed_date={
                    "text": "May 8",
                    "precision": "day",
                    "start_date": "2025-05-08",
                    "end_date": "2025-05-08",
                },
            ),
            "non_commitment_language",
        ),
    ],
)
def test_unknown_scope_policy_names_each_failed_predicate(
    session, project, fields, reason
):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.add(ExternalOrg(name="Other Party", aliases=[]))
    session.flush()
    [candidate] = _minutes_with(session, project, [fields])

    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert result.admitted_count == 0
    assert result.abstentions == [
        EventAdmissionAbstention(
            candidate_id=candidate.id,
            reason=reason,
            reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        )
    ]
    assert session.get(Candidate, candidate.id).state == "pending"


def test_model_confidence_cannot_replace_exact_party_evidence(session, project):
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    minutes = _document(
        session, project, filename="confidence-is-not-proof.pdf", doc_type="minutes"
    )
    candidate = _candidate(
        minutes,
        kind="event",
        fields=_event(
            ref=None,
            description="The package is due.",
            committed_date="2025-06-01",
        ),
    )
    candidate.confidence = 1.0
    candidate.payload_json = {
        **candidate.payload_json,
        "model_agreement": True,
        "citations": [
            {
                **candidate.payload_json["citations"][0],
                "quote": "The package is due. 2025-06-01",
            }
        ],
    }
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == candidate.source_document_id,
            DocPage.page_no == 1,
        )
    )
    assert page is not None
    page.text = "Tejas Pipeline project context. The package is due. 2025-06-01"
    _run(session, minutes, [candidate])
    declare_single_run_documents(session, project.id, principal=OPERATOR)
    session.flush()

    result = run_event_admission(
        session, project.id, policy_version=UNKNOWN_SCOPE_POLICY_VERSION
    )

    assert result.admitted_count == 0
    assert result.abstentions[0].reason == "stated_party_not_in_evidence"
    assert session.get(Candidate, candidate.id).state == "pending"


def _database_migration_head(project) -> str:
    session = object_session(project)
    assert session is not None
    migration_head = _current_migration_head(session)
    assert migration_head is not None
    return migration_head


def _activation_receipt(
    project, *, gates, source_revision=None, migration_head=None
):
    source_revision = source_revision or _current_source_revision()
    migration_head = migration_head or _database_migration_head(project)
    policy_sha256 = policy.canonical_sha256(
        canonical_event_admission_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
    )
    admission_count = 1 if gates.get("eligible_case_observed", True) else 0
    opt_in = {
        "metrics": {
            "admission_count": admission_count,
            "false_party_attribution": 0,
            "false_dependency_scope": 0,
            "project_side_masquerade": 0,
            "cross_project_references": 0,
            "unauthorized_work_decisions": 0,
            "duplicates": 0,
            "protected_dependency_delta": 0,
            "protected_report_delta": 0,
            "invalid_evidence_or_receipts": 0,
        },
        "admissions": [
            {
                "candidate_id": index + 1,
                "commitment_lineage_id": index + 1,
                "statement_event_id": index + 1,
            }
            for index in range(admission_count)
        ],
        "work_items": [
            {
                "commitment_lineage_id": index + 1,
                "statement_event_id": index + 1,
                "source_candidate_id": index + 1,
                "dependency_id": None,
                "attention_reasons": [
                    "unknown_scope",
                    "missing_internal_owner",
                    "missing_next_action",
                ],
            }
            for index in range(admission_count)
        ],
    }
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "source_revision": source_revision,
        "migration_head": migration_head,
        "selection_rule": SELECTION_RULE,
        "policy_version": UNKNOWN_SCOPE_POLICY_VERSION,
        "policy_sha256": policy_sha256,
        "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        "opt_in": opt_in,
        "migration_rehearsal": {
            "predecessor": "a257c9e6f204",
            "head": migration_head,
            "status": "passed",
            "fresh_head": migration_head,
            "fresh_status": "passed",
        },
        "authority_statement": (
            "Only the deterministic unknown-scope Commitment class is authorized; "
            "models receive no write authority."
        ),
    }
    receipt["gates"] = _receipt_promotion_gates(receipt)
    return receipt


def _committed_event_admission_race_project(
    session_factory, *, activate: bool, retry: bool
):
    """Commit only UUID-scoped setup required by independent transactions."""

    with session_factory() as setup:
        project = Project(
            slug=f"event-admission-suspension-race-{uuid4().hex}",
            name="Event Admission Suspension Race",
            is_synthetic=True,
            project_side_parties=[PROJECT_SIDE],
        )
        setup.add(project)
        setup.flush([project])
        first = record_acceptance_receipt(
            setup,
            project_id=project.id,
            source_revision=_current_source_revision(),
            migration_head=_database_migration_head(project),
            receipt_json=_activation_receipt(
                project,
                gates={"eligible_case_observed": True},
            ),
        )
        if activate:
            assert activate_passing_acceptance(setup, first.id) is not None
        receipt = first
        if retry:
            receipt = record_acceptance_receipt(
                setup,
                project_id=project.id,
                source_revision=_current_source_revision(),
                migration_head=_database_migration_head(project),
                receipt_json=_activation_receipt(
                    project,
                    gates={"eligible_case_observed": True},
                ),
            )
        ids = (project.id, receipt.id)
        setup.commit()
        return ids


def _delete_committed_event_admission_project(
    session_factory, project_id: int
) -> None:
    """Remove the exact immutable graph committed for a race test."""

    with session_factory() as cleanup:
        cleanup.execute(text("set local session_replication_role = replica"))
        cleanup.execute(
            text(
                "delete from event_admission_activations "
                "where project_id = :project_id"
            ),
            {"project_id": project_id},
        )
        cleanup.execute(
            text(
                "delete from event_admission_acceptance_receipts "
                "where project_id = :project_id"
            ),
            {"project_id": project_id},
        )
        cleanup.execute(
            text("delete from projects where id = :project_id"),
            {"project_id": project_id},
        )
        cleanup.commit()


def test_failed_acceptance_receipt_cannot_activate_normal_processing(
    session, project
):
    failed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={
                "eligible_case_observed": False,
                "zero_false_party_attribution": True,
            },
        ),
    )

    assert failed.status == "failed"
    assert activate_passing_acceptance(session, failed.id) is None
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [candidate] = _minutes_with(session, project, [_event(ref=None)])

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    assert result.abstentions[0].reason == "no_conflict_reference"
    assert session.get(Candidate, candidate.id).state == "pending"


def test_event_admission_status_reports_no_applicable_proof(session, project):
    status = read_event_admission_policy_status(session, project.id)

    assert status.status == "no_applicable_proof"
    assert status.proof_status == "no_applicable_proof"
    assert status.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION
    assert status.latest_receipt_id is None
    assert status.allowed_operations == ("replay",)


def test_event_admission_status_reports_failed_newest_proof(session, project):
    failed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": False},
        ),
    )

    status = read_event_admission_policy_status(session, project.id)

    assert failed.status == "failed"
    assert status.status == "failed_newest_proof"
    assert status.proof_status == "failed_newest_proof"
    assert status.latest_receipt_id == failed.id
    assert status.allowed_operations == ("replay",)


def test_event_admission_status_reports_current_proof_before_activation(
    session, project
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
    )

    status = read_event_admission_policy_status(session, project.id)

    assert status.status == "passed_current_not_active"
    assert status.proof_status == "passed_current"
    assert status.latest_receipt_id == passed.id
    assert status.latest_receipt_current is True
    assert status.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION
    assert status.allowed_operations == ("replay",)


def test_event_admission_status_reports_stale_bound_identities(
    session, project, monkeypatch
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
    )
    monkeypatch.setattr(
        "corridor.event_admission._current_source_revision", lambda: "b" * 40
    )

    status = read_event_admission_policy_status(session, project.id)

    assert status.status == "stale_bound_identities"
    assert status.proof_status == "stale_bound_identities"
    assert status.latest_receipt_id == passed.id
    assert status.latest_receipt_current is False
    assert status.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION
    assert status.allowed_operations == ("replay",)


def test_corrupt_passing_proof_cannot_activate_or_appear_current(session, project):
    receipt_json = {
        **_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
        "status": "passed",
    }
    corrupt = EventAdmissionAcceptanceReceipt(
        project_id=project.id,
        status="passed",
        source_revision=receipt_json["source_revision"],
        migration_head=receipt_json["migration_head"],
        predecessor_policy_version=EVENT_ADMISSION_POLICY_VERSION,
        policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
        policy_sha256=receipt_json["policy_sha256"],
        reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        selection_rule=SELECTION_RULE,
        receipt_json=receipt_json,
        receipt_sha256="f" * 64,
    )
    session.add(corrupt)
    session.flush([corrupt])

    with pytest.raises(ValueError, match="integrity"):
        activate_passing_acceptance(session, corrupt.id)

    status = read_event_admission_policy_status(session, project.id)
    assert status.status == "corrupt_newest_proof"
    assert status.proof_status == "corrupt_newest_proof"
    assert status.latest_receipt_integrity_valid is False
    assert status.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION
    assert status.allowed_operations == ("replay",)


def test_database_rejects_activation_for_a_failed_receipt(session, project):
    failed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": False},
        ),
    )

    with pytest.raises(ProgrammingError), session.begin_nested():
        session.add(
            EventAdmissionActivation(
                project_id=project.id,
                acceptance_receipt_id=failed.id,
                action="activate",
                policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
                reason="forged pass",
                recorded_by="corridor:event-admission-activation",
            )
        )
        session.flush()


def test_passing_receipt_activates_normal_processing_and_suspension_restores_v2(
    session, project
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={
                "eligible_case_observed": True,
                "zero_false_party_attribution": True,
            },
        ),
    )
    activation = activate_passing_acceptance(session, passed.id)
    assert activation is not None
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [admitted_candidate] = _minutes_with(session, project, [_event(ref=None)])

    admitted = run_event_admission(session, project.id)

    assert admitted.admitted_count == 1
    assert session.get(Candidate, admitted_candidate.id).state == "accepted"
    suspension = suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="receipt reproduction paused",
        recorded_by="local:operations",
    )
    assert suspension.action == "suspend"
    [pending_candidate] = _minutes_with(
        session,
        project,
        [_event(ref=None, description="Tejas Pipeline will provide another package.")],
        filename="minutes-after-suspension.pdf",
    )

    predecessor = run_event_admission(session, project.id)

    assert predecessor.admitted_count == 0
    assert session.get(Candidate, pending_candidate.id).state == "pending"


def test_event_admission_status_reports_standing_suspension_separately(
    session, project
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    suspension = suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="receipt reproduction paused",
        recorded_by="local:operations",
    )

    status = read_event_admission_policy_status(session, project.id)

    assert status.status == "suspended"
    assert status.proof_status == "passed_current"
    assert status.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION
    assert status.latest_receipt_id == passed.id
    assert status.latest_action == "suspend"
    assert status.latest_action_id == suspension.id
    assert status.latest_action_receipt_id == passed.id
    assert status.latest_action_policy_version == UNKNOWN_SCOPE_POLICY_VERSION
    assert status.latest_action_recorded_by == "local:operations"
    assert status.allowed_operations == ("lift", "replay")


def test_repeating_identical_activation_finalization_reuses_one_history_entry(
    session, project
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
    )

    first = activate_passing_acceptance(session, passed.id)
    retried = activate_passing_acceptance(session, passed.id)

    assert first is not None
    assert retried is not None
    assert retried.id == first.id
    assert session.scalars(
        select(EventAdmissionActivation).where(
            EventAdmissionActivation.project_id == project.id,
            EventAdmissionActivation.action == "activate",
        )
    ).all() == [first]


def test_activated_normal_processing_does_not_repeat_identical_abstention_outcomes(
    session, project
):
    receipt_json = _activation_receipt(
        project, gates={"eligible_case_observed": True}
    )
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=receipt_json["source_revision"],
        migration_head=receipt_json["migration_head"],
        receipt_json=receipt_json,
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [candidate] = _minutes_with(
        session,
        project,
        [_event(ref=None, event_type="response", committed_date=None)],
    )

    first = run_event_admission(session, project.id)
    after_first = session.scalars(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == candidate.id
        )
    ).all()
    second = run_event_admission(session, project.id)
    after_second = session.scalars(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.candidate_id == candidate.id
        )
    ).all()

    assert first.abstained_count == 1
    assert second.abstained_count == 0
    assert len(after_first) == 2
    assert len(after_second) == 2


def test_automatic_activation_respects_a_standing_human_suspension(
    session, project
):
    original = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
    )
    assert activate_passing_acceptance(session, original.id) is not None
    suspension = suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="receipt reproduction paused",
        recorded_by="local:operations",
    )
    retried = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
    )

    assert activate_passing_acceptance(session, retried.id) is None
    status = read_event_admission_policy_status(session, project.id)

    assert status.status == "suspended"
    assert status.latest_receipt_id == retried.id
    assert status.latest_action == "suspend"
    assert status.latest_action_id == suspension.id


def test_competing_suspension_blocks_later_automatic_activation(
    event_admission_isolated_database,
):
    session_factory = event_admission_isolated_database.session_factory
    project_id, retried_receipt_id = _committed_event_admission_race_project(
        session_factory,
        activate=True,
        retry=True,
    )
    suspension_written = Event()
    allow_commit = Event()

    def suspend_then_commit():
        with session_factory() as competing:
            suspend_unknown_scope_admission(
                competing,
                project_id=project_id,
                reason="receipt reproduction paused",
                recorded_by="local:operations",
            )
            suspension_written.set()
            assert allow_commit.wait(timeout=2)
            competing.commit()

    def activate_after_suspension():
        assert suspension_written.wait(timeout=2)
        with session_factory() as competing:
            activation = activate_passing_acceptance(competing, retried_receipt_id)
            competing.commit()
            return activation

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            suspension_future = pool.submit(suspend_then_commit)
            activation_future = pool.submit(activate_after_suspension)
            assert suspension_written.wait(timeout=2)
            allow_commit.set()
            suspension_future.result()
            assert activation_future.result() is None

        with session_factory() as verification:
            status = read_event_admission_policy_status(verification, project_id)
            assert status.status == "suspended"
            assert status.latest_receipt_id == retried_receipt_id
            assert status.latest_action == "suspend"
    finally:
        _delete_committed_event_admission_project(session_factory, project_id)


def test_competing_activation_then_suspension_leaves_policy_suspended(
    event_admission_isolated_database,
):
    session_factory = event_admission_isolated_database.session_factory
    project_id, receipt_id = _committed_event_admission_race_project(
        session_factory,
        activate=False,
        retry=False,
    )
    activation_written = Event()
    allow_activation_commit = Event()

    def activate_then_commit():
        with session_factory() as competing:
            activation = activate_passing_acceptance(competing, receipt_id)
            assert activation is not None
            activation_written.set()
            assert allow_activation_commit.wait(timeout=2)
            competing.commit()
            return activation.id

    def suspend_after_activation():
        assert activation_written.wait(timeout=2)
        with session_factory() as competing:
            suspension = suspend_unknown_scope_admission(
                competing,
                project_id=project_id,
                reason="receipt reproduction paused",
                recorded_by="local:operations",
            )
            competing.commit()
            return suspension.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            activation_future = pool.submit(activate_then_commit)
            suspension_future = pool.submit(suspend_after_activation)
            assert activation_written.wait(timeout=2)
            allow_activation_commit.set()
            assert activation_future.result() > 0
            assert suspension_future.result() > 0

        with session_factory() as verification:
            status = read_event_admission_policy_status(verification, project_id)
            assert status.status == "suspended"
            assert status.latest_action == "suspend"
            assert (
                status.effective_policy_version
                == EVENT_ADMISSION_POLICY_VERSION
            )
    finally:
        _delete_committed_event_admission_project(session_factory, project_id)


@pytest.mark.slow
def test_replay_cli_keeps_passing_proof_when_suspension_vetoes_activation(
    event_admission_isolated_database, capsys
):
    try:
        compose_postgres = subprocess.run(
            [
                "docker",
                "compose",
                "ps",
                "--status",
                "running",
                "--quiet",
                "postgres",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        pytest.skip("real replay requires the local Docker Compose Postgres service")
    if compose_postgres.returncode or not compose_postgres.stdout.strip():
        pytest.skip("real replay requires the local Docker Compose Postgres service")

    database = event_admission_isolated_database
    session_factory = database.session_factory
    project_slug = f"event-admission-replay-veto-{uuid4().hex}"
    with session_factory() as setup:
        project = Project(
            slug=project_slug,
            name="Event Admission Replay Veto",
            is_synthetic=True,
            project_side_parties=[PROJECT_SIDE],
        )
        setup.add_all(
            (project, ExternalOrg(name=PIPELINE, aliases=["Tejas Pipeline"]))
        )
        setup.flush([project])
        [candidate] = _minutes_with(
            setup,
            project,
            [_unknown_scope_commitment()],
            filename="replay-veto-minutes.pdf",
        )
        historical = record_acceptance_receipt(
            setup,
            project_id=project.id,
            source_revision=_current_source_revision(),
            migration_head=_database_migration_head(project),
            receipt_json=_activation_receipt(
                project,
                gates={"eligible_case_observed": True},
            ),
        )
        assert activate_passing_acceptance(setup, historical.id) is not None
        suspension = suspend_unknown_scope_admission(
            setup,
            project_id=project.id,
            reason="receipt reproduction paused",
            recorded_by="local:operations",
        )
        candidate_id = candidate.id
        project_id = project.id
        setup.commit()

    source_engine = session_factory.kw.get("bind")
    assert source_engine is not None
    source_database_url = source_engine.url.render_as_string(hide_password=False)
    expected_revision = _current_source_revision()
    assert expected_revision is not None

    assert (
        event_admission_acceptance_cli_main(
            [
                "replay",
                "--project-slug",
                project_slug,
                "--source-database-url",
                source_database_url,
                "--postgres-admin-url",
                settings.database_url,
                "--expected-clean-git-revision",
                expected_revision,
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["activated"] is False

    with session_factory() as verification:
        status = read_event_admission_policy_status(verification, project_id)
        assert status.status == "suspended"
        assert status.proof_status == "passed_current"
        assert status.latest_receipt_id == payload["receipt_id"]
        assert status.latest_action_id == suspension.id
        assert verification.get(Candidate, candidate_id).state == "pending"


def test_lifting_suspension_requires_a_human_principal_and_current_passing_proof(
    session, project
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
    )
    assert activate_passing_acceptance(session, passed.id) is not None

    with pytest.raises(InvalidHumanPrincipal):
        suspend_unknown_scope_admission(
            session,
            project_id=project.id,
            reason="receipt reproduction paused",
            recorded_by="corridor:event-admission-activation",
        )
    with pytest.raises(InvalidHumanPrincipal):
        suspend_unknown_scope_admission(
            session,
            project_id=project.id,
            reason="receipt reproduction paused",
            recorded_by=" local:operations ",
        )
    suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="receipt reproduction paused",
        recorded_by="local:operations",
    )

    with pytest.raises(InvalidHumanPrincipal):
        lift_unknown_scope_admission(
            session,
            project_id=project.id,
            recorded_by="corridor:event-admission-activation",
        )
    with pytest.raises(InvalidHumanPrincipal):
        lift_unknown_scope_admission(
            session,
            project_id=project.id,
            recorded_by=" local:human-lift ",
        )

    lifted = lift_unknown_scope_admission(
        session,
        project_id=project.id,
        recorded_by="local:human-lift",
    )
    status = read_event_admission_policy_status(session, project.id)

    assert lifted.action == "activate"
    assert lifted.recorded_by == "local:human-lift"
    assert lifted.acceptance_receipt_id == passed.id
    assert status.status == "active"
    assert status.allowed_operations == ("replay", "suspend")


def test_activated_extension_preserves_predecessor_selected_scope_behavior(
    session, project, admitted
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": True},
        ),
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    [candidate] = _minutes_with(session, project, [_event(ref="PL1")])

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 1
    assert session.get(Candidate, candidate.id).state == "accepted"
    [event] = _events_on(session, admitted.id)
    assert event.scope_mode == "selected"


def test_newer_failed_replay_suspends_older_activation(session, project):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project, gates={"eligible_case_observed": True}
        ),
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    failed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision="b" * 40,
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": False},
            source_revision="b" * 40,
        ),
    )
    assert failed.status == "failed"
    with pytest.raises(ValueError, match="latest"):
        activate_passing_acceptance(session, passed.id)
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [candidate] = _minutes_with(session, project, [_event(ref=None)])

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    assert result.abstentions[0].reason == "no_conflict_reference"
    assert session.get(Candidate, candidate.id).state == "pending"


def test_deployed_rule_digest_drift_suspends_activation(
    session, project, monkeypatch
):
    from corridor import event_admission as module

    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project, gates={"eligible_case_observed": True}
        ),
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    monkeypatch.setattr(
        module,
        "_rule_source_bytes",
        lambda: (("corridor.event_admission.changed", b"changed rules"),),
    )
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [candidate] = _minutes_with(session, project, [_event(ref=None)])

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    assert result.abstentions[0].reason == "no_conflict_reference"
    assert session.get(Candidate, candidate.id).state == "pending"


def test_source_revision_drift_suspends_activation(
    session, project, monkeypatch
):
    from corridor import event_admission as module

    receipt_json = _activation_receipt(
        project, gates={"eligible_case_observed": True}
    )
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=receipt_json["source_revision"],
        migration_head=receipt_json["migration_head"],
        receipt_json=receipt_json,
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    monkeypatch.setattr(module, "_current_source_revision", lambda: "b" * 40)
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [candidate] = _minutes_with(session, project, [_event(ref=None)])

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    assert result.abstentions[0].reason == "no_conflict_reference"
    assert session.get(Candidate, candidate.id).state == "pending"


def test_lift_refuses_stale_or_failed_newest_proof(session, project, monkeypatch):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project, gates={"eligible_case_observed": True}
        ),
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="receipt reproduction paused",
        recorded_by="local:operations",
    )
    monkeypatch.setattr(
        "corridor.event_admission._current_source_revision", lambda: "b" * 40
    )

    with pytest.raises(ValueError, match="current passing proof"):
        lift_unknown_scope_admission(
            session,
            project_id=project.id,
            recorded_by="local:human-lift",
        )

    failed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision="c" * 40,
        migration_head=_database_migration_head(project),
        receipt_json=_activation_receipt(
            project,
            gates={"eligible_case_observed": False},
            source_revision="c" * 40,
        ),
    )

    assert failed.status == "failed"
    with pytest.raises(ValueError, match="current passing proof"):
        lift_unknown_scope_admission(
            session,
            project_id=project.id,
            recorded_by="local:human-lift",
        )


def test_migration_head_drift_suspends_activation(
    session, project, monkeypatch
):
    from corridor import event_admission as module

    receipt_json = _activation_receipt(
        project, gates={"eligible_case_observed": True}
    )
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=receipt_json["source_revision"],
        migration_head=receipt_json["migration_head"],
        receipt_json=receipt_json,
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    monkeypatch.setattr(module, "_current_migration_head", lambda _session: "new-head")
    session.add(ExternalOrg(name=PIPELINE, aliases=[]))
    session.flush()
    [candidate] = _minutes_with(session, project, [_event(ref=None)])

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    assert result.abstentions[0].reason == "no_conflict_reference"
    assert session.get(Candidate, candidate.id).state == "pending"


def test_promotion_gate_rejects_non_residual_work_item_reason():
    opt_in = {
        "metrics": {
            "admission_count": 1,
            "false_party_attribution": 0,
            "false_dependency_scope": 0,
            "project_side_masquerade": 0,
            "cross_project_references": 0,
            "unauthorized_work_decisions": 0,
            "duplicates": 0,
            "protected_dependency_delta": 0,
            "protected_report_delta": 0,
            "invalid_evidence_or_receipts": 0,
        },
        "admissions": [
            {
                "candidate_id": 1,
                "commitment_lineage_id": 1,
                "statement_event_id": 1,
            }
        ],
        "work_items": [
            {
                "commitment_lineage_id": 1,
                "statement_event_id": 1,
                "source_candidate_id": 1,
                "dependency_id": None,
                "attention_reasons": [
                    "unknown_scope",
                    "missing_internal_owner",
                    "missing_next_action",
                    "committed_date_change",
                ],
            }
        ],
    }

    assert _promotion_gates(opt_in)["all_admissions_enter_residual_work"] is False


def test_refused_mechanical_admission_leaves_no_receipt_event_or_audit(
    session, project, admitted
):
    """A database refusal unwinds the policy writer's entire admission act."""
    [candidate] = _minutes_with(
        session,
        project,
        [_event(description="refuse this mechanical statement")],
    )
    session.execute(
        text(
            """
            create function refuse_test_mechanical_statement_write()
            returns trigger
            language plpgsql
            as $$
            begin
                if new.description = 'refuse this mechanical statement' then
                    raise exception 'mechanical statement write refused' using errcode = '23514';
                end if;
                return new;
            end;
            $$;
            """
        )
    )
    session.execute(
        text(
            """
            create trigger refuse_test_mechanical_statement_write
            before insert on dependency_events
            for each row execute function refuse_test_mechanical_statement_write();
            """
        )
    )

    with pytest.raises(IntegrityError, match="mechanical statement write refused"):
        run_event_admission(session, project.id)

    assert session.scalars(
        select(PolicyRun).where(
            PolicyRun.project_id == project.id,
            PolicyRun.family == "event-admission",
        )
    ).all() == []
    assert _events_on(session, admitted.id) == []
    assert session.scalars(
        select(EvidenceLink).where(EvidenceLink.document_id == candidate.source_document_id)
    ).all() == []
    assert session.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == admitted.id,
            AuditLog.action == "admit_event",
        )
    ).all() == []
    session.refresh(candidate)
    session.refresh(admitted)
    assert candidate.state == "pending"
    assert admitted.committed_date is None


@pytest.mark.parametrize(
    "fields,reason,verified",
    [
        (_event(event_type="response"), "event_type_outside_policy", True),
        (_event(event_date=None, committed_date=None), "no_date", True),
        (_event(ref="PL99"), "reference_resolves_to_no_dependency", True),
        (_event(ref=None), "no_conflict_reference", True),
        (_event(org="Some Other Co"), "party_mismatch", True),
        (_event(org=""), "party_unstated", True),
        (_event(), "citations_unverified", False),
    ],
)
def test_each_failed_check_abstains_and_leaves_the_candidate_pending(
    session, project, admitted, fields, reason, verified
):
    [candidate] = _minutes_with(session, project, [fields], verified=verified)

    result = run_event_admission(session, project.id)
    assert result.admitted_count == 0
    assert result.abstained_count == 1
    assert [a.reason for a in result.abstentions] == [reason]

    session.refresh(candidate)
    assert candidate.state == "pending"
    assert session.scalars(
        select(DependencyEvent).where(DependencyEvent.project_id == project.id)
    ).all() == []


def test_two_dependencies_for_one_reference_abstains(
    session, project, admitted
):
    """Exactly one, or the machine does not choose."""
    second = Dependency(
        project_id=project.id,
        ref_code="DEP-X",
        source_ref="PL1",
        title="Second PL1",
        dep_type="utility_relocation",
        external_org_id=admitted.external_org_id,
    )
    session.add(second)
    session.flush()
    _minutes_with(session, project, [_event()])

    result = run_event_admission(session, project.id)
    assert result.abstained_count == 1
    assert result.abstentions[0].reason == "reference_resolves_to_many"


# ── The actor boundary (ADR-0026) ────────────────────────────────────────


def test_candidate_7587_abstains_when_affected_party_does_not_prove_stated_actor(
    session, project, admitted
):
    """An invitation for Air Products is not an Air Products Commitment."""
    air_products = session.scalar(
        select(ExternalOrg).where(ExternalOrg.name == "Air Products")
    )
    if air_products is None:
        air_products = ExternalOrg(name="Air Products")
        session.add(air_products)
        session.flush()
    air_products_dependency = Dependency(
        project_id=project.id,
        ref_code="AIR-PRODUCTS-PL35",
        source_ref="PL35",
        dep_type="utility_relocation",
        title="Air Products PL35 relocation",
        external_org_id=air_products.id,
    )
    session.add(air_products_dependency)
    session.flush()
    candidate_7587 = _event(
        org="Air Products",
        ref="PL35",
        committed_date="2025-05-08",
        description=(
            "Air Products will be invited to the "
            "TxDOT-Utility Owners-DB Proposers Workshop on May 8th, 2025."
        ),
    )
    candidate_7587.pop("stated_party")
    [candidate] = _minutes_with(session, project, [candidate_7587])

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    assert [
        (abstention.candidate_id, abstention.reason)
        for abstention in result.abstentions
    ] == [(candidate.id, "party_unstated")]
    outcome = session.scalar(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == result.run_id,
            EventAdmissionOutcome.candidate_id == candidate.id,
        )
    )
    assert outcome is not None
    assert (outcome.outcome, outcome.reason, outcome.dependency_event_id) == (
        "abstained",
        "party_unstated",
        None,
    )
    session.refresh(candidate)
    session.refresh(air_products_dependency)
    assert candidate.state == "pending"
    assert air_products_dependency.committed_date is None
    assert _events_on(session, air_products_dependency.id) == []


def test_a_project_side_event_never_sets_a_committed_date(
    session, project, admitted
):
    """LJA taking an action item is not the External Party promising."""
    _minutes_with(
        session,
        project,
        [
            _event(
                org=PIPELINE,
                stated_party=PROJECT_SIDE,
                committed_date="2025-03-01",
                description="LJA will send the cross sections",
            )
        ],
    )

    result = run_event_admission(session, project.id)
    assert result.admitted_count == 0
    assert result.abstained_count == 1
    assert result.abstentions[0].reason == "project_side_actor"

    session.refresh(admitted)
    assert admitted.committed_date is None


def test_an_external_commitment_projects_the_committed_date(
    session, project, admitted
):
    """The latest External Party commitment date, projected — the same
    shape as a Work Decision's current values, recomputable from the
    events beneath it."""
    _minutes_with(
        session,
        project,
        [
            _event(event_date="2025-01-16", committed_date="2025-06-01"),
            _event(event_date="2025-02-20", committed_date="2025-09-15"),
        ],
    )

    run_event_admission(session, project.id)
    session.refresh(admitted)
    assert admitted.committed_date == date(2025, 9, 15)


# ── The run receipt ──────────────────────────────────────────────────────


def test_the_run_is_an_immutable_receipt_of_exact_outcomes(
    session, project, admitted
):
    _minutes_with(session, project, [_event(), _event(ref="PL99")])

    result = run_event_admission(session, project.id)
    run = session.get(PolicyRun, result.run_id)
    assert run.policy_approval_id is None  # nothing was signed
    assert run.policy_version == EVENT_ADMISSION_POLICY_VERSION
    assert len(run.policy_sha256) == 64
    assert run.abstention_reason_version == ABSTENTION_REASON_VERSION
    assert (run.applied_count, run.abstained_count) == (1, 1)

    outcomes = session.scalars(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.policy_run_id == run.id
        )
    ).all()
    assert {o.outcome for o in outcomes} == {"admitted", "abstained"}
    admitted_outcome = next(o for o in outcomes if o.outcome == "admitted")
    assert admitted_outcome.dependency_event_id is not None
    assert admitted_outcome.reason is None


def test_an_identical_rerun_records_no_new_outcome(session, project, admitted):
    _minutes_with(session, project, [_event()])

    first = run_event_admission(session, project.id)
    assert first.admitted_count == 1

    second = run_event_admission(session, project.id)
    assert second.admitted_count == 0
    assert second.abstained_count == 0
    events = session.scalars(
        select(DependencyEvent).where(DependencyEvent.project_id == project.id)
    ).all()
    assert events != []
    assert len(events) == 1


def test_the_receipt_records_the_deployed_checks_that_ran(
    session, project, admitted, monkeypatch
):
    """ADR-0022's digest discipline, which this family keeps without the
    authorization ADR-0029 removed: two runs under different checks can
    never claim the same digest, so a replay reads honestly."""
    from corridor import event_admission as module

    _minutes_with(session, project, [_event()])
    before = run_event_admission(session, project.id)
    before_sha = session.get(PolicyRun, before.run_id).policy_sha256

    real = module._rule_source_bytes

    def edited():
        return tuple(
            (name, source + b"\n# a check was edited\n")
            for name, source in real()
        )

    monkeypatch.setattr(module, "_rule_source_bytes", edited)
    after = run_event_admission(session, project.id)
    assert session.get(PolicyRun, after.run_id).policy_sha256 != before_sha


@pytest.mark.parametrize(
    "dependency_name",
    ["corridor.statement_lifecycle", "corridor.verify"],
)
def test_duplicate_guard_dependencies_are_sealed_in_the_rules_digest(
    dependency_name, monkeypatch
):
    from corridor import event_admission as module

    sources = module._rule_source_bytes()
    assert dependency_name in dict(sources)
    before = module._rules_digest()
    monkeypatch.setattr(
        module,
        "_rule_source_bytes",
        lambda: tuple(
            (
                name,
                source + b"\n# duplicate predicate changed\n"
                if name == dependency_name
                else source,
            )
            for name, source in sources
        ),
    )

    assert module._rules_digest() != before


def test_the_receipt_records_the_project_side_parties_that_ran(
    session, project, admitted
):
    """Who counts as the project's own side decides which events may
    carry a commitment, so it is part of what the receipt claims."""
    _minutes_with(session, project, [_event()])
    before = run_event_admission(session, project.id)
    before_sha = session.get(PolicyRun, before.run_id).policy_sha256

    project.project_side_parties = [PROJECT_SIDE, "Another Consultant"]
    session.flush()
    after = run_event_admission(session, project.id)
    assert session.get(PolicyRun, after.run_id).policy_sha256 != before_sha


# ── The two dates stay two dates ─────────────────────────────────────────


def test_a_promised_date_never_stands_in_for_the_date_it_was_said(
    session, project, admitted
):
    """An event with no meeting date still records what was promised —
    but its promised date must not be written as the date it was stated,
    because that is the ordering the Committed Date projection trusts."""
    _minutes_with(
        session,
        project,
        [
            _event(event_date="2025-02-01", committed_date="2025-09-01"),
            _event(event_date=None, committed_date="2025-06-01"),
        ],
    )
    result = run_event_admission(session, project.id)
    assert result.admitted_count == 2

    undated = next(
        event
        for event in session.scalars(select(DependencyEvent))
        if event.new_timing.start_date == date(2025, 6, 1)
    )
    assert undated.event_date is None

    # The February statement is the only one that carries a date it was
    # said on, so it holds the Committed Date — an undated statement
    # cannot claim to be the most recent.
    session.refresh(admitted)
    assert admitted.committed_date == date(2025, 9, 1)


def test_an_unparseable_date_abstains_rather_than_guessing(
    session, project, admitted
):
    [candidate] = _minutes_with(
        session, project, [_event(event_date="sometime in spring")]
    )
    result = run_event_admission(session, project.id)
    assert [a.reason for a in result.abstentions] == ["unparseable_date"]
    session.refresh(candidate)
    assert candidate.state == "pending"


# ── Per-party references (ADR-0030) ──────────────────────────────────────


def _second_party_dependency(session, project, *, ref, org_name, alias=None):
    """Another party's Dependency carrying the same stated number."""
    from corridor.models import ExternalOrg

    org = ExternalOrg(name=org_name, aliases=[alias] if alias else [])
    session.add(org)
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code=f"DEP-{org_name[:3].upper()}-{ref}",
        source_ref=ref,
        dep_type="utility_relocation",
        title=f"{org_name} conflict {ref}",
        external_org_id=org.id,
    )
    session.add(dependency)
    session.flush()
    return dependency


def test_a_shared_number_resolves_by_the_statements_party(
    session, project, admitted
):
    """Under a per-party scheme one number sits on several parties'
    lists; the stated party is the other half of the name (ADR-0030)."""
    other = _second_party_dependency(
        session, project, ref="PL1", org_name="Synthetic Cable Co"
    )
    _minutes_with(session, project, [_event(org="Synthetic Cable Co")])

    result = run_event_admission(session, project.id)
    assert result.admitted_count == 1
    [event] = _events_on(session, other.id)
    assert event.event_type == "commitment"
    assert (
        _events_on(session, admitted.id)
        == []
    )


def test_narrowing_by_party_honors_the_recorded_aliases(
    session, project, admitted
):
    other = _second_party_dependency(
        session,
        project,
        ref="PL1",
        org_name="Synthetic Cable Communications LLC",
        alias="SynCable",
    )
    _minutes_with(session, project, [_event(org="SynCable")])

    result = run_event_admission(session, project.id)
    assert result.admitted_count == 1
    assert (
        len(
                _events_on(session, other.id)
        )
        == 1
    )


def test_a_number_no_party_disambiguates_still_abstains(
    session, project, admitted
):
    """Two parties hold the number and the statement matches neither:
    ambiguity stays ambiguous rather than being guessed."""
    _second_party_dependency(session, project, ref="PL1", org_name="Synthetic Cable Co")
    _minutes_with(session, project, [_event(org="Some Other Co")])

    result = run_event_admission(session, project.id)
    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {
        "reference_resolves_to_many"
    }


# ── The pile of statements the machine could not place (#213) ────────────


def test_an_unplaceable_statement_is_kept_and_named(session, project, admitted):
    """A dated promise is exactly what this product exists to catch;
    losing one silently is worse than a short list."""
    from corridor.event_admission import waiting_statements

    [candidate] = _minutes_with(session, project, [_event(ref="PL99")])
    result = run_event_admission(session, project.id)
    assert result.admitted_count == 0

    [waiting] = waiting_statements(session, project.id)
    assert waiting["candidate"].id == candidate.id
    assert waiting["reason"] == "reference_resolves_to_no_dependency"
    assert waiting["external_org"] == PIPELINE
    assert waiting["event_date"] == "2025-01-16"


def test_attaching_places_the_statement_a_human_names(
    session, project, admitted
):
    from corridor.event_admission import attach_statement, waiting_statements

    [candidate] = _minutes_with(session, project, [_event(ref="PL99")])
    run_event_admission(session, project.id)

    event = attach_statement(
        session, candidate, admitted, principal=OPERATOR
    )

    assert event.event_type == "commitment"
    assert event.created_by == OPERATOR.subject
    session.refresh(candidate)
    assert candidate.state == "accepted"
    assert waiting_statements(session, project.id) == []


def test_attaching_uses_the_candidate_payload_re_read_under_the_project_lock(
    session, project, admitted
):
    """A reviewed edit committed after the initial read is the attached fact."""
    from corridor.event_admission import attach_statement

    [candidate] = _minutes_with(
        session, project, [_event(ref="PL99", committed_date="2025-06-01")]
    )
    run_event_admission(session, project.id)
    edited_payload = json.loads(json.dumps(candidate.payload_json))
    edited_payload["fields"]["committed_date"] = "2025-07-15"
    session.execute(
        text(
            "update candidates set payload_json = cast(:payload as jsonb) "
            "where id = :candidate_id"
        ),
        {"payload": json.dumps(edited_payload), "candidate_id": candidate.id},
    )
    assert candidate.payload_json["fields"]["committed_date"] == "2025-06-01"

    event = attach_statement(session, candidate, admitted, principal=OPERATOR)

    assert event.new_timing.start_date == date(2025, 7, 15)


def test_the_pile_disables_attach_when_cited_evidence_is_outside_the_project(
    session, project, admitted
):
    from corridor.event_admission import waiting_statements

    [candidate] = _minutes_with(session, project, [_event(ref="PL99")])
    run_event_admission(session, project.id)
    payload = json.loads(json.dumps(candidate.payload_json))
    payload["citations"][0]["document_id"] = 9_999_999
    candidate.payload_json = payload
    session.flush()

    [waiting] = waiting_statements(session, project.id)

    assert waiting["attachable"] is False


def test_refused_statement_placement_leaves_the_candidate_and_event_unchanged(
    session, project, admitted
):
    """An audit refusal must unwind the event a human placement just created."""
    from corridor.event_admission import attach_statement

    [candidate] = _minutes_with(session, project, [_event(ref="PL99")])
    run_event_admission(session, project.id)
    session.execute(
        text(
            """
            create function refuse_test_statement_attachment_audit()
            returns trigger
            language plpgsql
            as $$
            begin
                if new.action = 'attach_statement' then
                    raise exception 'statement attachment audit refused' using errcode = '23514';
                end if;
                return new;
            end;
            $$;
            """
        )
    )
    session.execute(
        text(
            """
            create trigger refuse_test_statement_attachment_audit
            before insert on audit_log
            for each row execute function refuse_test_statement_attachment_audit();
            """
        )
    )

    with pytest.raises(IntegrityError, match="statement attachment audit refused"):
        attach_statement(session, candidate, admitted, principal=OPERATOR)

    assert _events_on(session, admitted.id) == []
    assert session.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == admitted.id,
            AuditLog.action == "attach_statement",
        )
    ).all() == []
    session.refresh(candidate)
    session.refresh(admitted)
    assert candidate.state == "pending"
    assert admitted.committed_date is None


def test_attaching_holds_the_masquerade_boundary(session, project, admitted):
    """A project-side actor stating a delivery date is an action item,
    never an External Party's commitment — a human naming a record does
    not change that (ADR-0026)."""
    from corridor.event_admission import StatementUnplaceable, attach_statement

    [candidate] = _minutes_with(
        session,
        project,
        [_event(org=PIPELINE, stated_party=PROJECT_SIDE, ref="PL99")],
    )
    run_event_admission(session, project.id)

    with pytest.raises(StatementUnplaceable, match="own side"):
        attach_statement(session, candidate, admitted, principal=OPERATOR)
    session.refresh(candidate)
    assert candidate.state == "pending"


def test_attaching_refuses_an_unreadable_date(session, project, admitted):
    from corridor.event_admission import StatementUnplaceable, attach_statement

    [candidate] = _minutes_with(
        session, project, [_event(event_date="the third of never", ref="PL99")]
    )
    run_event_admission(session, project.id)

    with pytest.raises(StatementUnplaceable, match="date"):
        attach_statement(session, candidate, admitted, principal=OPERATOR)


def test_attaching_is_an_attributable_human_act(session, project, admitted):
    from corridor.event_admission import attach_statement

    [candidate] = _minutes_with(session, project, [_event(ref="PL99")])
    run_event_admission(session, project.id)

    with pytest.raises(InvalidHumanPrincipal):
        attach_statement(
            session, candidate, admitted, principal="system:batch"
        )


def test_an_attached_commitment_moves_the_committed_date(
    session, project, admitted
):
    """The projection is the same one the policy path feeds."""
    from corridor.event_admission import attach_statement

    [candidate] = _minutes_with(
        session,
        project,
        [_event(ref="PL99", committed_date="2025-06-03")],
    )
    run_event_admission(session, project.id)
    attach_statement(session, candidate, admitted, principal=OPERATOR)

    session.refresh(admitted)
    assert admitted.committed_date == date(2025, 6, 3)


def test_a_statement_cannot_attach_to_another_projects_record(
    session, project, admitted
):
    from corridor.event_admission import StatementUnplaceable, attach_statement
    from corridor.models import Project as ProjectModel

    other = ProjectModel(
        slug="statement-other", name="Other", is_synthetic=True
    )
    session.add(other)
    session.flush()
    stray = Dependency(
        project_id=other.id,
        ref_code="DEP-00001",
        source_ref="PL99",
        dep_type="utility_relocation",
        title="elsewhere",
    )
    session.add(stray)
    session.flush()

    [candidate] = _minutes_with(session, project, [_event(ref="PL99")])
    run_event_admission(session, project.id)

    with pytest.raises(StatementUnplaceable, match="another project"):
        attach_statement(session, candidate, stray, principal=OPERATOR)


def test_attaching_refuses_a_quote_never_found_on_its_page(
    session, project, admitted
):
    """The policy's first refusal. Without it a Committed Date could be
    published from a citation the system had already disproved."""
    from corridor.event_admission import StatementUnplaceable, attach_statement

    [candidate] = _minutes_with(
        session, project, [_event(ref="PL99")], verified=False
    )
    run_event_admission(session, project.id)

    with pytest.raises(StatementUnplaceable, match="not found on its page"):
        attach_statement(session, candidate, admitted, principal=OPERATOR)
    session.refresh(admitted)
    assert admitted.committed_date is None


def test_attaching_refuses_a_candidate_outside_its_declared_run(
    session, project, admitted
):
    """The scope every other human write goes through; attaching by hand
    was the one path that skipped it."""
    from corridor.event_admission import StatementUnplaceable, attach_statement

    minutes = _document(
        session, project, filename="stray-minutes.pdf", doc_type="minutes"
    )
    stray = _candidate(minutes, kind="event", fields=_event(ref="PL99"))
    session.add(stray)
    session.flush()  # never attached to an ExtractionRun

    with pytest.raises(StatementUnplaceable):
        attach_statement(session, stray, admitted, principal=OPERATOR)


def test_attaching_refuses_a_dismissed_record(session, project, admitted):
    from corridor.adjudicate import dismiss_dependency
    from corridor.event_admission import StatementUnplaceable, attach_statement

    [candidate] = _minutes_with(session, project, [_event(ref="PL99")])
    run_event_admission(session, project.id)
    dismiss_dependency(session, admitted, "duplicate", principal=OPERATOR)

    with pytest.raises(StatementUnplaceable, match="dismissed"):
        attach_statement(session, candidate, admitted, principal=OPERATOR)


def test_the_pile_offers_only_what_can_actually_be_placed(
    session, project, admitted
):
    """Offering an attach the mutation would refuse is the failure the
    lane rule already names."""
    from corridor.event_admission import waiting_statements

    minutes = _document(
        session, project, filename="undeclared.pdf", doc_type="minutes"
    )
    stray = _candidate(minutes, kind="event", fields=_event(ref="PL99"))
    session.add(stray)
    session.flush()

    assert stray.id not in {
        w["candidate"].id for w in waiting_statements(session, project.id)
    }


def test_the_machine_never_attaches_to_a_dismissed_record(
    session, project, admitted
):
    """The human attach refuses a dismissed record; the machine must not
    be the looser door. An event written there would be filed where the
    list, the engine, and the pile never look again (ADR-0032)."""
    from corridor.adjudicate import dismiss_dependency

    dismiss_dependency(session, admitted, "duplicate", principal=OPERATOR)
    [candidate] = _minutes_with(session, project, [_event()])

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    assert {a.reason for a in result.abstentions} == {
        "reference_resolves_to_no_dependency"
    }
    assert (
            _events_on(session, admitted.id)
        == []
    )
    session.refresh(admitted)
    assert admitted.committed_date is None


def test_the_machine_reads_only_declared_current_candidates(
    session, project, admitted
):
    """The same scope every other door enforces: an event candidate with
    no declared run lineage is invisible to the machine writer too."""
    minutes = _document(
        session, project, filename="undeclared-m.pdf", doc_type="minutes"
    )
    stray = _candidate(minutes, kind="event", fields=_event())
    session.add(stray)
    session.flush()  # never attached to an ExtractionRun, never declared

    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    assert result.abstained_count == 0  # not even evaluated
    session.refresh(stray)
    assert stray.state == "pending"


def test_the_pile_marks_what_attach_would_refuse(session, project, admitted):
    from corridor.event_admission import waiting_statements

    _minutes_with(
        session,
        project,
        [_event(ref="PL99"), _event(ref="PL98", event_type="response")],
    )
    run_event_admission(session, project.id)

    by_ref = {
        w["conflict_ref"]: w for w in waiting_statements(session, project.id)
    }
    assert by_ref["PL99"]["attachable"] is True
    assert by_ref["PL98"]["attachable"] is False  # type outside the policy


def test_the_pile_disables_attach_when_the_statement_date_is_unreadable(
    session, project, admitted
):
    """Preview and mutation share the statement's target-independent rules."""
    from corridor.event_admission import waiting_statements

    _minutes_with(
        session,
        project,
        [_event(ref="PL99", event_date="the third of never")],
    )
    run_event_admission(session, project.id)

    [waiting] = waiting_statements(session, project.id)
    assert waiting["attachable"] is False


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        (
            {**_event(ref="PL99"), "description": ""},
            "preserve what the party said",
        ),
        (
            {
                **_event(ref="PL99"),
                "committed_date": {
                    "text": "January 2025",
                    "precision": "month",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-30",
                },
            },
            "invalid calendar bounds",
        ),
    ],
)
def test_the_pile_and_mutation_refuse_the_same_invalid_statement_draft(
    session, project, admitted, fields, reason
):
    """A preview must not offer a writer-rejected statement as attachable."""
    from corridor.event_admission import (
        StatementUnplaceable,
        attach_statement,
        waiting_statements,
    )

    [candidate] = _minutes_with(session, project, [fields])
    result = run_event_admission(session, project.id)

    assert result.admitted_count == 0
    [waiting] = waiting_statements(session, project.id)
    assert waiting["attachable"] is False
    with pytest.raises(StatementUnplaceable, match=reason):
        attach_statement(session, candidate, admitted, principal=OPERATOR)
    assert _events_on(session, admitted.id) == []
    session.refresh(candidate)
    assert candidate.state == "pending"


def test_the_pile_disables_attach_when_the_speaker_is_not_registered(
    session, project, admitted
):
    from corridor.event_admission import waiting_statements

    _minutes_with(
        session,
        project,
        [_event(ref="PL99", stated_party="Unregistered Speaker")],
    )
    run_event_admission(session, project.id)

    [waiting] = waiting_statements(session, project.id)
    assert waiting["attachable"] is False


def test_the_pile_disables_attach_when_the_timing_shape_contradicts_the_type(
    session, project, admitted
):
    from corridor.event_admission import waiting_statements

    commitment = _event(ref="PL99")
    commitment["previous_timing"] = {
        "text": "May 2025",
        "precision": "month",
        "start_date": "2025-05-01",
        "end_date": "2025-05-31",
    }
    change = _event(ref="PL98", event_type="committed_date_change")
    _minutes_with(session, project, [commitment, change])
    run_event_admission(session, project.id)

    by_ref = {
        waiting["conflict_ref"]: waiting
        for waiting in waiting_statements(session, project.id)
    }
    assert by_ref["PL99"]["attachable"] is False
    assert by_ref["PL98"]["attachable"] is False
