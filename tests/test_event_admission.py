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
from datetime import date

import pytest
from sqlalchemy import select

from corridor.adjudicate import accept_candidate
from corridor.db import Session, engine
from corridor.event_admission import (
    ABSTENTION_REASON_VERSION,
    EVENT_ADMISSION_POLICY_VERSION,
    EventAdmissionNotAuthorized,
    authorize_event_admission,
    run_event_admission,
)
from corridor.extraction_runs import (
    declare_single_run_documents,
    record_extraction_run,
)
from corridor.models import (
    Candidate,
    Dependency,
    DependencyEvent,
    DocPage,
    Document,
    EventAdmissionOutcome,
    EventAdmissionRun,
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
    committed_date=None,
    description=None,
):
    return {
        "event_type": event_type,
        "description": description or f"{org} spoke about {ref}",
        "external_org": org,
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


def _minutes_with(session, project, event_fields_list, *, verified=True):
    minutes = _document(
        session, project, filename="minutes.pdf", doc_type="minutes"
    )
    candidates = [
        _candidate(minutes, kind="event", fields=f, verified=verified)
        for f in event_fields_list
    ]
    _run(session, minutes, candidates)
    declare_single_run_documents(session, project.id, principal=OPERATOR)
    return candidates


# ── Authorization ────────────────────────────────────────────────────────


def test_a_run_without_authorization_refuses(session, project, admitted):
    _minutes_with(session, project, [_event()])
    with pytest.raises(EventAdmissionNotAuthorized):
        run_event_admission(session, project.id)


def test_authorization_is_an_attributable_human_act(session, project):
    approval = authorize_event_admission(session, project.id, principal=OPERATOR)
    assert approval.policy_version == EVENT_ADMISSION_POLICY_VERSION
    assert approval.approved_by == OPERATOR.subject
    assert len(approval.policy_sha256) == 64

    with pytest.raises(InvalidHumanPrincipal):
        authorize_event_admission(
            session, project.id, principal="system:batch"
        )


# ── The checks ───────────────────────────────────────────────────────────


def test_a_clean_event_is_admitted_onto_its_dependency(
    session, project, admitted
):
    [candidate] = _minutes_with(session, project, [_event()])
    authorize_event_admission(session, project.id, principal=OPERATOR)

    result = run_event_admission(session, project.id)
    assert result.admitted_count == 1
    assert result.abstained_count == 0

    event = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.dependency_id == admitted.id
        )
    ).one()
    assert event.event_type == "commitment"
    assert event.event_date == date(2025, 1, 16)
    session.refresh(candidate)
    assert candidate.state == "accepted"


@pytest.mark.parametrize(
    "fields,reason,verified",
    [
        (_event(event_type="response"), "event_type_outside_policy", True),
        (_event(event_date=None), "no_date", True),
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
    authorize_event_admission(session, project.id, principal=OPERATOR)

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
        status="identified",
    )
    session.add(second)
    session.flush()
    _minutes_with(session, project, [_event()])
    authorize_event_admission(session, project.id, principal=OPERATOR)

    result = run_event_admission(session, project.id)
    assert result.abstained_count == 1
    assert result.abstentions[0].reason == "reference_resolves_to_many"


# ── The actor boundary (ADR-0026) ────────────────────────────────────────


def test_a_project_side_event_never_sets_a_committed_date(
    session, project, admitted
):
    """LJA taking an action item is not the External Party promising."""
    _minutes_with(
        session,
        project,
        [
            _event(
                org=PROJECT_SIDE,
                committed_date="2025-03-01",
                description="LJA will send the cross sections",
            )
        ],
    )
    authorize_event_admission(session, project.id, principal=OPERATOR)

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
    authorize_event_admission(session, project.id, principal=OPERATOR)

    run_event_admission(session, project.id)
    session.refresh(admitted)
    assert admitted.committed_date == date(2025, 9, 15)


# ── The run receipt ──────────────────────────────────────────────────────


def test_the_run_is_an_immutable_receipt_of_exact_outcomes(
    session, project, admitted
):
    _minutes_with(session, project, [_event(), _event(ref="PL99")])
    approval = authorize_event_admission(
        session, project.id, principal=OPERATOR
    )

    result = run_event_admission(session, project.id)
    run = session.get(EventAdmissionRun, result.run_id)
    assert run.policy_approval_id == approval.id
    assert run.policy_sha256 == approval.policy_sha256
    assert run.abstention_reason_version == ABSTENTION_REASON_VERSION
    assert (run.admitted_count, run.abstained_count) == (1, 1)

    outcomes = session.scalars(
        select(EventAdmissionOutcome).where(
            EventAdmissionOutcome.event_admission_run_id == run.id
        )
    ).all()
    assert {o.outcome for o in outcomes} == {"admitted", "abstained"}
    admitted_outcome = next(o for o in outcomes if o.outcome == "admitted")
    assert admitted_outcome.dependency_event_id is not None
    assert admitted_outcome.reason is None


def test_an_identical_rerun_records_no_new_outcome(session, project, admitted):
    _minutes_with(session, project, [_event()])
    authorize_event_admission(session, project.id, principal=OPERATOR)

    first = run_event_admission(session, project.id)
    assert first.admitted_count == 1

    second = run_event_admission(session, project.id)
    assert second.admitted_count == 0
    assert second.abstained_count == 0
    assert session.scalars(select(DependencyEvent)).all() != []
    assert len(session.scalars(select(DependencyEvent)).all()) == 1


def test_a_stale_policy_version_refuses_until_reauthorized(
    session, project, admitted, monkeypatch
):
    _minutes_with(session, project, [_event()])
    authorize_event_admission(session, project.id, principal=OPERATOR)

    from corridor import event_admission as module

    monkeypatch.setattr(
        module, "EVENT_ADMISSION_POLICY_VERSION", "event-admission-v2"
    )
    with pytest.raises(EventAdmissionNotAuthorized):
        run_event_admission(session, project.id)


# ── The digest covers the rules themselves ───────────────────────────────


def test_editing_a_check_pauses_the_policy_until_reauthorized(
    session, project, admitted, tmp_path, monkeypatch
):
    """ADR-0022's guarantee, which this family inherits: the digest
    covers the deployed bytes of the code that decides, not merely its
    configuration. Changing a check without changing its version must
    pause the policy, not run unreviewed."""
    from corridor import event_admission as module

    _minutes_with(session, project, [_event()])
    authorize_event_admission(session, project.id, principal=OPERATOR)
    assert module.current_event_admission_approval(session, project.id)

    real = module._rule_source_bytes

    def edited():
        return tuple(
            (name, source + b"\n# a check was edited\n")
            for name, source in real()
        )

    monkeypatch.setattr(module, "_rule_source_bytes", edited)
    assert module.current_event_admission_approval(session, project.id) is None
    with pytest.raises(EventAdmissionNotAuthorized):
        run_event_admission(session, project.id)


def test_changing_the_project_side_parties_pauses_the_policy(
    session, project, admitted
):
    """Who counts as the project's own side decides which events may
    carry a commitment, so it is part of what was authorized."""
    from corridor import event_admission as module

    authorize_event_admission(session, project.id, principal=OPERATOR)
    assert module.current_event_admission_approval(session, project.id)

    project.project_side_parties = [PROJECT_SIDE, "Another Consultant"]
    session.flush()
    assert module.current_event_admission_approval(session, project.id) is None


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
    authorize_event_admission(session, project.id, principal=OPERATOR)
    result = run_event_admission(session, project.id)
    assert result.admitted_count == 2

    undated = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.committed_date == date(2025, 6, 1)
        )
    ).one()
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
    authorize_event_admission(session, project.id, principal=OPERATOR)
    result = run_event_admission(session, project.id)
    assert [a.reason for a in result.abstentions] == ["unparseable_date"]
    session.refresh(candidate)
    assert candidate.state == "pending"
