"""The joined policy tables hold every family to the same rules.

ADR-0028: one approvals table and one runs table name their family; each
family keeps its own outcome table. The join had to strengthen
enforcement, not weaken it — a cross-family pointer is a constraint
violation, the counts-reconciliation check Carry-Forward alone carried
now holds for every family, and the shared tables are immutable in the
schema.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.models import (
    DependencyAdmissionOutcome,
    EventAdmissionOutcome,
    PolicyApproval,
    PolicyRun,
    Project,
)


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
        slug="policy-tables-test", name="Policy Tables Test", is_synthetic=True
    )
    session.add(p)
    session.flush()
    return p


def _approval(session, project, family):
    approval = PolicyApproval(
        project_id=project.id,
        family=family,
        policy_version=f"{family}-v1",
        approved_by="local:policy-tables-tester",
        policy_json={"policy_version": f"{family}-v1"},
        policy_sha256="0" * 64,
    )
    session.add(approval)
    session.flush([approval])
    return approval


def _run(session, project, approval, *, applied=0, abstained=0):
    run = PolicyRun(
        project_id=project.id,
        family=approval.family,
        policy_approval_id=approval.id,
        policy_version=approval.policy_version,
        policy_sha256=approval.policy_sha256,
        abstention_reason_version=f"{approval.family}-abstentions-v1",
        applied_count=applied,
        abstained_count=abstained,
    )
    session.add(run)
    session.flush([run])
    return run


def test_a_run_cannot_name_another_family_s_approval(session, project):
    event_approval = _approval(session, project, "event-admission")
    with pytest.raises(IntegrityError):
        run = PolicyRun(
            project_id=project.id,
            family="dependency-admission",
            policy_approval_id=event_approval.id,
            policy_version=event_approval.policy_version,
            policy_sha256=event_approval.policy_sha256,
            abstention_reason_version="dependency-admission-abstentions-v1",
            applied_count=0,
            abstained_count=0,
        )
        session.add(run)
        session.flush([run])


def test_an_outcome_cannot_point_at_another_family_s_run(session, project):
    approval = _approval(session, project, "dependency-admission")
    run = _run(session, project, approval, abstained=1)

    with pytest.raises(IntegrityError):
        outcome = EventAdmissionOutcome(
            policy_run_id=run.id,
            family="event-admission",
            candidate_id=1,
            outcome="abstained",
            reason="citations_unverified",
        )
        session.add(outcome)
        session.flush([outcome])


def test_an_outcome_cannot_lie_about_its_own_family(session, project):
    approval = _approval(session, project, "dependency-admission")
    run = _run(session, project, approval, abstained=1)

    with pytest.raises(IntegrityError):
        outcome = EventAdmissionOutcome(
            policy_run_id=run.id,
            family="dependency-admission",  # the CHECK pins the literal
            candidate_id=1,
            outcome="abstained",
            reason="citations_unverified",
        )
        session.add(outcome)
        session.flush([outcome])


def test_every_family_must_reconcile_its_counts_now(session, project):
    """The check Carry-Forward alone used to carry (ADR-0028): a run whose
    stored counts disagree with its actual outcome rows refuses."""
    approval = _approval(session, project, "dependency-admission")
    _run(session, project, approval, applied=3, abstained=0)

    with pytest.raises(IntegrityError) as excinfo:
        session.execute(
            text("set constraints policy_runs_must_match_outcomes immediate")
        )
        session.flush()
    assert "reconcile" in str(excinfo.value)


def test_a_reconciled_admission_run_passes_the_deferred_check(
    session, project
):
    from corridor.models import Candidate, Document

    import hashlib

    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"pt-doc").hexdigest(),
        filename="pt.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "X1"}},
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.5,
        citations_verified=False,
    )
    session.add(candidate)
    session.flush()

    approval = _approval(session, project, "dependency-admission")
    run = _run(session, project, approval, abstained=1)
    session.add(
        DependencyAdmissionOutcome(
            policy_run_id=run.id,
            candidate_id=candidate.id,
            outcome="abstained",
            reason="citations_unverified",
        )
    )
    session.flush()
    session.execute(
        text("set constraints policy_runs_must_match_outcomes immediate")
    )
    session.flush()  # reconciled: no refusal


def test_the_shared_tables_are_immutable(session, project):
    approval = _approval(session, project, "event-admission")
    run = _run(session, project, approval)

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                update(PolicyApproval)
                .where(PolicyApproval.id == approval.id)
                .values(approved_by="local:someone-else")
            )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                update(PolicyRun)
                .where(PolicyRun.id == run.id)
                .values(applied_count=99)
            )
