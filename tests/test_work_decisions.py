"""The Work Decision service seam: assign an Internal Owner (ADR-0025).

A Work Decision proves only what the project decided and when. These tests
drive the one seam every later Work Decision surface consumes: principal +
Dependency + state change in, typed immutable receipt out, current value
projected, audit holding a pointer and never the payload.
"""

from datetime import date

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from corridor.audit import ASSIGN_INTERNAL_OWNER, DEPENDENCY
from corridor.db import Session, engine
from corridor.models import AuditLog, Dependency, Project, WorkDecision
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.work_decisions import (
    assign_internal_owner,
    current_internal_owner_decision,
)

RECORDER = HumanPrincipal("local:coordination-recorder")


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
        slug="work-decisions-test", name="Work Decisions Test", is_synthetic=True
    )
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def dependency(session, project):
    d = Dependency(
        project_id=project.id,
        ref_code="WD-1",
        dep_type="utility_relocation",
        title="Water main at 1102+20",
        status="identified",
    )
    session.add(d)
    session.flush()
    return d


def _decisions(session, dependency_id):
    return session.scalars(
        select(WorkDecision)
        .where(WorkDecision.dependency_id == dependency_id)
        .order_by(WorkDecision.id)
    ).all()


def test_assignment_writes_a_typed_receipt_and_projects_the_current_value(
    session, dependency
):
    decision = assign_internal_owner(
        session, dependency.id, "Dana Fields", principal=RECORDER
    )

    assert decision.decision_type == "assign_internal_owner"
    assert decision.field == "internal_owner"
    assert decision.before_value is None
    assert decision.after_value == "Dana Fields"
    assert decision.recorded_by == RECORDER.subject
    assert decision.recorded_at is not None
    assert decision.predecessor_decision_id is None
    session.refresh(dependency)
    assert dependency.internal_owner == "Dana Fields"


def test_reassignment_appends_and_pins_its_predecessor(session, dependency):
    first = assign_internal_owner(
        session, dependency.id, "Dana Fields", principal=RECORDER
    )
    second = assign_internal_owner(
        session, dependency.id, "Lee Marsh", principal=RECORDER
    )

    assert second.predecessor_decision_id == first.id
    assert second.before_value == "Dana Fields"
    assert second.after_value == "Lee Marsh"
    session.refresh(dependency)
    assert dependency.internal_owner == "Lee Marsh"
    assert [d.id for d in _decisions(session, dependency.id)] == [
        first.id,
        second.id,
    ]
    current = current_internal_owner_decision(session, dependency.id)
    assert current is not None and current.id == second.id


def test_self_assignment_records_both_facts(session, dependency):
    decision = assign_internal_owner(
        session,
        dependency.id,
        RECORDER.subject,
        principal=RECORDER,
    )
    assert decision.recorded_by == RECORDER.subject
    assert decision.after_value == RECORDER.subject


def test_assigning_the_current_owner_again_records_nothing_new(
    session, dependency
):
    assign_internal_owner(session, dependency.id, "Dana Fields", principal=RECORDER)
    assign_internal_owner(session, dependency.id, "Dana Fields", principal=RECORDER)

    assert len(_decisions(session, dependency.id)) == 1


def test_a_work_decision_requires_an_attributable_human(session, dependency):
    with pytest.raises(InvalidHumanPrincipal):
        assign_internal_owner(
            session, dependency.id, "Dana Fields", principal="local:free-text"
        )
    assert _decisions(session, dependency.id) == []


def test_a_blank_owner_is_a_refusal_not_a_clearing(session, dependency):
    with pytest.raises(ValueError, match="Internal Owner"):
        assign_internal_owner(session, dependency.id, "   ", principal=RECORDER)


def test_a_missing_dependency_refuses(session, project):
    with pytest.raises(ValueError, match="dependency"):
        assign_internal_owner(session, 999999999, "Dana", principal=RECORDER)


def test_a_projection_that_diverged_from_its_receipts_refuses(
    session, dependency
):
    assign_internal_owner(session, dependency.id, "Dana Fields", principal=RECORDER)
    dependency.internal_owner = "Tampered Name"
    session.flush()

    with pytest.raises(ValueError, match="diverged"):
        assign_internal_owner(
            session, dependency.id, "Lee Marsh", principal=RECORDER
        )


def test_the_audit_log_points_at_the_receipt_and_never_duplicates_it(
    session, dependency
):
    decision = assign_internal_owner(
        session, dependency.id, "Dana Fields", principal=RECORDER
    )

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == DEPENDENCY,
            AuditLog.entity_id == dependency.id,
            AuditLog.action == ASSIGN_INTERNAL_OWNER,
        )
    ).one()
    assert entry.after_json == {"work_decision_id": decision.id}
    assert entry.before_json is None


def test_receipts_are_immutable_below_the_service_boundary(session, dependency):
    decision = assign_internal_owner(
        session, dependency.id, "Dana Fields", principal=RECORDER
    )

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                update(WorkDecision)
                .where(WorkDecision.id == decision.id)
                .values(after_value="Rewritten")
            )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                delete(WorkDecision).where(WorkDecision.id == decision.id)
            )


def test_a_role_label_cannot_record_a_work_decision(session, dependency):
    """'system' and 'reviewer' are roles; the recorder is a person."""
    for label in ("local:system", "local:reviewer"):
        with pytest.raises(InvalidHumanPrincipal):
            assign_internal_owner(
                session, dependency.id, "Dana", principal=HumanPrincipal(label)
            )
    assert _decisions(session, dependency.id) == []
