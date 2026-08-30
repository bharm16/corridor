"""Public behavior of the grouped Constraint Follow-up Plan (#333).

One roster-backed Save commits the affected append-only Coordination
Decisions and their grouping receipt atomically, or nothing (ADR-0035,
ADR-0038).  Undo appends the grouped compensation and refuses once later
work depends on a result; a later correction chains forward instead.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from access_support import seed_membership
from corridor.db import Session, engine
from corridor.models import (
    Dependency,
    ExternalOrg,
    FollowUpPlanReceipt,
    FollowUpPlanReversal,
    Project,
    ProjectRosterEntry,
    WorkDecision,
)
from corridor.principals import HumanPrincipal
from corridor.web.app import app, get_human_principal, get_session
from corridor.work_decisions import (
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    FollowUpPlanDraft,
    FollowUpPlanPredecessors,
    FollowUpPlanRefusal,
    FollowUpPlanUndoRefusal,
    StaleFollowUpPlan,
    complete_next_action,
    current_deferral_decision,
    current_follow_up_plan_receipt,
    current_internal_owner_decision,
    current_next_action_decision,
    defer_work,
    save_follow_up_plan,
    undo_follow_up_plan,
)

RECORDER = HumanPrincipal("local:plan-coordinator")
ACTION = FOLLOW_UP_NEXT_ACTION_CHOICES[0]
OTHER_ACTION = FOLLOW_UP_NEXT_ACTION_CHOICES[1]


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
        slug="follow-up-plan-test",
        name="Follow-up plan test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    seed_membership(session, project, RECORDER)
    return project


@pytest.fixture
def party(session):
    party = ExternalOrg(name="CenterPoint Energy")
    session.add(party)
    session.flush()
    return party


@pytest.fixture
def dependency(session, project, party):
    dependency = Dependency(
        project_id=project.id,
        ref_code="UC-041",
        dep_type="utility_relocation",
        title="12-inch gas main",
        station_from="102+00",
        station_to="108+50",
        external_org_id=party.id,
    )
    session.add(dependency)
    session.flush()
    return dependency


@pytest.fixture
def roster_entry(session, project):
    entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:maria-alvarez",
        display_name="Maria Alvarez",
    )
    session.add(entry)
    session.flush()
    return entry


def _draft(dependency, roster_entry, **overrides):
    values = dict(
        dependency_id=dependency.id,
        internal_owner_roster_entry_id=roster_entry.id,
        next_action=ACTION,
        action_due_date=date(2026, 9, 15),
        action_due_date_unknown_reason=None,
        expected=FollowUpPlanPredecessors(),
    )
    values.update(overrides)
    return FollowUpPlanDraft(**values)


def _decisions(session, dependency):
    return session.scalars(
        select(WorkDecision)
        .where(WorkDecision.dependency_id == dependency.id)
        .order_by(WorkDecision.id)
    ).all()


def _client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: RECORDER
    return TestClient(app)


def _clear_overrides():
    app.dependency_overrides.clear()


# --- the atomic Save ---------------------------------------------------------


def test_one_save_commits_both_decisions_and_one_grouping_receipt(
    session, project, dependency, roster_entry
):
    result = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )

    assert dependency.internal_owner == "Maria Alvarez"
    assert dependency.next_action == ACTION
    assert dependency.action_due_date == date(2026, 9, 15)
    owner_tail = current_internal_owner_decision(session, dependency.id)
    action_tail = current_next_action_decision(session, dependency.id)
    assert owner_tail.id == result.receipt.internal_owner_decision_id
    assert action_tail.id == result.receipt.next_action_decision_id
    # The grouping receipt retains the stable roster identity beside the
    # displayed name the decision recorded.
    assert result.receipt.internal_owner_roster_entry_id == roster_entry.id
    assert owner_tail.after_value == "Maria Alvarez"
    assert result.receipt.recorded_by == RECORDER.subject
    assert current_follow_up_plan_receipt(session, dependency.id).id == (
        result.receipt.id
    )


def test_a_late_refusal_rolls_back_the_whole_save(
    session, project, dependency, roster_entry
):
    # The unknown-date reason is checked inside the Next Action writer, after
    # the Internal Owner decision was already appended; the refusal must
    # leave nothing behind.
    with pytest.raises(FollowUpPlanRefusal):
        save_follow_up_plan(
            session,
            _draft(
                dependency,
                roster_entry,
                action_due_date=None,
                action_due_date_unknown_reason="because",
            ),
            principal=RECORDER,
        )
    assert _decisions(session, dependency) == []
    assert dependency.internal_owner is None
    assert session.scalars(select(FollowUpPlanReceipt)).all() == []


def test_free_text_and_foreign_and_inactive_identities_are_refused(
    session, project, dependency, roster_entry
):
    other_project = Project(slug="other-project", name="Other", is_synthetic=True)
    session.add(other_project)
    session.flush()
    foreign_entry = ProjectRosterEntry(
        project_id=other_project.id,
        principal_subject="local:someone-else",
        display_name="Someone Else",
    )
    inactive_entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:left-the-project",
        display_name="Left The Project",
        active=False,
    )
    session.add_all([foreign_entry, inactive_entry])
    session.flush()

    for bad_entry_id in (foreign_entry.id, inactive_entry.id, None, 999999):
        with pytest.raises(FollowUpPlanRefusal):
            save_follow_up_plan(
                session,
                _draft(
                    dependency,
                    roster_entry,
                    internal_owner_roster_entry_id=bad_entry_id,
                ),
                principal=RECORDER,
            )
    with pytest.raises(FollowUpPlanRefusal):
        save_follow_up_plan(
            session,
            _draft(dependency, roster_entry, next_action="free text step"),
            principal=RECORDER,
        )
    assert _decisions(session, dependency) == []


def test_a_stale_concurrent_save_refuses_whole_and_overwrites_nothing(
    session, project, dependency, roster_entry
):
    first = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )
    # A second screen was read before the first Save landed: its expected
    # predecessors are both absent.
    with pytest.raises(StaleFollowUpPlan):
        save_follow_up_plan(
            session,
            _draft(dependency, roster_entry, next_action=OTHER_ACTION),
            principal=RECORDER,
        )
    assert dependency.next_action == ACTION
    assert (
        current_next_action_decision(session, dependency.id).id
        == first.receipt.next_action_decision_id
    )


def test_a_saved_assignment_is_effective_without_acceptance_or_delivery(
    session, project, dependency, roster_entry
):
    save_follow_up_plan(session, _draft(dependency, roster_entry), principal=RECORDER)
    # The projection and the authoritative decision agree immediately; no
    # notification or recipient acceptance is part of the commit.
    tail = current_internal_owner_decision(session, dependency.id)
    assert tail.after_value == dependency.internal_owner == "Maria Alvarez"


def test_an_unchanged_plan_is_refused_instead_of_recording_a_no_op(
    session, project, dependency, roster_entry
):
    first = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )
    with pytest.raises(FollowUpPlanRefusal):
        save_follow_up_plan(
            session,
            _draft(
                dependency,
                roster_entry,
                expected=FollowUpPlanPredecessors(
                    internal_owner_decision_id=first.receipt.internal_owner_decision_id,
                    next_action_decision_id=first.receipt.next_action_decision_id,
                ),
            ),
            principal=RECORDER,
        )
    assert len(_decisions(session, dependency)) == 2


# --- grouped Undo ------------------------------------------------------------


def test_undo_appends_reversals_and_preserves_the_original_decisions(
    session, project, dependency, roster_entry
):
    result = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )
    reversal = undo_follow_up_plan(
        session, result.receipt.id, principal=RECORDER
    )

    assert dependency.internal_owner is None
    assert dependency.next_action is None
    assert dependency.action_due_date is None
    decisions = _decisions(session, dependency)
    assert len(decisions) == 4  # two originals preserved, two reversals appended
    originals = {
        result.receipt.internal_owner_decision_id,
        result.receipt.next_action_decision_id,
    }
    assert originals <= {decision.id for decision in decisions}
    assert current_internal_owner_decision(session, dependency.id).id == (
        reversal.internal_owner_reversal_decision_id
    )
    assert current_follow_up_plan_receipt(session, dependency.id) is None
    with pytest.raises(FollowUpPlanUndoRefusal):
        undo_follow_up_plan(session, result.receipt.id, principal=RECORDER)


def test_undo_restores_the_exact_prior_plan_values(
    session, project, dependency, roster_entry
):
    first = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )
    second_entry = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:jordan-reyes",
        display_name="Jordan Reyes",
    )
    session.add(second_entry)
    session.flush()
    second = save_follow_up_plan(
        session,
        _draft(
            dependency,
            second_entry,
            internal_owner_roster_entry_id=second_entry.id,
            next_action=OTHER_ACTION,
            action_due_date=None,
            action_due_date_unknown_reason="awaiting_external_information",
            expected=FollowUpPlanPredecessors(
                internal_owner_decision_id=first.receipt.internal_owner_decision_id,
                next_action_decision_id=first.receipt.next_action_decision_id,
            ),
        ),
        principal=RECORDER,
    )

    undo_follow_up_plan(session, second.receipt.id, principal=RECORDER)

    assert dependency.internal_owner == "Maria Alvarez"
    assert dependency.next_action == ACTION
    assert dependency.action_due_date == date(2026, 9, 15)
    assert dependency.action_due_date_reason is None


def test_undo_restores_a_deferral_the_grouped_save_resumed(
    session, project, dependency, roster_entry
):
    defer_work(
        session,
        dependency.id,
        reason="waiting_for_external_party",
        return_date=date(2026, 10, 1),
        principal=RECORDER,
    )
    result = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )
    assert result.receipt.resumed_deferral_decision_id is not None
    assert dependency.deferral_reason is None

    undo_follow_up_plan(session, result.receipt.id, principal=RECORDER)

    assert dependency.deferral_reason == "waiting_for_external_party"
    assert dependency.deferral_return_date == date(2026, 10, 1)


def test_undo_refuses_when_a_later_act_depends_on_the_save(
    session, project, dependency, roster_entry
):
    result = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )
    completion = complete_next_action(
        session,
        dependency.id,
        no_follow_up_reason="no_immediate_follow_up",
        principal=RECORDER,
    )

    with pytest.raises(FollowUpPlanUndoRefusal):
        undo_follow_up_plan(session, result.receipt.id, principal=RECORDER)
    # The refusal never cascades: the later completion stands untouched.
    assert current_next_action_decision(session, dependency.id).id == completion.id
    assert session.scalars(select(FollowUpPlanReversal)).all() == []


# --- later targeted correction ----------------------------------------------


def test_a_later_correction_chains_the_changed_choice_to_its_predecessor(
    session, project, dependency, roster_entry
):
    first = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )
    corrected = save_follow_up_plan(
        session,
        _draft(
            dependency,
            roster_entry,
            next_action=OTHER_ACTION,
            expected=FollowUpPlanPredecessors(
                internal_owner_decision_id=first.receipt.internal_owner_decision_id,
                next_action_decision_id=first.receipt.next_action_decision_id,
            ),
        ),
        principal=RECORDER,
    )

    # Only the changed choice appends; the receipt names exactly it.
    assert corrected.internal_owner_decision is None
    assert corrected.receipt.internal_owner_decision_id is None
    successor = corrected.next_action_decision
    assert successor.predecessor_decision_id == first.receipt.next_action_decision_id
    assert successor.before_value is not None
    # Earlier history is untouched, not edited.
    original = session.get(WorkDecision, first.receipt.next_action_decision_id)
    assert ACTION in original.after_value


# --- the customer HTTP route -------------------------------------------------


def test_the_constraint_page_offers_one_plan_form_and_saves_it_atomically(
    session, project, dependency, roster_entry
):
    try:
        with _client(session) as client:
            page = client.get(f"/ledger/{project.slug}/{dependency.id}")
            assert page.status_code == 200
            assert page.text.count(f"/dependencies/{dependency.id}/plan") == 1
            # The dissolved free-text routes are gone, not merely unlinked.
            assert f"/dependencies/{dependency.id}/owner" not in page.text
            assert f"/dependencies/{dependency.id}/action\"" not in page.text
            assert 'name="owner"' not in page.text

            saved = client.post(
                f"/dependencies/{dependency.id}/plan",
                data={
                    "slug": project.slug,
                    "internal_owner_roster_entry_id": str(roster_entry.id),
                    "next_action": ACTION,
                    "action_due_date": "2026-09-15",
                    "action_due_date_unknown_reason": "",
                    "expected_internal_owner_decision_id": "",
                    "expected_next_action_decision_id": "",
                },
                follow_redirects=False,
            )
            assert saved.status_code == 303

            refreshed = client.get(f"/ledger/{project.slug}/{dependency.id}")
            assert "Maria Alvarez" in refreshed.text
            assert ACTION in refreshed.text
            assert "2026-09-15" in refreshed.text
    finally:
        _clear_overrides()
    receipt = current_follow_up_plan_receipt(session, dependency.id)
    assert receipt is not None
    assert receipt.internal_owner_roster_entry_id == roster_entry.id


def test_the_route_refuses_stale_and_invalid_submissions(
    session, project, dependency, roster_entry
):
    try:
        with _client(session) as client:
            base = {
                "slug": project.slug,
                "internal_owner_roster_entry_id": str(roster_entry.id),
                "next_action": ACTION,
                "action_due_date": "2026-09-15",
                "action_due_date_unknown_reason": "",
                "expected_internal_owner_decision_id": "",
                "expected_next_action_decision_id": "",
            }
            assert (
                client.post(
                    f"/dependencies/{dependency.id}/plan",
                    data={**base, "next_action": "call them maybe"},
                    follow_redirects=False,
                ).status_code
                == 400
            )
            assert (
                client.post(
                    f"/dependencies/{dependency.id}/plan",
                    data={**base, "internal_owner_roster_entry_id": "Maria Alvarez"},
                    follow_redirects=False,
                ).status_code
                == 400
            )
            first = client.post(
                f"/dependencies/{dependency.id}/plan",
                data=base,
                follow_redirects=False,
            )
            assert first.status_code == 303
            # The same screen submitted again is stale: it re-renders the
            # newer state with 409 and applies nothing.
            stale = client.post(
                f"/dependencies/{dependency.id}/plan",
                data={**base, "next_action": OTHER_ACTION},
                follow_redirects=False,
            )
            assert stale.status_code == 409
            assert "Maria Alvarez" in stale.text
    finally:
        _clear_overrides()
    assert dependency.next_action == ACTION


def test_the_page_offers_grouped_undo_and_it_reverses_the_save(
    session, project, dependency, roster_entry
):
    result = save_follow_up_plan(
        session, _draft(dependency, roster_entry), principal=RECORDER
    )
    try:
        with _client(session) as client:
            page = client.get(f"/ledger/{project.slug}/{dependency.id}")
            assert f"/dependencies/{dependency.id}/plan/undo" in page.text
            undone = client.post(
                f"/dependencies/{dependency.id}/plan/undo",
                data={
                    "slug": project.slug,
                    "receipt_id": str(result.receipt.id),
                },
                follow_redirects=False,
            )
            assert undone.status_code == 303
            refreshed = client.get(f"/ledger/{project.slug}/{dependency.id}")
            assert "unassigned" in refreshed.text
            assert f"/dependencies/{dependency.id}/plan/undo" not in refreshed.text
    finally:
        _clear_overrides()
    assert dependency.internal_owner is None


def test_the_dissolved_free_text_routes_no_longer_exist(
    session, project, dependency, roster_entry
):
    try:
        with _client(session) as client:
            for path, data in (
                (
                    f"/dependencies/{dependency.id}/owner",
                    {"slug": project.slug, "owner": "Maria Alvarez"},
                ),
                (
                    f"/dependencies/{dependency.id}/action",
                    {"slug": project.slug, "action": "call them"},
                ),
            ):
                response = client.post(path, data=data, follow_redirects=False)
                assert response.status_code in (404, 405)
    finally:
        _clear_overrides()
    assert _decisions(session, dependency) == []


# --- Close, successor, note, and defer from the Constraint surface (#334) -----


def test_the_constraint_page_completes_with_a_structured_successor_and_note(
    session, project, dependency, roster_entry
):
    save_follow_up_plan(session, _draft(dependency, roster_entry), principal=RECORDER)
    action = current_next_action_decision(session, dependency.id)
    try:
        with _client(session) as client:
            page = client.get(f"/ledger/{project.slug}/{dependency.id}").text
            assert f"/dependencies/{dependency.id}/action/complete" in page
            assert f"/dependencies/{dependency.id}/defer" in page

            completed = client.post(
                f"/dependencies/{dependency.id}/action/complete",
                data={
                    "slug": project.slug,
                    "expected_next_action_decision_id": str(action.id),
                    "successor_action": OTHER_ACTION,
                    "successor_due_date": "2026-10-01",
                    "note": "Closed on the site walk.",
                },
                follow_redirects=False,
            )
            assert completed.status_code == 303
    finally:
        _clear_overrides()

    completion = session.scalar(
        select(WorkDecision).where(
            WorkDecision.dependency_id == dependency.id,
            WorkDecision.decision_type == "complete_next_action",
        )
    )
    assert completion.predecessor_decision_id == action.id
    assert completion.note == "Closed on the site walk."
    # The successor is the current live action, chained to the completion.
    successor = current_next_action_decision(session, dependency.id)
    assert successor.predecessor_decision_id == completion.id
    assert OTHER_ACTION in successor.after_value


def test_the_constraint_page_refuses_a_stale_close(
    session, project, dependency, roster_entry
):
    save_follow_up_plan(session, _draft(dependency, roster_entry), principal=RECORDER)
    stale = current_next_action_decision(session, dependency.id)
    # The action the screen showed is replaced before the coordinator submits.
    save_follow_up_plan(
        session,
        _draft(
            dependency,
            roster_entry,
            next_action=OTHER_ACTION,
            expected=FollowUpPlanPredecessors(
                internal_owner_decision_id=current_internal_owner_decision(
                    session, dependency.id
                ).id,
                next_action_decision_id=stale.id,
            ),
        ),
        principal=RECORDER,
    )
    current = current_next_action_decision(session, dependency.id)
    assert current.id != stale.id

    try:
        with _client(session) as client:
            refused = client.post(
                f"/dependencies/{dependency.id}/action/complete",
                data={
                    "slug": project.slug,
                    "expected_next_action_decision_id": str(stale.id),
                    "no_follow_up_reason": "no_immediate_follow_up",
                },
                follow_redirects=False,
            )
            assert refused.status_code == 409
    finally:
        _clear_overrides()
    # Nothing closed: the newer action is still current, unchanged.
    assert current_next_action_decision(session, dependency.id).id == current.id


def test_the_constraint_page_refuses_a_free_text_successor(
    session, project, dependency, roster_entry
):
    save_follow_up_plan(session, _draft(dependency, roster_entry), principal=RECORDER)
    action = current_next_action_decision(session, dependency.id)
    try:
        with _client(session) as client:
            refused = client.post(
                f"/dependencies/{dependency.id}/action/complete",
                data={
                    "slug": project.slug,
                    "expected_next_action_decision_id": str(action.id),
                    "successor_action": "call them next week",
                    "successor_due_date": "2026-10-01",
                },
                follow_redirects=False,
            )
            assert refused.status_code == 400
    finally:
        _clear_overrides()
    assert current_next_action_decision(session, dependency.id).id == action.id


def test_the_constraint_page_defers_with_a_reason_and_return_date(
    session, project, dependency, roster_entry
):
    save_follow_up_plan(session, _draft(dependency, roster_entry), principal=RECORDER)
    action = current_next_action_decision(session, dependency.id)
    try:
        with _client(session) as client:
            # A deferral without a return date is refused: not a synonym for an
            # unknown Action Due Date.
            no_date = client.post(
                f"/dependencies/{dependency.id}/defer",
                data={
                    "slug": project.slug,
                    "expected_next_action_decision_id": str(action.id),
                    "deferral_reason": "waiting_for_information",
                    "return_date": "",
                },
                follow_redirects=False,
            )
            assert no_date.status_code == 400
            assert current_deferral_decision(session, dependency.id) is None

            deferred = client.post(
                f"/dependencies/{dependency.id}/defer",
                data={
                    "slug": project.slug,
                    "expected_next_action_decision_id": str(action.id),
                    "deferral_reason": "waiting_for_information",
                    "return_date": "2026-10-01",
                },
                follow_redirects=False,
            )
            assert deferred.status_code == 303
    finally:
        _clear_overrides()
    deferral = current_deferral_decision(session, dependency.id)
    assert deferral.deferral_reason == "waiting_for_information"
    assert deferral.deferral_return_date == date(2026, 10, 1)
    # The action is untouched by the deferral.
    assert current_next_action_decision(session, dependency.id).id == action.id
