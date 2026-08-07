"""The Work Decision service seam: assign an Internal Owner (ADR-0025).

A Work Decision proves only what the project decided and when. These tests
drive the one seam every later Work Decision surface consumes: principal +
Dependency + state change in, typed immutable receipt out, current value
projected, audit holding a pointer and never the payload.
"""

import json
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
    cancel_next_action,
    complete_next_action,
    current_internal_owner_decision,
    current_next_action_decision,
    set_next_action,
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


# --- The Next Action lifecycle (#174) ---------------------------------------


def test_setting_a_next_action_writes_one_receipt_for_action_and_date(
    session, dependency
):
    decision = set_next_action(
        session,
        dependency.id,
        "Request relocation schedule from the City",
        due_date=date(2026, 9, 1),
        principal=RECORDER,
    )

    assert decision.decision_type == "set_next_action"
    assert decision.field == "next_action"
    assert decision.before_value is None
    assert json.loads(decision.after_value) == {
        "action": "Request relocation schedule from the City",
        "due_date": "2026-09-01",
    }
    session.refresh(dependency)
    assert dependency.next_action == "Request relocation schedule from the City"
    assert dependency.action_due_date == date(2026, 9, 1)


def test_the_action_due_date_is_optional(session, dependency):
    set_next_action(
        session, dependency.id, "Walk the crossing", principal=RECORDER
    )
    session.refresh(dependency)
    assert dependency.next_action == "Walk the crossing"
    assert dependency.action_due_date is None


def test_completion_clears_the_current_action_and_pins_its_predecessor(
    session, dependency
):
    first = set_next_action(
        session, dependency.id, "Walk the crossing", principal=RECORDER
    )
    done = complete_next_action(session, dependency.id, principal=RECORDER)

    assert done.decision_type == "complete_next_action"
    assert done.predecessor_decision_id == first.id
    assert json.loads(done.before_value)["action"] == "Walk the crossing"
    assert done.after_value is None
    session.refresh(dependency)
    assert dependency.next_action is None
    assert dependency.action_due_date is None


def test_cancellation_is_a_distinct_decision_type(session, dependency):
    set_next_action(session, dependency.id, "Walk the crossing", principal=RECORDER)
    withdrawn = cancel_next_action(session, dependency.id, principal=RECORDER)

    assert withdrawn.decision_type == "cancel_next_action"
    session.refresh(dependency)
    assert dependency.next_action is None


def test_completing_nothing_refuses(session, dependency):
    with pytest.raises(ValueError, match="no current Next Action"):
        complete_next_action(session, dependency.id, principal=RECORDER)
    with pytest.raises(ValueError, match="no current Next Action"):
        cancel_next_action(session, dependency.id, principal=RECORDER)


def test_the_chain_reconstructs_in_order_across_the_lifecycle(
    session, dependency
):
    first = set_next_action(
        session, dependency.id, "Walk the crossing", principal=RECORDER
    )
    done = complete_next_action(session, dependency.id, principal=RECORDER)
    second = set_next_action(
        session, dependency.id, "Confirm as-builts received", principal=RECORDER
    )

    assert done.predecessor_decision_id == first.id
    assert second.predecessor_decision_id == done.id
    session.refresh(dependency)
    assert dependency.next_action == "Confirm as-builts received"


def test_setting_the_identical_action_again_records_nothing_new(
    session, dependency
):
    set_next_action(
        session,
        dependency.id,
        "Walk the crossing",
        due_date=date(2026, 9, 1),
        principal=RECORDER,
    )
    set_next_action(
        session,
        dependency.id,
        "Walk the crossing",
        due_date=date(2026, 9, 1),
        principal=RECORDER,
    )
    actions = [
        d for d in _decisions(session, dependency.id) if d.field == "next_action"
    ]
    assert len(actions) == 1


def test_owner_and_action_chains_are_independent(session, dependency):
    assign_internal_owner(session, dependency.id, "Dana Fields", principal=RECORDER)
    set_next_action(session, dependency.id, "Walk the crossing", principal=RECORDER)

    owner = current_internal_owner_decision(session, dependency.id)
    action = current_next_action_decision(session, dependency.id)
    assert owner is not None and owner.field == "internal_owner"
    assert action is not None and action.field == "next_action"
    assert owner.predecessor_decision_id is None
    assert action.predecessor_decision_id is None


def test_a_diverged_action_projection_refuses(session, dependency):
    set_next_action(session, dependency.id, "Walk the crossing", principal=RECORDER)
    dependency.next_action = "Tampered"
    session.flush()

    with pytest.raises(ValueError, match="diverged"):
        set_next_action(
            session, dependency.id, "Anything else", principal=RECORDER
        )


def test_a_blank_action_refuses(session, dependency):
    with pytest.raises(ValueError, match="Next Action"):
        set_next_action(session, dependency.id, "   ", principal=RECORDER)
