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

import hashlib
import json
from copy import deepcopy
from datetime import date

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from corridor.adjudicate import accept_candidate
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
)
from corridor.event_admission_acceptance import (
    RECEIPT_VERSION,
    SELECTION_RULE,
    _promotion_gates,
    _receipt_promotion_gates,
    activate_passing_acceptance,
    record_acceptance_receipt,
    suspend_unknown_scope_admission,
)
from corridor import policy
from corridor.extraction_runs import (
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
    EventAdmissionOutcome,
    EventAdmissionActivation,
    ExternalOrg,
    CandidateDisposition,
    DependencyEventScopeDecision,
    PolicyRun,
    Project,
)
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
    _run(session, minutes, candidates)
    declare_single_run_documents(session, project.id, principal=OPERATOR)
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
        select(DependencyEvent).where(DependencyEvent.created_by == "corridor:event-admission")
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
    assert session.scalar(select(func.count()).select_from(DependencyEvent)) == 1
    assert session.scalar(select(func.count()).select_from(CandidateDisposition)) == 1
    assert session.scalar(
        select(func.count()).select_from(EventAdmissionOutcome).where(
            EventAdmissionOutcome.outcome == "admitted"
        )
    ) == 1


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


def _activation_receipt(
    project, *, gates, source_revision=None, migration_head="a316c5d7e9f1"
):
    source_revision = source_revision or _current_source_revision()
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


def test_failed_acceptance_receipt_cannot_activate_normal_processing(
    session, project
):
    failed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head="a316c5d7e9f1",
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


def test_database_rejects_activation_for_a_failed_receipt(session, project):
    failed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head="a316c5d7e9f1",
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
        migration_head="a316c5d7e9f1",
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


def test_activated_extension_preserves_predecessor_selected_scope_behavior(
    session, project, admitted
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head="a316c5d7e9f1",
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
        migration_head="a316c5d7e9f1",
        receipt_json=_activation_receipt(
            project, gates={"eligible_case_observed": True}
        ),
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    failed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision="b" * 40,
        migration_head="a316c5d7e9f1",
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
        migration_head="a316c5d7e9f1",
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
    assert session.scalars(select(DependencyEvent)).all() == []


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
    assert session.scalars(select(DependencyEvent)).all() != []
    assert len(session.scalars(select(DependencyEvent)).all()) == 1


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
