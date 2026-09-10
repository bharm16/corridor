"""The one reading of a retained Follow-up Plan (#425, #526, ADR-0084 §1).

``native_follow_up_reading`` had no test file of its own while three surfaces
rendered its output: the internal report, the issue report, and — through
``project_workflow`` — the chase list. These tests hold the module to the two
things it owns. First, one type: the fields below are the ones
``delta_follow_up_plans`` retains, and there is no second dataclass of the same
name for a renderer to disagree with. Second, one answer to "is this plan still
open": the reversal of the packet act that recorded a plan is decided here, in
``undone_follow_up_plan_ids``, and ``outstanding_follow_up`` asks the same
function rather than repeating the join.

Fixtures come from ``test_issue_rendering`` because that module already builds
an adopted baseline, a Proposed Delta against it, and a recorded Needs
coordination outcome; a second copy of that scaffolding would be a second
opinion about what a recorded plan looks like.
"""

from __future__ import annotations

from datetime import timedelta

from corridor.native_follow_up_reading import (
    AcceptedFollowUpPlan,
    read_adopted_follow_up_plans,
    undone_follow_up_plan_ids,
)
from corridor.project_workflow import outstanding_follow_up
from test_issue_rendering import (  # noqa: F401  (fixtures are used by name)
    ALICE,
    CUTOFF,
    PLANNED_AT,
    RETURNS_AT,
    SUBJECT,
    _baseline,
    _delta,
    _plan,
    project,
    session,
)


def _recorded(session, project, **overrides):
    _, baseline = _baseline(session, project)
    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2027-03-01",
        baseline_revision=baseline,
    )
    arguments = {
        "question": "Which Promised For date does the utility hold to?",
        "return_date": RETURNS_AT,
    }
    arguments.update(overrides)
    result = _plan(session, project, delta, baseline, **arguments)
    return delta, int(result.revision_id), result


def test_one_plan_reads_back_as_the_fields_the_record_holds(session, project):
    """Every field is retained; none of them is composed by a renderer."""

    delta, revision, result = _recorded(session, project)

    (plan,) = read_adopted_follow_up_plans(
        session, project.id, revision, current=True, as_of=CUTOFF
    )
    assert isinstance(plan, AcceptedFollowUpPlan)
    assert plan.plan_id == result.children[0].follow_up_plan_id
    assert (plan.delta_id, plan.revision_id) == (delta.id, revision)
    assert plan.target_subject_identity == SUBJECT
    assert plan.target_field == "committed_date"
    assert plan.open_question == "Which Promised For date does the utility hold to?"
    assert plan.responsible_organization == "City Water"
    assert plan.responsible_principal is None
    assert plan.responsible == "City Water"
    assert plan.return_date == RETURNS_AT
    assert plan.recorded_by == "local:alice"
    assert plan.recorded_at == PLANNED_AT
    # The type has nowhere to put the proposed value, and nowhere to put a
    # next-action sentence nobody recorded.
    assert not hasattr(plan, "next_action")
    assert not [
        name for name in vars(plan) if "propos" in name and name != "delta_id"
    ]


def test_the_reading_and_the_chase_list_agree_on_which_plans_are_open(
    session, project
):
    """One question, one answer: the same plan, named by both readers.

    ``outstanding_follow_up`` is the chase list's authority and it now asks
    this module whether the recording act still stands, so the two readings
    cannot disagree about a reversal.
    """

    delta, revision, result = _recorded(session, project)

    plans = read_adopted_follow_up_plans(
        session, project.id, revision, current=True, as_of=CUTOFF
    )
    needs = outstanding_follow_up(
        session, project_id=project.id, open_delta_ids=(delta.id,)
    )

    assert [plan.plan_id for plan in plans] == [need.plan_id for need in needs]
    assert [plan.open_question for plan in plans] == [
        need.open_question for need in needs
    ]
    assert not session.scalars(
        undone_follow_up_plan_ids((project.id,))
    ).all()


def test_a_reversed_packet_act_raises_no_ask_in_either_reading(session, project):
    """An act that never stood raises no ask, decided in one place."""

    from corridor.review_packets import reverse_review_packet

    delta, revision, result = _recorded(session, project)
    reversal = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=int(result.receipt_id),
        principal=ALICE,
        idempotency_key=f"reverse:{result.receipt_id}",
        reversed_at=PLANNED_AT + timedelta(hours=1),
    )
    session.expire_all()
    boundary = max(revision, int(reversal.revision_id or 0))

    assert session.scalars(undone_follow_up_plan_ids((project.id,))).all() == [
        result.children[0].follow_up_plan_id
    ]
    assert (
        read_adopted_follow_up_plans(
            session,
            project.id,
            boundary,
            current=True,
            as_of=CUTOFF,
        )
        == ()
    )
    assert (
        outstanding_follow_up(
            session, project_id=project.id, open_delta_ids=(delta.id,)
        )
        == ()
    )
